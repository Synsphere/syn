"""WSL-to-Windows host authority adapter. Never requests root private material.

Windows owns the DPAPI root vault and the restricted named pipe. This client
may retrieve non-root operational credentials, subject to the broker's policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass

from syn_client.operator_protocol import SynProtocolError


# The broker serializes vault operations and may be displaying a native
# approval dialog. Waiting for a free pipe must not share the short response
# budget, and must never cause a submitted mutation to be replayed.
CONNECT_TIMEOUT_SECONDS = 300
RESPONSE_TIMEOUT_SECONDS = 30
APPROVAL_TIMEOUT_SECONDS = 300
_APPROVAL_OPERATIONS = {"create", "group_key", "dag_anchor", "peer_record", "sign"}
_BRIDGE_FAILURES = {
    "input-invalid": "request validation failed before submission",
    "connect-timeout": "the serialized Windows broker remained busy or unavailable before submission",
    "connect-denied": "Windows denied access to the custody pipe before submission",
    "connect-failed": "the Windows custody pipe could not be opened before submission",
    "write-timeout": "request submission timed out; the outcome is unconfirmed",
    "write-failed": "request submission failed; the outcome is unconfirmed",
    "read-timeout": "the broker response timed out; the outcome is unconfirmed",
    "read-failed": "the broker response could not be read; the outcome is unconfirmed",
    "response-invalid": "the broker response was empty or oversized; the outcome is unconfirmed",
}


def _transport_failure(result):
    # Never echo arbitrary PowerShell stderr: it may contain request data,
    # credentials or a script dump. Only our fixed diagnostic markers survive.
    for line in str(getattr(result, "stderr", "")).splitlines():
        match = re.fullmatch(r"SYN_HOST_CUSTODY_FAILURE:([a-z-]+)", line.strip())
        if match and match[1] in _BRIDGE_FAILURES:
            return _BRIDGE_FAILURES[match[1]]
    return f"PowerShell/WSL bridge exited with status {result.returncode}; no trusted transport diagnostic was returned"


def enabled() -> bool:
    return bool(os.environ.get("SYN_HOST_CUSTODY_PIPE"))


def request(body):
    pipe = os.environ.get("SYN_HOST_CUSTODY_PIPE", "")
    script = os.environ.get("SYN_HOST_CUSTODY_CLIENT_WIN", "")
    powershell = os.environ.get("SYN_HOST_CUSTODY_POWERSHELL", "")
    if not re.fullmatch(r"SynHostCustody-[a-f0-9]{32}", pipe):
        raise SynProtocolError("Missing or malformed Windows host custody pipe")
    if not script or not powershell or not os.path.isabs(powershell):
        raise SynProtocolError("Windows custody bridge is not configured")
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False)
    if len(raw) > 130_000:
        raise SynProtocolError("Host custody request exceeds size limit")
    response_timeout = (APPROVAL_TIMEOUT_SECONDS if body.get("op") in _APPROVAL_OPERATIONS
                        else RESPONSE_TIMEOUT_SECONDS)
    try:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Pipe", pipe,
             "-ConnectTimeoutMilliseconds", str(CONNECT_TIMEOUT_SECONDS * 1000),
             "-ResponseTimeoutMilliseconds", str(response_timeout * 1000)],
            input=raw+"\n", capture_output=True, text=True,
            timeout=CONNECT_TIMEOUT_SECONDS + response_timeout + 10,
            check=False)
    except subprocess.TimeoutExpired as error:
        raise SynProtocolError("Windows host custody transport deadline exceeded; request outcome is unconfirmed; no fallback to WSL secrets") from error
    except OSError as error:
        raise SynProtocolError("Windows host custody PowerShell bridge could not start; check WSL interop; no fallback to WSL secrets") from error
    if result.returncode != 0:
        raise SynProtocolError("Windows host custody transport failed: " + _transport_failure(result)
                               + "; no fallback to WSL secrets")
    if len(result.stdout) > 140_000:
        raise SynProtocolError("Windows host custody response exceeds size limit; no fallback to WSL secrets")
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise SynProtocolError("Host custody response is malformed") from error
    if not isinstance(response, dict) or set(response) not in ({"ok", "result"}, {"ok", "error"}):
        raise SynProtocolError("Host custody response has unknown fields")
    if response["ok"] is not True:
        raise SynProtocolError("Host custody rejected request: " + str(response.get("error", "unknown"))[:200])
    return response["result"]


class HostKeyringProxy:
    """Non-root keystore slots; the broker blocks all HD root/private slots."""

    def get_password(self, service: str, account: str) -> str | None:
        value = request({"op": "get", "service": service, "account": "slot", "slot": account})
        if value is not None and not isinstance(value, str):
            raise SynProtocolError("Host credential result must be text or null")
        return value

    def set_password(self, service: str, account: str, value: str) -> None:
        result = request({"op": "set", "service": service, "account": "slot",
                          "slot": account, "value": value})
        if result is not True:
            raise SynProtocolError("Host credential write not acknowledged")


def create_operator_identity(*, service: str, account: str, lock_dir) -> dict:
    # The host displays a separate native approval dialog. WSL does not supply
    # entropy or private material. A second initialization is rejected.
    value = request({"op": "create", "service": service, "account": account})
    if not isinstance(value, dict) or not re.fullmatch(r"[0-9a-f]{64}",
                                                       value.get("base_public_key_hash", "")):
        raise SynProtocolError("Host returned invalid public Operator identity")
    return value


@dataclass(frozen=True, slots=True)
class HostHdOperatorSigner:
    service: str
    account: str
    expected_public_hash: bytes

    def __post_init__(self):
        if type(self.expected_public_hash) is not bytes or len(self.expected_public_hash) != 32:
            raise SynProtocolError("Pinned Windows operator hash must be 32 bytes")
        if hashlib.sha256(self.public_key).digest() != self.expected_public_hash:
            raise SynProtocolError("Host Operator does not match pinned authority")

    def _call(self, operation, **arguments):
        return request({"op": operation, "service": self.service, "account": self.account,
                        "pin": self.expected_public_hash.hex(), **arguments})

    @property
    def public_key(self):
        response = self._call("identity")
        try:
            return bytes.fromhex(response["base_public_key"])
        except (ValueError, TypeError, KeyError) as error:
            raise SynProtocolError("Host Operator identity response is invalid") from error

    def sign(self, payload: bytes) -> bytes:
        if type(payload) is not bytes or not 0 < len(payload) <= 65_536:
            raise SynProtocolError("Host operator signing input is invalid")
        try:
            signature = bytes.fromhex(self._call("sign", payload=payload.hex()))
        except (ValueError, TypeError) as error:
            raise SynProtocolError("Invalid host Operator signature") from error
        if len(signature) != 64:
            raise SynProtocolError("Host Operator signature has wrong length")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(self.public_key).verify(signature, payload)
        except Exception as error:
            raise SynProtocolError("Host signature failed verification against pinned Operator") from error
        return signature

    def derive_candidate_public_hashes(self, inheritance: int) -> tuple[bytes, ...]:
        if type(inheritance) is not int or not 0 <= inheritance <= 0xFFFFFF:
            raise SynProtocolError("Invalid host inheritance")
        raw = self._call("candidates", inheritance=inheritance)
        try:
            hashes = tuple(bytes.fromhex(v) for v in raw)
        except (TypeError, ValueError) as error:
            raise SynProtocolError("Invalid host candidates") from error
        if len(hashes) != 8 or any(len(x) != 32 for x in hashes):
            raise SynProtocolError("Host returned invalid candidate public hashes")
        return hashes

    def sign_peer_record(self, body) -> bytes:
        """Request a fixed public binding, with host-retained exact approval."""
        from syn.peer_record import DOMAIN, canonical, validate_body
        validate_body(body, expected_operator=self.expected_public_hash.hex())
        try:
            signature = bytes.fromhex(self._call("peer_record", body=body))
        except (ValueError, TypeError) as error:
            raise SynProtocolError("Invalid host peer record signature") from error
        if len(signature) != 64:
            raise SynProtocolError("Host peer record signature has wrong length")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(self.public_key).verify(signature, DOMAIN + canonical(body))
        except Exception as error:
            raise SynProtocolError("Host peer signature differs from pinned Operator") from error
        return signature

    def device_commitment(self, hardware_id: bytes) -> str:
        if type(hardware_id) is not bytes or not 6 <= len(hardware_id) <= 256:
            raise SynProtocolError("Invalid device identifier")
        value = self._call("device_commitment", hardware=hardware_id.hex())
        if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
            raise SynProtocolError("Invalid host device commitment")
        return value

    def derive_group_key(self, network_id: str, group_id: str, epoch: int) -> bytes:
        # The host must explicitly approve first use of an epoch before
        # returning a derived group key for the existing group-seal protocol.
        try:
            result = bytes.fromhex(self._call(
                "group_key", network_id=network_id, group_id=group_id, epoch=epoch))
        except (TypeError, ValueError) as error:
            raise SynProtocolError("Invalid host group key") from error
        if len(result) != 32:
            raise SynProtocolError("Host group key must contain 32 bytes")
        return result


def operator_signer(service: str, account: str, expected: bytes):
    if enabled():
        return HostHdOperatorSigner(service, account, expected)
    from .hd_keys import PythonHdOperatorSigner
    return PythonHdOperatorSigner(service, account, expected)

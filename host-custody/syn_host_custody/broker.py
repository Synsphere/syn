"""Windows-only DPAPI vault and SID-restricted named-pipe server.

No root entropy or extended private key is returned by this wire protocol.
Only one broker owns the vault for its lifetime. The Windows launcher controls
its process lifetime and does not expose a TCP listener.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

from .core import Authority, canonical, MAX_REQUEST, public_identity


class VaultInUseError(RuntimeError):
    """A live Windows handle already owns the host authority's exclusive lock."""

    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        super().__init__(
            "Another Syn host-custody process is already using this Windows vault. "
            "Close its Syn launcher before starting another instance. If that launcher "
            "has already closed, stop its remaining syn_host_custody.broker process "
            "and retry. Do not delete authority.lock or vault-v1.dpapi."
        )


class WindowsVault:
    def __init__(self, directory=None):
        if os.name != "nt":
            raise OSError("Windows host custody is only supported on Windows")
        import win32crypt
        self.crypto = win32crypt
        base = Path(os.environ["LOCALAPPDATA"]) / "Syn" / "HostCustody" if directory is None else Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "vault-v1.dpapi"
        self.lock_path = base / "authority.lock"
        self._lock = None
        self._closed = False
        # One Windows host process exclusively owns the vault. Never allow two
        # brokers to race when a second Syn launcher is started.
        import pywintypes
        import win32con
        import win32file
        self._close_handle = win32file.CloseHandle
        try:
            self._lock = win32file.CreateFile(str(self.lock_path),
                win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                win32con.OPEN_ALWAYS, win32con.FILE_ATTRIBUTE_NORMAL, None)
        except pywintypes.error as error:
            if error.winerror in (32, 33):  # Sharing violation / lock violation.
                raise VaultInUseError(self.lock_path) from error
            raise
        try:
            self.data = self._load()
        except BaseException:
            # Corrupt/incompatible vault data must not leave this process owning
            # the lock after construction failed.
            self.close()
            raise

    def close(self):
        self._closed = True
        if self._lock is not None:
            # Retain ownership if CloseHandle fails so an explicit retry can
            # release it. Never continue using the vault after a close attempt.
            self._close_handle(self._lock)
            self._lock = None

    def __enter__(self):
        self._require_healthy()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "identities": {}, "slots": {}, "approvals": []}
        sealed = self.path.read_bytes()
        raw = self.crypto.CryptUnprotectData(sealed, None, None, None, 1)[1]
        body = json.loads(raw)
        if body.get("version") != 1 or set(body) != {"version", "identities", "slots", "approvals"}:
            raise ValueError("Host vault format is unsupported; refusing key replacement")
        return body

    def _persist(self):
        self._require_healthy()
        sealed = self.crypto.CryptProtectData(canonical(self.data),
            "Syn host root custody", None, None, None, 1)
        # Never truncate an existing vault in place. DPAPI protects data and
        # integrity; local Windows user ACL protects the directory.
        fd, name = tempfile.mkstemp(prefix=".vault-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(sealed)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def has_identity(self, ref):
        self._require_healthy()
        return ref in self.data["identities"]

    def create_identity(self, ref, entropy, identity):
        self._require_healthy()
        if ref in self.data["identities"] or len(entropy) != 32:
            raise ValueError("Host root identity already exists or is invalid")
        self.data["identities"][ref] = {"entropy": entropy.hex(), "public": identity}
        self._persist()

    def identity(self, ref):
        self._require_healthy()
        value = self.data["identities"].get(ref)
        if value is None:
            raise ValueError("Host Operator identity does not exist; explicit recovery is required")
        entropy = bytes.fromhex(value["entropy"])
        current = public_identity(entropy)
        if current != value["public"]:
            raise ValueError("Host identity does not match sealed manifest")
        return entropy, current

    def _require_healthy(self):
        if getattr(self, "_closed", False):
            raise OSError("Host vault is closed")
        if getattr(self, "_write_failed", False):
            raise OSError("Host vault write failed; restart the broker before further use")

    def get_slot(self, service, slot):
        self._require_healthy()
        return self.data["slots"].get(service + "/" + slot)

    def set_slot(self, service, slot, value):
        self._require_healthy()
        previous = self.data
        self.data = {**previous, "slots": {**previous["slots"], service + "/" + slot: value}}
        try:
            self._persist()
        except BaseException:
            # Never let a failed DPAPI/disk write appear acknowledged on retry.
            self.data = previous
            self._write_failed = True
            raise

    def _group_label(self, ref, network, group, epoch):
        return canonical([ref, network, group, epoch]).decode("ascii")

    def is_approved(self, ref, network, group, epoch):
        self._require_healthy()
        return self._group_label(ref, network, group, epoch) in self.data["approvals"]

    def approve_group(self, ref, network, group, epoch):
        self._require_healthy()
        label = self._group_label(ref, network, group, epoch)
        if label not in self.data["approvals"]:
            self.data["approvals"].append(label)
            self._persist()


def host_approve(question):
    from ctypes import windll
    # User approval is *Windows-native*; a WSL terminal answer is not enough
    # to provision a root authority or authorize an epoch-key transfer.
    return windll.user32.MessageBoxW(None, question,
        "Syn Windows Host Custody", 0x04 | 0x30) == 6


def pipe_security():
    import win32api
    import win32security
    import pywintypes
    import ntsecuritycon
    # Do not use the default named-pipe DACL (can grant Everyone read).
    sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION,
        ntsecuritycon.GENERIC_READ | ntsecuritycon.GENERIC_WRITE, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attrs = pywintypes.SECURITY_ATTRIBUTES()
    attrs.SECURITY_DESCRIPTOR = descriptor
    return attrs


def handle_stream(handle, authority):
    import win32file
    data = bytearray()
    while b"\n" not in data:
        _, chunk = win32file.ReadFile(handle, 4096)
        data.extend(chunk)
        if len(data) > MAX_REQUEST:
            raise ValueError("Host broker request exceeds maximum length")
        if not chunk:
            raise ValueError("Host broker peer closed before its request")
    if any(v not in (10, 13, 32) for v in data[data.index(10) + 1:]):
        raise ValueError("Trailing bytes are forbidden")
    request = json.loads(bytes(data[:data.index(10)]))
    try:
        response = {"ok": True, "result": authority.request(request)}
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        response = {"ok": False, "error": str(error)[:200]}
    win32file.WriteFile(handle, canonical(response) + b"\n")


def serve(pipe, directory=None):
    if os.name != "nt" or not pipe.startswith("SynHostCustody-") or len(pipe) > 90:
        raise ValueError("Host broker requires a bounded Windows-local pipe name")
    with WindowsVault(directory) as vault:
        _serve_pipe(pipe, Authority(vault, approve=host_approve))


def _serve_pipe(pipe, authority):
    import pywintypes
    import win32pipe
    import win32file
    import winerror
    address = "\\\\.\\pipe\\" + pipe
    attrs = pipe_security()
    while True:
        handle = win32pipe.CreateNamedPipe(address, win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | 0x8,
            1, MAX_REQUEST, MAX_REQUEST, 0, attrs)
        try:
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as error:
                if error.winerror != winerror.ERROR_PIPE_CONNECTED:
                    raise
            try:
                handle_stream(handle, authority)
            except (ValueError, OSError, EOFError, BrokenPipeError):
                # A malformed/unauthorized client cannot terminate the broker.
                pass
            try:
                win32file.FlushFileBuffers(handle)
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
        finally:
            win32file.CloseHandle(handle)


def recovery_cli(action, service, account, pin):
    """Explicit Windows-console-only backup/import; no recovery phrase over IPC."""
    import re
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Root recovery requires a private interactive Windows terminal")
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise ValueError("Recovery requires an independent pinned Operator public-key hash")
    with WindowsVault() as vault:
        _recover(action, service, account, pin, Authority(vault, approve=host_approve))


def _recover(action, service, account, pin, authority):
    from mnemonic import Mnemonic
    ref = service + "/" + account
    if action == "export":
        seed, identity = authority.store.identity(ref)
        if identity["base_public_key_hash"] != pin:
            raise ValueError("Pinned Operator identity does not match the Windows vault")
        if not host_approve("Display the Syn root recovery phrase in this Windows console?"):
            raise ValueError("Root recovery phrase export declined")
        print("Syn root recovery words (keep offline; they are never transmitted to WSL):")
        print(Mnemonic("english").to_mnemonic(seed))
        return
    if authority.store.has_identity(ref):
        raise ValueError("Windows host Operator identity already exists; refusing replacement")
    phrase = getpass.getpass("Enter existing 24-word Syn recovery phrase: ")
    codec = Mnemonic("english")
    if len(phrase.split()) != 24 or not codec.check(phrase):
        raise ValueError("Invalid 24-word Syn recovery phrase")
    entropy = bytes(codec.to_entropy(phrase))
    identity = public_identity(entropy)
    if identity["base_public_key_hash"] != pin:
        raise ValueError("Recovery phrase does not match the pinned Operator")
    if not host_approve("Import the verified Syn root identity into Windows DPAPI custody?"):
        raise ValueError("Windows root import declined")
    authority.store.create_identity(ref, entropy, identity)
    print("Root identity imported and independently pinned. Legacy operational state")
    print("still needs an explicit migration; existing WSL keystore was not modified.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    methods = parser.add_mutually_exclusive_group(required=True)
    methods.add_argument("--pipe")
    methods.add_argument("--export-recovery", action="store_true")
    methods.add_argument("--import-recovery", action="store_true")
    parser.add_argument("--service", default="syn.operator")
    parser.add_argument("--account")
    parser.add_argument("--expected-base-hash")
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--owner-created", type=int, help="Launcher's exact Windows creation FILETIME")
    args = parser.parse_args(argv)
    if (args.owner_pid is None) != (args.owner_created is None):
        parser.error("--owner-pid and --owner-created must be provided together")
    if args.owner_pid is not None and not args.pipe:
        parser.error("launcher ownership applies only to --pipe")
    if args.owner_pid is not None and (not 1 <= args.owner_pid <= 0xFFFFFFFF
                                      or not 1 <= args.owner_created < 2**64):
        parser.error("launcher PID and creation FILETIME must be positive and bounded")
    from .owner import OwnerProcess, OwnerProcessError
    try:
        if args.pipe:
            owner = OwnerProcess(args.owner_pid, args.owner_created) if args.owner_pid is not None else nullcontext()
            # Verify/watch the launcher before opening the authority's vault.
            with owner:
                serve(args.pipe)
        else:
            if not args.account or not args.expected_base_hash:
                parser.error("recovery requires --account and --expected-base-hash")
            recovery_cli("export" if args.export_recovery else "import", args.service,
                         args.account, args.expected_base_hash)
    except VaultInUseError as error:
        print(f"Syn host custody: {error}\n"
              "Inspect: .\\scripts\\Stop-SynHostCustody.ps1\n"
              "Stop only a broker whose Syn launcher is no longer needed, using -ProcessId.",
              file=sys.stderr)
        return 3
    except OwnerProcessError as error:
        print(f"Syn host custody: {error}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

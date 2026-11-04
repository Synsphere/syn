"""Bounded pending LAN pledges using the native operator's anchored state protocol."""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager

from syn.agent.lan_enrollment import _checked_submission, offer_digest
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes

from .adoption_state import EnrollmentJournal
from .custody import _agent_key_lock
from .enrollment import _reserve_locked
from .service import OperatorServer
from .state import OperatorState

PROFILE = "syn.lan-pending-inbox/1"
MAX_PENDING = 8
MAX_REPLAY_RECORDS = 128
MAX_PACKET_BYTES = 16_384


class LanInbox(OperatorState):
    def __init__(self, server):
        super().__init__(
            server.directory, server.signer.service, server.signer.account, server.expected_hash
        )
        self.path = server.directory / "lan-pledges.json"
        self.slot = server.signer.account + ".operator.lan-inbox.v1"

    def create(self):
        self._create(
            {"profile": PROFILE, "base_hash": self.base_hash, "observed_at": 0, "records": []}
        )

    def _validate(self, body):
        body = exact(body, {"profile", "base_hash", "observed_at", "records"}, "LAN pending inbox")
        records = body["records"]
        if (
            body["profile"] != PROFILE
            or body["base_hash"] != self.base_hash
            or type(body["observed_at"]) is not int
            or not 0 <= body["observed_at"] < 2**63
            or not isinstance(records, list)
            or len(records) > MAX_REPLAY_RECORDS
        ):
            raise SynProtocolError("Invalid bounded LAN pending inbox")
        offers, devices = set(), set()
        pending = 0
        for record in records:
            record = exact(
                record, {"offer", "device", "submission", "expires_at", "packet"}, "LAN inbox entry"
            )
            for key in ("offer", "device", "submission"):
                hex_bytes(record[key], 32, "LAN inbox digest")
            if (
                record["offer"] in offers
                or record["device"] in devices
                or type(record["expires_at"]) is not int
                or not 0 < record["expires_at"] < 2**63
            ):
                raise SynProtocolError("Invalid or duplicate LAN pending record")
            offers.add(record["offer"])
            devices.add(record["device"])
            packet = record["packet"]
            if packet is not None:
                if (
                    not isinstance(packet, dict)
                    or len(canonical(packet)) > MAX_PACKET_BYTES
                    or _digest(packet) != record["submission"]
                    or record["expires_at"] < body["observed_at"]
                ):
                    raise SynProtocolError("LAN pending payload differs from its protected record")
                pending += 1
        if pending > MAX_PENDING:
            raise SynProtocolError("LAN pending capacity reached")

    def _save(self, state):
        self._validate(state)
        self._commit(state)

    def refresh(self, now):
        state = self.read(recover=True)
        if now < state["observed_at"]:
            raise SynProtocolError("LAN inbox clock moved backwards")
        records = [
            {**record, "packet": None} if record["expires_at"] < now else record
            for record in state["records"]
        ]
        if records != state["records"]:
            state = {**state, "records": records, "observed_at": now}
            self._save(state)
        return state

    def add(self, packet, *, offer, device, expires_at, now):
        if len(canonical(packet)) > MAX_PACKET_BYTES or expires_at < now:
            raise SynProtocolError("LAN pending payload exceeds its size or time limit")
        state = self.refresh(now)
        submission = _digest(packet)
        for record in state["records"]:
            if record["offer"] == offer or record["device"] == device:
                if record["packet"] is not None and record["submission"] == submission:
                    return
                raise SynProtocolError("LAN pledge or device already has a protected record")
        record = {
            "offer": offer,
            "device": device,
            "submission": submission,
            "expires_at": expires_at,
            "packet": packet,
        }
        self._save({**state, "observed_at": now, "records": [*state["records"], record]})


def _digest(packet):
    return hashlib.sha256(canonical(packet)).hexdigest()


@contextmanager
def _locked(config_path):
    server = OperatorServer(config_path)
    with _agent_key_lock(
        server.directory, server.signer.service, server.signer.account + ".operator"
    ):
        yield server, LanInbox(server)


def initialize(config_path):
    with _locked(config_path) as (server, inbox):
        server.state.read()
        EnrollmentJournal(
            server.directory, server.signer.service, server.signer.account, server.expected_hash
        ).read()
        inbox.create()
    return {"profile": PROFILE, "initialized": True, "agent_activation_authorized": False}


def intake(config_path, packet, *, host, port):
    with _locked(config_path) as (server, inbox):
        return _intake_locked(server, inbox, packet, host=host, port=port)


def _intake_locked(server, inbox, packet, *, host, port):
    now = int(time.time())
    pulse, pledge, authority = _checked_submission(server, packet, host=host, port=port, now=now)
    device = server.signer.device_commitment(bytes.fromhex(pledge["offer"]["hardware_unique_id"]))
    offer = offer_digest(pledge)
    if any(row["device_commitment"] == device for row in authority["reservations"]):
        raise SynProtocolError("LAN pledge device already has an operator allocation")
    inbox.add(packet, offer=offer, device=device, expires_at=pulse["pulse"]["expires_at"], now=now)
    return {
        "profile": "syn.lan-pledge-pending/1",
        "pledge_offer_sha256": offer,
        "pending_persisted": True,
        "agent_activation_authorized": False,
    }


def _selected(server, inbox, offer, now):
    # Resolve interrupted canonical writes before comparing the retained packet.
    server.state.read(recover=True)
    journal = EnrollmentJournal(
        server.directory, server.signer.service, server.signer.account, server.expected_hash
    )
    journal.read(recover=True)
    for row in inbox.refresh(now)["records"]:
        if row["offer"] != offer or row["packet"] is None:
            continue
        packet = row["packet"]
        endpoint = packet["route"]["advertisement"]["endpoint"]
        pulse, pledge, _ = _checked_submission(
            server, packet, host=endpoint["host"], port=endpoint["port"], now=now
        )
        device = server.signer.device_commitment(bytes.fromhex(pledge["offer"]["hardware_unique_id"]))
        if (offer_digest(pledge), device, pulse["pulse"]["expires_at"]) != (
            offer, row["device"], row["expires_at"]
        ):
            raise SynProtocolError("Selected LAN pledge differs from its protected inbox record")
        return journal, pulse, pledge
    raise SynProtocolError("No unexpired LAN pledge matches the selected digest")


def reserve(config_path, *, offer, operator_byte, approve):
    """Offer exactly one retained, validated LAN pledge for explicit reservation."""
    hex_bytes(offer, 32, "LAN pending offer")
    with _locked(config_path) as (server, inbox):
        _, pulse, pledge = _selected(server, inbox, offer, int(time.time()))
        return _reserve_locked(server, pulse, pledge, operator_byte, approve=approve, recover_exact=True)


def grant(config_path, request, *, offer, approve):
    """Continue the selected LAN pledge through the separate adoption approval."""
    from .adoption_cli import _grant_locked

    hex_bytes(offer, 32, "LAN pending offer")
    request = exact(request, {"candidates", "selection"}, "LAN selected enrollment")
    with _locked(config_path) as (server, inbox):
        now = int(time.time())
        journal, pulse, pledge = _selected(server, inbox, offer, now)
        return _grant_locked(server, journal, pulse, pledge, request, now=now, approve=approve)


def pending(config_path, *, offer=None):
    if offer is not None:
        hex_bytes(offer, 32, "LAN pending offer")
    with _locked(config_path) as (_, inbox):
        records = [
            row for row in inbox.refresh(int(time.time()))["records"] if row["packet"] is not None
        ]
        if offer is None:
            return {
                "profile": PROFILE,
                "pending": [
                    {"pledge_offer_sha256": row["offer"], "expires_at": row["expires_at"]}
                    for row in records
                ],
                "agent_activation_authorized": False,
            }
        for row in records:
            if row["offer"] == offer:
                return row["packet"]
    raise SynProtocolError("No unexpired LAN pledge matches the selected digest")

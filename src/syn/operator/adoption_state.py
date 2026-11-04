"""Canonical, OS-anchored selected-state and one-use adoption journal.

This is Syn authority state, separate from Potbot's PostgreSQL task records.
There is no import of the transitional SQLite ledger and no automatic repair.
All mutations require the enclosing operator's exclusive native authority lock.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from syn_client.adoption import verify_adoption
from syn_client.custody import verify_ed25519
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes
from .custody import _native_backend
from .enrollment import CANDIDATE_DOMAIN, _verified_offer
from .state import OperatorState

SELECTION_DOMAIN = b"syn.agent.independence.v2\0"


class EnrollmentJournal(OperatorState):
    def __init__(self, directory: Path, service: str, account: str, base_hash: str):
        super().__init__(directory, service, account, base_hash)
        self.path = directory / "adoptions.json"
        self.slot = account + ".operator.adoption-state.v1"
        self.master_hash = bytes.fromhex(base_hash)

    def create(self):
        if (self.path.exists() or self.path.is_symlink()
                or _native_backend().get_password(self.service, self.slot) is not None):
            raise SynProtocolError("Adoption journal exists or is incomplete")
        body = {"profile": "syn.adoption-state/1", "base_hash": self.base_hash, "events": []}
        anchor = {"profile": "syn.operator-state-anchor/1", "path": str(self.path),
                  "base_hash": self.base_hash, "current": None, "pending": body}
        self._store_anchor(anchor)
        self._replace(body)
        self._store_anchor({**anchor, "current": self.digest(body), "pending": None})

    def _validate(self, body):
        exact(body, {"profile", "base_hash", "events"}, "adoption journal")
        if (body["profile"] != "syn.adoption-state/1" or body["base_hash"] != self.base_hash
                or not isinstance(body["events"], list) or len(body["events"]) > 64):
            raise SynProtocolError("Invalid bounded adoption journal")
        for event in body["events"]:
            exact(event, {"kind", "offer", "body"}, "adoption event")
            hex_bytes(event["offer"], 32, "pledge digest")
            if event["kind"] not in {"selected", "granted", "sealed", "receipt", "confirmed", "groupsealed", "grouprolled"}:
                raise SynProtocolError("Unknown adoption transition")

    def commit(self, body):
        current = self.read()
        self._validate(body)
        if body["events"][:len(current["events"])] != current["events"]:
            raise SynProtocolError("Adoption events are append-only")
        if len(canonical(body)) > 48000:
            raise SynProtocolError("Adoption journal capacity reached")
        anchor = self._anchor()
        self._store_anchor({**anchor, "pending": body})
        self._replace(body)
        self._store_anchor({**anchor, "current": self.digest(body), "pending": None})

    def _events(self, offer, kind=None):
        hex_bytes(offer, 32, "pledge digest")
        return [x for x in self.read()["events"] if x["offer"] == offer
                and (kind is None or x["kind"] == kind)]

    def _append(self, kind, offer, body):
        current = self.read()
        self.commit({**current, "events": current["events"] + [
            {"kind": kind, "offer": offer, "body": body}]})

    def require_selected(self, selection):
        entries = self._events(selection["offer_sha256"], "selected")
        if len(entries) != 1 or entries[0]["body"]["commitment"] != selection:
            raise SynProtocolError("Selection has no matching canonical authorized record")

    def select(self, *, server, signed_pulse, signed_offer, signed_candidates, signed_selection, now, approve):
        _, offer_digest, _ = _verified_offer(server, signed_pulse, signed_offer, now)
        temporary_public_key = signed_offer["temporary_public_key"]
        candidate = exact(signed_candidates, {"candidates", "operator_public_key", "signature"}, "candidates")
        body = candidate["candidates"]
        # The immutable reservation binds the complete original candidate preimage.
        digest = hashlib.sha256(CANDIDATE_DOMAIN + canonical(body)).hexdigest()
        records = [x for x in server.state.read()["reservations"] if x["candidates_sha256"] == digest]
        if (len(records) != 1 or records[0]["pledge_offer_sha256"] != offer_digest
                or candidate["operator_public_key"] != server.signer.public_key.hex()):
            raise SynProtocolError("Candidates are not a canonical operator reservation")
        verify_ed25519(server.signer.public_key, hex_bytes(candidate["signature"], 64, "candidate signature"),
                       CANDIDATE_DOMAIN + canonical(body))
        packet = exact(signed_selection, {"selection", "temporary_public_key", "signature"}, "selection")
        choice = exact(packet["selection"], {"schema_version", "type_ref", "target_master_public_key_hash",
                       "inheritance", "independence", "chosen_short_public_key_hash", "candidate_offer_digest"}, "choice")
        index = choice["independence"]
        if (type(index) is not int or not 1 <= index <= 8 or type(choice["schema_version"]) is not int
                or choice["schema_version"] != 2 or choice["type_ref"] != "synny://type/syn-agent-independence-selection-v2"
                or choice["target_master_public_key_hash"] != self.base_hash
                or choice["inheritance"] != records[0]["inheritance"]
                or choice["candidate_offer_digest"] != digest
                or packet["temporary_public_key"] != temporary_public_key):
            raise SynProtocolError("Agent selection differs from reserved candidates")
        verify_ed25519(hex_bytes(temporary_public_key, 32, "temporary key"),
                       hex_bytes(packet["signature"], 64, "selection signature"), SELECTION_DOMAIN + canonical(choice))
        hashes = server.signer.derive_candidate_public_hashes(choice["inheritance"])
        if (body["short_public_key_hashes"] != [x[:3].hex() for x in hashes]
                or choice["chosen_short_public_key_hash"] != hashes[index - 1][:3].hex()):
            raise SynProtocolError("Selected public key differs from actual HD derivation")
        offer = records[0]["pledge_offer_sha256"]
        if self._events(offer):
            raise SynProtocolError("Pledge already has a selected-key transition")
        selected = {"offer_sha256": offer, "master_public_key_hash": self.base_hash,
                    "temporary_public_key": temporary_public_key, "inheritance": choice["inheritance"],
                    "independence": index, "selected_public_key_hash": hashes[index - 1].hex(),
                    "selection_sha256": hashlib.sha256(SELECTION_DOMAIN + canonical(choice)
                        + bytes.fromhex(packet["signature"])).hexdigest()}
        if approve(selected) is not True:
            raise SynProtocolError("Independent operator acceptance was not granted")
        import time
        _verified_offer(server, signed_pulse, signed_offer, int(time.time()))
        self._append("selected", offer, {"commitment": selected, "signed_candidates": candidate,
                                        "signed_selection": packet})
        return selected

    def reserve_grant(self, *, offer, grant_hash):
        hex_bytes(grant_hash, 32, "grant digest")
        if len(self._events(offer, "selected")) != 1 or self._events(offer, "granted"):
            raise SynProtocolError("Pledge has no available one-use grant")
        self._append("granted", offer, {"grant": grant_hash})

    def reserve_seal(self, *, offer, grant_hash, request_hash):
        hex_bytes(request_hash, 32, "request digest")
        granted = self._events(offer, "granted")
        if len(granted) != 1 or granted[0]["body"]["grant"] != grant_hash or self._events(offer, "sealed"):
            raise SynProtocolError("Grant is absent, different or already sealed")
        self._append("sealed", offer, {"grant": grant_hash, "request": request_hash})

    def inspect(self, offer):
        records = self._events(offer.hex(), "receipt")
        return records[0]["body"] if len(records) == 1 else None

    def publish(self, receipt, *, now):
        verify_adoption(receipt, expected_master_hash=self.master_hash, now=now)
        offer = receipt["receipt"]["offer_sha256"]
        if len(self._events(offer, "sealed")) != 1 or self._events(offer, "receipt"):
            raise SynProtocolError("Receipt is absent from its one-use sealed handoff")
        self._append("receipt", offer, receipt)

    def confirm(self, *, offer, grant_hash, receipt_hash):
        sealed = self._events(offer, "sealed")
        record = self.inspect(bytes.fromhex(offer))
        body = {"grant": grant_hash, "receipt": receipt_hash}
        if (len(sealed) != 1 or sealed[0]["body"]["grant"] != grant_hash or record is None
                or hashlib.sha256(canonical(record)).hexdigest() != receipt_hash):
            raise SynProtocolError("Confirmation differs from retained sealed handoff")
        prior = self._events(offer, "confirmed")
        if prior:
            if len(prior) != 1 or prior[0]["body"] != body:
                raise SynProtocolError("Conflicting adoption confirmation")
            return
        self._append("confirmed", offer, body)


SynHandoffLedger = EnrollmentJournal
PublicAdoptionJournal = EnrollmentJournal

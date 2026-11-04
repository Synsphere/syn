"""Exercise archival migration using the exact legacy SQLite writers."""

from __future__ import annotations

import ast
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from syn.operator.legacy_state import PLAN, archive, replay
from syn_client.adoption import verify_historical_legacy_adoption
from syn_client.operator_protocol import SynProtocolError, canonical, parse

NOW = 1_790_000_000
NETWORK = "disposable-legacy-network"


class Signer:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()

    @property
    def public_key(self):
        return self.key.public_key().public_bytes_raw()

    def sign(self, payload):
        return self.key.sign(payload)


class FixtureCredentials:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value


def digest(value):
    return hashlib.sha256(value).hexdigest()


def sha(path):
    return digest(path.read_bytes())


def cli(*args):
    return subprocess.run(
        [sys.executable, "-I", "-B", "-m", "syn.operator", "legacy-state", *map(str, args)],
        capture_output=True,
        timeout=15,
    )


def potbot_schema(root, filename, table):
    path = root / "src/potbot/tools" / filename
    matches = [
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and f"CREATE TABLE IF NOT EXISTS {table} (" in node.value
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture
def legacy(monkeypatch):
    source = os.environ.get("SYN_LEGACY_FIXTURE")
    if sys.platform not in {"linux", "darwin"} or source is None:
        pytest.skip("Requires the pinned Potbot writer checkout on a disposable native runner")
    root = Path(source).resolve(strict=True)
    monkeypatch.syspath_prepend(str(root / "syn-node/src"))
    monkeypatch.syspath_prepend(str(root / "src"))

    from potbot.domain.syn_adoption import (
        AdoptionReceipt,
        SelectedKeyProof,
        SignedAdoptionReceipt,
        SignedSelectedKeyProof,
    )
    from syn_node.inherited_adoption import SynHandoffLedger
    from syn_node.p2p_contracts import build_signed_offer, make_advert
    from syn_node.p2p_inbox import PledgeInbox
    from syn_node.protocol import SynProtocolError as OldProtocolError
    from syn_node.receipts import PublicAdoptionJournal
    from syn_node.receipts import verify_adoption as verify_legacy

    with tempfile.TemporaryDirectory(prefix="syn-legacy-migration-", dir="/tmp") as work:
        base = Path(work).resolve()
        paths = {
            role: base / f"source-{role}.sqlite"
            for role in ("receipts", "handoffs", "lan", "potbot")
        }
        operator, agent = Signer(), Signer()
        pin = hashlib.sha256(operator.public_key).digest()
        offer = hashlib.sha256(b"legacy selected pledge").digest()
        proof = SelectedKeyProof(
            offer, pin, 123, 3, agent.public_key, os.urandom(32), NOW - 1, NOW + 50
        )
        signed = SignedSelectedKeyProof(proof, agent.sign(proof.signed_bytes()))
        claim = AdoptionReceipt(
            offer, pin, 123, 3, proof.selected_public_key_hash, signed.digest, NOW
        )
        packet = SignedAdoptionReceipt(
            claim, signed, operator.public_key, operator.sign(claim.signed_bytes())
        ).to_dict()
        signed_digest = verify_legacy(packet, expected_master_hash=pin, now=NOW)

        receipts = PublicAdoptionJournal(paths["receipts"], expected_master_hash=pin)
        assert receipts.publish(packet, now=NOW) == signed_digest
        with closing(sqlite3.connect(paths["receipts"])) as db:
            original = (
                db.execute("SELECT signed_json FROM public_adoptions").fetchone()[0].encode("ascii")
            )

        handoffs = SynHandoffLedger(paths["handoffs"], operator_hash=pin)
        grant, request, other = (
            digest(value) for value in (b"legacy grant", b"legacy request", b"sealed only")
        )
        handoffs.reserve_grant(offer=offer.hex(), grant_hash=grant)
        handoffs.reserve_seal(offer=offer.hex(), grant_hash=grant, request_hash=request)
        handoffs.confirm(offer=offer.hex(), grant_hash=grant, receipt_hash=digest(original))
        handoffs.reserve_grant(offer=other, grant_hash=grant)
        handoffs.reserve_seal(offer=other, grant_hash=grant, request_hash=request)
        with pytest.raises(OldProtocolError):
            handoffs.reserve_grant(offer=offer.hex(), grant_hash=grant)

        inbox = PledgeInbox.create(
            path=paths["lan"],
            service="syn.fixture",
            account="operator",
            master_hash=pin,
            network_id=NETWORK,
            lock_dir=base,
            backend=FixtureCredentials(),
        )

        def admit(device, when):
            advert = make_advert(
                operator, network_id=NETWORK, host="192.168.1.7", port=45121, now=when
            )
            pledge = build_signed_offer(Signer(), advert=advert, hardware_id=device, now=when)
            return inbox.admit(pledge, advert["signed_pulse"], advert=advert, now=when)

        lan_offer = admit(b"fixture-hardware-one", NOW)
        assert inbox.expire(now=NOW + 301) == 1
        with pytest.raises(OldProtocolError):
            admit(b"fixture-hardware-one", NOW + 301)
        lan_pending = admit(b"fixture-hardware-two", NOW + 301)
        with closing(sqlite3.connect(paths["lan"])) as db:
            lan_handle, nonce, ciphertext = db.execute(
                "SELECT device_handle, nonce, encrypted FROM pledge_inbox WHERE offer_sha=?",
                (lan_offer,),
            ).fetchone()
        assert nonce is None and ciphertext is None

        paths["potbot"].touch(mode=0o600)
        with closing(sqlite3.connect(paths["potbot"])) as db:
            db.executescript(potbot_schema(root, "syn_enrollment_ledger.py", "syn_agent_pledges"))
            db.executescript(potbot_schema(root, "syn_adoption_ledger.py", "syn_agent_adoptions"))
            db.execute(
                "INSERT INTO syn_agent_pledges (offer_sha256,master_hash,network_id,device_handle,"
                "pulse_challenge,temporary_public_key,created_at,expires_at,state,inheritance,independence,"
                "selected_public_key_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    offer.hex(),
                    pin.hex(),
                    NETWORK,
                    digest(b"potbot device"),
                    digest(b"pulse"),
                    agent.public_key.hex(),
                    NOW - 1,
                    NOW + 50,
                    "selected",
                    123,
                    3,
                    proof.selected_public_key_hash.hex(),
                ),
            )
            db.execute(
                "INSERT INTO syn_agent_adoptions VALUES (?,?,?,?,?,?,?,?)",
                (
                    offer.hex(),
                    pin.hex(),
                    proof.selected_public_key_hash.hex(),
                    123,
                    3,
                    signed_digest,
                    NOW,
                    original.decode("ascii"),
                ),
            )
            db.commit()

        def make_plan(name, *, overrides=None):
            spec = {role: {"path": str(path), "sha256": sha(path)} for role, path in paths.items()}
            spec.update(overrides or {})
            plan = base / name
            plan.write_bytes(
                canonical(
                    {"profile": PLAN, "operator": pin.hex(), "network": NETWORK, "sources": spec}
                )
            )
            plan.chmod(0o600)
            return plan

        yield (
            base,
            paths,
            make_plan,
            pin,
            offer.hex(),
            original,
            lan_offer,
            lan_handle,
            lan_pending,
            other,
        )


def test_migrate_legacy_receipts_and_replay_tombstones(legacy):
    base, paths, make_plan, pin, offer, original, lan_offer, lan_handle, lan_pending, other = legacy
    before = {role: sha(path) for role, path in paths.items()}
    target = base / "archive"
    plan = make_plan("migration.json")
    result = cli("archive", "--plan", plan, "--output", target)
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["snapshots"] == before
    assert manifest["counts"] == {
        "public_adoptions": 1,
        "syn_agent_adoptions": 1,
        "potbot_pledges": 1,
        "handoffs": 2,
        "lan_pending": 1,
        "lan_tombstones": 1,
    }
    archived = {role: sha(target / f"{role}.sqlite") for role in paths}
    assert archived == before
    options = [
        "--archive",
        target,
        "--expected-operator",
        pin.hex(),
        "--expected-archive-sha256",
        manifest["archive_sha256"],
    ]
    retrieved = cli("receipt", *options, "--offer-sha256", offer)
    assert retrieved.returncode == 0 and retrieved.stdout == original
    verify_historical_legacy_adoption(parse(retrieved.stdout), expected_master_hash=pin)

    expired = cli("replay", *options, "--offer-sha256", lan_offer)
    assert expired.returncode == 0
    returned_tombstone = json.loads(expired.stdout)["records"]["lan"]
    original_tombstone = {
        "state": "expired",
        "device_handle": lan_handle,
        "created": NOW,
        "expires": NOW + 300,
    }
    assert returned_tombstone == original_tombstone
    keywords = {"operator": pin.hex(), "archive_sha256": manifest["archive_sha256"]}
    assert replay(target, offer=lan_pending, **keywords)["records"]["lan"]["state"] == "pending"
    assert replay(target, offer=other, **keywords)["records"]["handoffs"]["state"] == "sealed"
    confirmed = replay(target, offer=offer, **keywords)["records"]
    assert confirmed["handoffs"]["receipt_sha256"] == digest(original)
    assert confirmed["potbot"]["state"] == "selected"
    assert cli("archive", "--plan", plan, "--output", target).returncode == 2
    assert cli("receipt", *options[:-1], "0" * 64, "--offer-sha256", offer).returncode == 2

    archived_db = target / "lan.sqlite"
    archived_db.chmod(0o600)
    with closing(sqlite3.connect(archived_db)) as db:
        db.execute("DELETE FROM pledge_inbox WHERE offer_sha=?", (lan_offer,))
        db.commit()
    assert cli("replay", *options, "--offer-sha256", lan_offer).returncode == 2
    assert {role: sha(path) for role, path in paths.items()} == before
    evidence = os.environ.get("SYN_LEGACY_EVIDENCE")
    if evidence:
        Path(evidence).write_bytes(
            canonical(
                {
                    "profile": "syn.official-legacy-archive-evidence/1",
                    "source_sha256": before,
                    "archive_snapshot_sha256": archived,
                    "original_receipt_sha256": digest(original),
                    "returned_receipt_sha256": digest(retrieved.stdout),
                    "original_tombstone_sha256": digest(canonical(original_tombstone)),
                    "returned_tombstone_sha256": digest(canonical(returned_tombstone)),
                    "counts": manifest["counts"],
                    "receipt_signatures_verified": True,
                    "tampered_archive_refused": True,
                }
            )
        )


def test_archive_refuses_stale_pins_active_journals_and_altered_receipts(legacy):
    base, paths, make_plan, *_ = legacy
    plan = make_plan(
        "first.json", overrides={"lan": {"path": str(paths["lan"]), "sha256": "0" * 64}}
    )
    with pytest.raises(SynProtocolError, match="pinned snapshot"):
        archive(plan, base / "wrong-pin")
    assert not (base / "wrong-pin/manifest.json").exists()

    plan = make_plan("live.json")
    sidecar = Path(str(paths["lan"]) + "-wal")
    sidecar.touch(mode=0o600)
    try:
        with pytest.raises(SynProtocolError, match="checkpoint"):
            archive(plan, base / "active")
    finally:
        sidecar.unlink()

    with closing(sqlite3.connect(paths["receipts"])) as db:
        text = db.execute("SELECT signed_json FROM public_adoptions").fetchone()[0]
        altered = json.loads(text)
        altered["operator_signature"] = "00" * 64
        db.execute(
            "UPDATE public_adoptions SET signed_json=?", (canonical(altered).decode("ascii"),)
        )
        db.commit()
    plan = make_plan("altered.json")
    with pytest.raises(SynProtocolError, match="signature"):
        archive(plan, base / "altered")
    assert not (base / "altered/manifest.json").exists()

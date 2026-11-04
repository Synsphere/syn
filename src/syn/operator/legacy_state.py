"""Offline, read-only custody of Phase 41/42/47/48 SQLite evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
import sqlite3
import stat
import sys

from syn_client.adoption import verify_historical_legacy_adoption
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse

from .custody import private_directory, private_read
from .state import sync_directory, write_new

PROFILE = "syn.legacy-state-archive/1"
PLAN = "syn.legacy-state-plan/1"
ROLES = ("receipts", "handoffs", "lan", "potbot")
MAX_BYTES = 256 * 1024 * 1024
MAX_ROWS = 65_536
PLEDGE_STATES = {"pending", "offered", "selected", "expired", "denied"}


def _digest(value):
    hex_bytes(value, 32, "legacy SHA-256")
    return value


def _network(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable():
        raise SynProtocolError("Invalid legacy network identity")
    return value


def _sidecars(path):
    if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise SynProtocolError("Stop and checkpoint the legacy SQLite writers before archiving")


def _snapshot(path, *, expected, output=None):
    """Read one closed rollback-journal database and optionally preserve its exact bytes."""
    private_directory(path.parent)
    _sidecars(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or not 100 <= before.st_size <= MAX_BYTES
        ):
            raise SynProtocolError("Legacy database must be an owner-private bounded regular file")
        header = stream.read(100)
        if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x01\x01":
            raise SynProtocolError(
                "Legacy snapshot must be a standalone SQLite rollback-journal file"
            )
        digest = hashlib.sha256(header)
        if output is not None:
            output.write(header)
        remaining = before.st_size - len(header)
        while remaining:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                raise SynProtocolError("Legacy snapshot changed during read")
            remaining -= len(chunk)
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        after = os.fstat(stream.fileno())
        if stream.read(1) or any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        ):
            raise SynProtocolError("Legacy snapshot changed during read")
    _sidecars(path)
    if digest.hexdigest() != expected:
        raise SynProtocolError("Legacy database differs from the independently pinned snapshot")


def _database(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _tables(db, required, optional=()):
    tables = {
        row["name"]: row["type"]
        for row in db.execute("PRAGMA table_list")
        if row["schema"] == "main" and not row["name"].startswith("sqlite_")
    }
    if not set(required) <= set(tables) <= set(required) | set(optional) or set(
        tables.values()
    ) != {"table"}:
        raise SynProtocolError("Unexpected legacy database tables")
    return tables


def _rows(db, sql):
    for index, row in enumerate(db.execute(sql)):
        if index >= MAX_ROWS:
            raise SynProtocolError("Legacy table exceeds the bounded migration size")
        yield row


def _unsigned(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise SynProtocolError("Invalid legacy integer")
    return value


def _receipt(row, pin, *, projection=False):
    raw = row["signed_receipt_json" if projection else "signed_json"]
    if not isinstance(raw, str) or len(raw) > 65536:
        raise SynProtocolError("Invalid legacy signed receipt storage")
    raw = raw.encode("ascii")
    packet = parse(raw)
    body = packet.get("receipt")
    if not isinstance(body, dict) or "adopted_at" not in body:
        raise SynProtocolError("Legacy receipt omits its original signing time")
    digest = verify_historical_legacy_adoption(packet, expected_master_hash=bytes.fromhex(pin))
    if (
        row["offer_sha256"] != body["offer_sha256"]
        or row["master_hash" if projection else "master_sha256"] != pin
        or row["receipt_sha256" if projection else "digest"] != digest
        or (
            projection
            and any(
                row[key] != body[key]
                for key in ("adopted_at", "inheritance", "independence", "selected_public_key_hash")
            )
        )
    ):
        raise SynProtocolError("Legacy receipt projection differs from the original signed bytes")
    return raw, body


def _validate(databases, pin, network):
    counts = {}
    receipt_hashes = {}
    for role, db in databases.items():
        if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise SynProtocolError("Legacy snapshot failed the SQLite integrity check")
        if role == "receipts":
            _tables(db, {"public_adoptions"})
            table, projection = "public_adoptions", False
        elif role == "potbot":
            tables = _tables(db, {"syn_agent_pledges"}, {"syn_agent_adoptions"})
            counts["potbot_pledges"] = _potbot_pledges(db, pin, network)
            if "syn_agent_adoptions" not in tables:
                continue
            table, projection = "syn_agent_adoptions", True
        else:
            continue
        counts[table] = 0
        seen = set()
        for row in _rows(db, f"SELECT * FROM {table}"):
            raw, body = _receipt(row, pin, projection=projection)
            offer = body["offer_sha256"]
            packet_hash = hashlib.sha256(raw).hexdigest()
            if offer in seen or receipt_hashes.setdefault(offer, packet_hash) != packet_hash:
                raise SynProtocolError("Conflicting copies of a legacy signed receipt")
            if projection:
                pledge = db.execute(
                    "SELECT * FROM syn_agent_pledges WHERE offer_sha256=?", (offer,)
                ).fetchone()
                if (
                    pledge is None
                    or pledge["state"] != "selected"
                    or body["adopted_at"] > pledge["expires_at"]
                    or any(
                        pledge[k] != body[k]
                        for k in ("inheritance", "independence", "selected_public_key_hash")
                    )
                ):
                    raise SynProtocolError("Legacy receipt does not match its retained selection")
            seen.add(offer)
            counts[table] += 1
    if "handoffs" in databases:
        db = databases["handoffs"]
        _tables(db, {"handoffs"})
        seen = set()
        for row in _rows(db, "SELECT * FROM handoffs"):
            offer = _digest(row["offer_sha256"])
            _digest(row["grant_sha256"])
            state = row["state"]
            if (
                offer in seen
                or row["master_sha256"] != pin
                or state not in {"granted", "sealed", "confirmed"}
            ):
                raise SynProtocolError("Invalid legacy handoff replay record")
            if state != "granted":
                _digest(row["request_sha256"])
            elif row["request_sha256"] is not None or offer in receipt_hashes:
                raise SynProtocolError("Unsealed legacy grant conflicts with retained evidence")
            if state == "confirmed":
                if _digest(row["receipt_sha256"]) != receipt_hashes.get(offer):
                    raise SynProtocolError(
                        "Confirmed legacy handoff needs its exact signed receipt"
                    )
            elif row["receipt_sha256"] is not None:
                raise SynProtocolError("Unconfirmed legacy handoff has a receipt commitment")
            seen.add(offer)
        counts["handoffs"] = len(seen)
    if "lan" in databases:
        counts.update(_lan_rows(databases["lan"], pin, network))
    return counts


def _potbot_pledges(db, pin, network):
    offers, devices, allocations = set(), set(), set()
    for row in _rows(
        db,
        "SELECT offer_sha256, master_hash, network_id, device_handle, state, inheritance FROM syn_agent_pledges",
    ):
        offer, device = _digest(row["offer_sha256"]), _digest(row["device_handle"])
        if (
            row["master_hash"] != pin
            or row["network_id"] != network
            or row["state"] not in PLEDGE_STATES
            or offer in offers
            or device in devices
        ):
            raise SynProtocolError("Invalid or differently scoped Potbot legacy pledge")
        if row["inheritance"] is not None:
            allocation = _unsigned(row["inheritance"], 0, 0xFFFFFF)
            if allocation in allocations:
                raise SynProtocolError("Conflicting legacy inheritance reservation")
            allocations.add(allocation)
        offers.add(offer)
        devices.add(device)
    return len(offers)


def _lan_rows(db, pin, network):
    _tables(db, {"inbox_metadata", "pledge_inbox"})
    rows = db.execute(
        "SELECT id, master, network, key_commitment FROM inbox_metadata LIMIT 2"
    ).fetchall()
    if len(rows) != 1 or tuple(rows[0])[:3] != (1, pin, network):
        raise SynProtocolError("Legacy LAN inbox belongs to another operator or network")
    _digest(rows[0]["key_commitment"])
    offers, devices, nonces = set(), set(), set()
    expired = 0
    for row in _rows(db, "SELECT * FROM pledge_inbox"):
        offer, device = _digest(row["offer_sha"]), _digest(row["device_handle"])
        created = _unsigned(row["created"], 1, 2**63 - 301)
        expiry = _unsigned(row["expires"], 1, 2**63 - 1)
        if expiry - created != 300 or offer in offers or device in devices:
            raise SynProtocolError("Invalid legacy LAN replay commitment")
        nonce, ciphertext = row["nonce"], row["encrypted"]
        if row["state"] == "expired" and nonce is None and ciphertext is None:
            expired += 1
        elif (
            row["state"] == "pending"
            and isinstance(nonce, bytes)
            and len(nonce) == 12
            and nonce not in nonces
            and isinstance(ciphertext, bytes)
            and 16 <= len(ciphertext) <= 4112
        ):
            nonces.add(nonce)
        else:
            raise SynProtocolError("Invalid legacy LAN pending or expired record")
        offers.add(offer)
        devices.add(device)
    if len(nonces) > 4096:
        raise SynProtocolError("Legacy LAN snapshot exceeds the pending quota")
    return {"lan_pending": len(nonces), "lan_tombstones": expired}


def archive(plan_path, output):
    plan = exact(
        parse(private_read(plan_path)),
        {"profile", "operator", "network", "sources"},
        "legacy migration plan",
    )
    sources = exact(plan["sources"], set(ROLES), "legacy sources")
    pin, network = _digest(plan["operator"]), _network(plan["network"])
    if plan["profile"] != PLAN or all(source is None for source in sources.values()):
        raise SynProtocolError("Select at least one explicit legacy snapshot")
    private_directory(output.parent)
    if not output.is_absolute() or output.resolve() != output:
        raise SynProtocolError("Archive directory must be a canonical absolute path")
    output.mkdir(mode=0o700)
    sync_directory(output.parent)
    files = {}
    with ExitStack() as stack:
        databases = {}
        for role, source in sources.items():
            if source is None:
                files[role] = None
                continue
            exact(source, {"path", "sha256"}, "pinned legacy snapshot")
            digest = _digest(source["sha256"])
            dest = output / (role + ".sqlite")
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                _snapshot(Path(source["path"]), expected=digest, output=stream)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
            _snapshot(dest, expected=digest)
            databases[role] = stack.enter_context(closing(_database(dest)))
            files[role] = digest
        counts = _validate(databases, pin, network)
    body = {
        "profile": PROFILE,
        "operator": pin,
        "network": network,
        "snapshots": files,
        "counts": counts,
    }
    raw = canonical(body)
    write_new(output / "manifest.json", raw)
    return {**body, "archive_sha256": hashlib.sha256(raw).hexdigest()}


@contextmanager
def _open_archive(directory, expected_pin, expected_archive):
    _digest(expected_pin)
    _digest(expected_archive)
    raw = private_read(directory / "manifest.json")
    if hashlib.sha256(raw).hexdigest() != expected_archive:
        raise SynProtocolError("Legacy archive does not match its independent pin")
    manifest = exact(
        parse(raw), {"profile", "operator", "network", "snapshots", "counts"}, "legacy archive"
    )
    if manifest["profile"] != PROFILE or manifest["operator"] != expected_pin:
        raise SynProtocolError("Legacy archive belongs to another operator")
    files = exact(manifest["snapshots"], set(ROLES), "legacy archive snapshots")
    with ExitStack() as stack:
        databases = {}
        for role, digest in files.items():
            if digest is not None:
                path = directory / (role + ".sqlite")
                _snapshot(path, expected=_digest(digest))
                databases[role] = stack.enter_context(closing(_database(path)))
        yield databases


def receipt(directory, *, operator, archive_sha256, offer):
    _digest(offer)
    with _open_archive(directory, operator, archive_sha256) as dbs:
        for role, table in (("receipts", "public_adoptions"), ("potbot", "syn_agent_adoptions")):
            if role not in dbs or (
                role == "potbot"
                and table not in _tables(dbs[role], {"syn_agent_pledges"}, {"syn_agent_adoptions"})
            ):
                continue
            row = (
                dbs[role]
                .execute(f"SELECT * FROM {table} WHERE offer_sha256=?", (offer,))
                .fetchone()
            )
            if row is not None:
                return _receipt(row, operator, projection=role == "potbot")[0]
    raise SynProtocolError("No archived signed receipt for the selected offer")


def replay(directory, *, operator, archive_sha256, offer):
    _digest(offer)
    with _open_archive(directory, operator, archive_sha256) as dbs:
        result = {}
        queries = {
            "handoffs": "SELECT state, grant_sha256, request_sha256, receipt_sha256 FROM handoffs WHERE offer_sha256=?",
            "lan": "SELECT state, device_handle, created, expires FROM pledge_inbox WHERE offer_sha=?",
            "potbot": "SELECT state, device_handle, inheritance FROM syn_agent_pledges WHERE offer_sha256=?",
        }
        for role, sql in queries.items():
            if role in dbs:
                row = dbs[role].execute(sql, (offer,)).fetchone()
                if row is not None:
                    result[role] = dict(row)
        return {"profile": PROFILE, "offer_sha256": offer, "records": result}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="syn-operator legacy-state", description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("archive")
    create.add_argument("--plan", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    for operation in ("receipt", "replay"):
        read = commands.add_parser(operation)
        read.add_argument("--archive", type=Path, required=True)
        read.add_argument("--expected-operator", required=True)
        read.add_argument("--expected-archive-sha256", required=True)
        read.add_argument("--offer-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "archive":
            print(json.dumps(archive(args.plan, args.output), sort_keys=True))
        else:
            operation = receipt if args.operation == "receipt" else replay
            result = operation(
                args.archive,
                operator=args.expected_operator,
                archive_sha256=args.expected_archive_sha256,
                offer=args.offer_sha256,
            )
            if isinstance(result, bytes):
                sys.stdout.buffer.write(result)
            else:
                print(json.dumps(result, sort_keys=True))
        return 0
    except (SynProtocolError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        print(
            "Legacy archive operation refused. Check the pinned snapshots and private paths.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare/apply a small, hash-guarded insertion to two explicit instruction files."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
TARGETS = [
    (Path(r"Z:\My Songs\.codex-relocated-home\AGENTS.md"), b"## External browser access across chats"),
    (Path(r"Z:\My Songs\AGENTS.md"), b"**Cross-chat blogworthiness reminder throttle"),
]
HEADING = b"## Search narrowly and exclude generated storage"
PLAN = HERE / "policy-change-plan.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


if "--apply" not in sys.argv:
    policy = (HERE / "bounded-search-policy.txt").read_text(encoding="utf-8").rstrip() + "\n\n"
    changes = []
    for index, (path, anchor) in enumerate(TARGETS):
        before = path.read_bytes()
        if HEADING in before:
            raise SystemExit(f"Policy already exists; inspect before updating: {path}")
        if before.count(anchor) != 1:
            raise SystemExit(f"Ambiguous insertion anchor: {path}")
        newline = "\r\n" if b"\r\n" in before else "\n"
        insertion = policy.replace("\n", newline).encode("utf-8")
        offset = before.index(anchor)
        after = before[:offset] + insertion + before[offset:]
        backup = HERE / f"instructions-before-{index}.bin"
        candidate = HERE / f"instructions-candidate-{index}.bin"
        backup.write_bytes(before)
        candidate.write_bytes(after)
        assert after.replace(insertion, b"", 1) == before
        changes.append({"path": str(path), "before_sha256": digest(before), "after_sha256": digest(after), "backup": str(backup), "candidate": str(candidate), "insertion_byte_offset": offset, "added_bytes": len(insertion)})
    PLAN.write_text(json.dumps({"owner_chat": "01a19a0f-2359-7830-8a2f-263f78c8a55a", "prepared_at": datetime.now(timezone.utc).isoformat(), "changes": changes}, indent=2), encoding="utf-8")
    print(json.dumps({"state": "prepared", "changes": [{k: v for k, v in x.items() if k not in ("backup", "candidate")} for x in changes]}))
else:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    allowed = {str(p) for p, _ in TARGETS}
    for change in plan["changes"]:
        if change["path"] not in allowed:
            raise SystemExit("Unexpected target")
        if digest(Path(change["path"]).read_bytes()) != change["before_sha256"]:
            raise SystemExit(f"Concurrent change; preserved: {change['path']}")
        if digest(Path(change["candidate"]).read_bytes()) != change["after_sha256"]:
            raise SystemExit("Candidate changed")
    receipts = []
    for change in plan["changes"]:
        path = Path(change["path"])
        candidate = Path(change["candidate"]).read_bytes()
        if digest(path.read_bytes()) != change["before_sha256"]:
            raise SystemExit(f"Concurrent change; preserved: {path}")
        path.write_bytes(candidate)
        verified = path.read_bytes()
        assert digest(verified) == change["after_sha256"]
        assert verified.count(HEADING) == 1
        receipts.append({"path": str(path), "sha256": digest(verified), "verified": True})
    (HERE / "policy-change-receipt.json").write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "changes": receipts, "running_chat_adoption": "not_established"}, indent=2), encoding="utf-8")
    print(json.dumps({"state": "applied_and_read_back", "changes": receipts}))

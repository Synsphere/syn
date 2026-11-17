"""Bounded reads of five exact incident-related sessions, never history discovery."""
import json
from pathlib import Path

HERE = Path(__file__).parent
ROOT = Path(r"C:\Users\Kieran\.codex\sessions\2026")
SPECS = {
    "01a199cc-af97-7b63-94db-5ebaee6852f8": "11/02/rollout-2026-11-02T01-08-11-01a199cc-af97-7b63-94db-5ebaee6852f8.jsonl",
    "01a199ed-fc1a-7820-8682-467c723503d7": "11/02/rollout-2026-11-02T01-44-34-01a199ed-fc1a-7820-8682-467c723503d7.jsonl",
    "01a19a0b-17bb-7820-a2a0-b8d8f6b3b751": "11/02/rollout-2026-11-02T02-16-20-01a19a0b-17bb-7820-a2a0-b8d8f6b3b751.jsonl",
    "01a19965-66ad-7c01-916c-a8429ce17b7a": "11/01/rollout-2026-11-01T23-15-22-01a19965-66ad-7c01-916c-a8429ce17b7a.jsonl",
    "01a167e4-a90e-7193-b5a3-e9736b2d6e38": "10/23/rollout-2026-10-23T09-33-21-01a167e4-a90e-7193-b5a3-e9736b2d6e38.jsonl",
}
NEEDLES = ["Search narrowly and exclude generated storage", "New narrow-search exclusions applied", ".rgignore", "01a19a0f-orphan-scan-cause"]
results = []
for identifier, relative in SPECS.items():
    path = ROOT / relative
    if not path.exists():
        results.append({"thread_id": identifier, "file": str(path), "state": "exact_source_unavailable"})
        continue
    length = path.stat().st_size
    start = max(0, length - 3 * 1024 * 1024)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(length - start)
    lines = data.splitlines()
    if start:
        lines = lines[1:]
    matches = []
    latest = None
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        latest = event.get("timestamp", latest)
        payload = event.get("payload", {})
        text = json.dumps(payload, ensure_ascii=False)
        found = [needle for needle in NEEDLES if needle in text]
        if not found:
            continue
        pos = min(text.index(needle) for needle in found)
        role = payload.get("role")
        kind = payload.get("type")
        classification = "instruction_receipt" if role == "user" and NEEDLES[0] in found else "explicit_applied_record" if NEEDLES[1] in found else "mention_or_command"
        matches.append({"at": event.get("timestamp"), "event_type": event.get("type"), "payload_type": kind, "role": role, "classification": classification, "matched_terms": found, "excerpt": text[max(0, pos - 80):pos + 320]})
    results.append({"thread_id": identifier, "file": str(path), "file_bytes": length, "tail_bytes_read": len(data), "latest_event_at": latest, "matches": matches, "instruction_receipt_seen": any(m["classification"] == "instruction_receipt" for m in matches), "explicit_applied_record_seen": any(m["classification"] == "explicit_applied_record" for m in matches)})
(HERE / "running-chat-instruction-receipts.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps([{k: v for k, v in r.items() if k not in ("file", "matches")} | {"matches": r.get("matches", [])[-2:]} for r in results]))

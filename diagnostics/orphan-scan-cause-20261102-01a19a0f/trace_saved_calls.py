"""Read only the eight known search launches from their exact saved sessions."""
import json
from pathlib import Path

BASE = Path(r"C:\Users\Kieran\OneDrive\Documents\Syn\diagnostics")
SESSIONS = Path(r"C:\Users\Kieran\.codex\sessions\2026")
SPECS = [
    (27812, "11/01/rollout-2026-11-01T23-02-45-01a19959-dc0e-7a43-a239-787d0d3d4995.jsonl", "spacesleuth|space sleuth", "2026-11-01T23:31:16"),
    (23076, "11/02/rollout-2026-11-02T00-13-51-01a1999a-f51a-7520-bc55-582d57b83f7c.jsonl", "Native 4K Diya Ring Visualiser", "2026-11-02T00:17:5"),
    (42108, "11/02/rollout-2026-11-02T00-13-51-01a1999a-f51a-7520-bc55-582d57b83f7c.jsonl", "B48Qx83ivrw", "2026-11-02T00:18:1"),
    (19424, "11/01/rollout-2026-11-01T23-15-22-01a19965-66ad-7c01-916c-a8429ce17b7a.jsonl", "FRAMERUDIMENTS_FFPROBE", "2026-11-02T00:39:1"),
    (24936, "11/02/rollout-2026-11-02T00-49-24-01a199bb-7e59-7153-b6d2-d5f51dd24242.jsonl", "RastaMoves|rastamoves", "2026-11-02T00:50:1"),
    (24848, "11/02/rollout-2026-11-02T00-13-51-01a1999a-f51a-7520-bc55-582d57b83f7c.jsonl", "The telephone queue as a medical instrument", "2026-11-02T00:57:1"),
    (16004, "11/02/rollout-2026-11-02T00-59-19-01a199c4-9593-7aa2-b271-9a9865af6f9c.jsonl", "YouTube|Search Console|TikTok", "2026-11-02T00:59:3"),
    (30136, "11/02/rollout-2026-11-02T01-47-11-01a199f0-657c-72b0-96ad-6b7dc328bb84.jsonl", "platform monitor|platform", "2026-11-02T01:47:2"),
]


def flattened(value):
    if isinstance(value, list):
        return "\n".join(flattened(v) for v in value)
    if isinstance(value, dict):
        if "text" in value:
            return flattened(value["text"])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        if isinstance(decoded, (list, dict)):
            return flattened(decoded)
        return value
    return str(value)


cache = {}
results = []
for pid, relative, needle, at in SPECS:
    path = SESSIONS / relative
    if path not in cache:
        entries = []
        with path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "response_item":
                    continue
                p = event.get("payload", {})
                if p.get("type") not in ("custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output", "message"):
                    continue
                entries.append((line_no, event))
        cache[path] = entries
    entries = cache[path]
    calls = []
    for line_no, e in entries:
        p = e["payload"]
        body = p.get("input", p.get("arguments", ""))
        if p.get("type") in ("custom_tool_call", "function_call") and needle in body and e.get("timestamp", "").startswith(at):
            call_id = p.get("call_id")
            outputs = [{"line": n, "at": ev.get("timestamp"), "text": flattened(ev["payload"].get("output", ""))[:6000]} for n, ev in entries if ev["payload"].get("call_id") == call_id and ev["payload"].get("type", "").endswith("_output")]
            subsequent = []
            for n, ev in entries:
                if n <= line_no or len(subsequent) >= 10:
                    continue
                pl = ev["payload"]
                if pl.get("type") == "message" and pl.get("role") == "assistant":
                    text = flattened(pl.get("content", []))
                    subsequent.append({"line": n, "at": ev.get("timestamp"), "kind": "assistant", "text": text[:800]})
                elif pl.get("type") in ("custom_tool_call", "function_call"):
                    subsequent.append({"line": n, "at": ev.get("timestamp"), "kind": "call", "text": pl.get("input", pl.get("arguments", ""))[:1300]})
            calls.append({"line": line_no, "at": e.get("timestamp"), "call_id": call_id, "input": body, "outputs": outputs, "subsequent": subsequent})
    results.append({"pid": pid, "file": str(path), "launches": calls})

out = Path(__file__).parent / "saved-call-evidence.json"
out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
for r in results:
    for c in r["launches"]:
        print(json.dumps({"pid": r["pid"], "file": r["file"], "line": c["line"], "at": c["at"], "input": c["input"][:3500], "outputs": c["outputs"], "subsequent": c["subsequent"][:5]}, ensure_ascii=True))

"""Prepare prompt-only changes to ACTIVE automations; never edit their TOML."""
import hashlib
import json
import re
import tomllib
from pathlib import Path

HERE = Path(__file__).parent
ROOT = Path(r"C:\Users\Kieran\.codex\automations")
QUIET = "Quiet blogworthiness record: before finishing, record Blogworthiness: PASS or FAIL with a short evidence-based reason and source evidence in the existing report or final handoff. For PASS also record the reader-interest angle, duplicate check and next action. Do not send or surface a standalone completion-check reminder."
MARKER = "Search exclusions and process ownership — 2 November 2026:"
GUARD = MARKER + " Before local discovery, read the current 'Search narrowly and exclude generated storage' section in the shared AGENTS.md and honour the owning root's .rgignore. Start with known files and the exact source/document subtree. Exclude dependency stores, virtual environments, build/output trees, caches/test temp folders, models, generated media and unrelated worktrees; avoid blanket --hidden and drive/workspace-wide recursion. Put explicit exclusions after inclusion globs. Retain session_id/exit_code; poll a yielded search or cancel its actual owned process/tree and verify exit. Do not leave discovery running after moving on. Apply at the next safe step without restarting or interrupting existing productive work. Record the actual scope/exclusions used in the normal private run receipt; a saved rule alone is not adoption evidence."
PURPOSES = {
    "daily-outreach-response-monitor": "Monitor replies to existing music outreach and reconcile verified outcomes under the retained reply, privacy and duplicate-send gates.",
    "daily-overdue-task-unblocker": "Run the existing overdue-task unblocker for My Songs using its retained task ledger, authority and completion rules.",
    "danceflow-and-potbot-plugin-discovery-research": "Run the existing DanceFlow and Potbot marketing and plugin discovery research under its retained scope, cost, privacy and publication limits.",
    "dancemoves-wordpress-review": "Continue the existing DanceMoves WordPress review work under its retained review evidence and exact-action gates.",
    "hell-gondola-daily-pre-release": "Run the existing Song marketing operations queue for Z:\\My Songs, retaining due-only claims, batch limits, campaign gates and stop conditions.",
    "verify-arcadians-blog-publication": "Verify the existing Arcadians blog publication under the retained article identity, dates, read-only acceptance and stop rules below.",
    "weekly-future-release-marketing-calendar": "Maintain the existing future-release marketing calendar under its retained timing, evidence, privacy and publication gates.",
    "weekly-release-and-epk-sync": "Perform the existing weekly release, EPK and MP3 metadata reconciliation under its retained canonical-source and public-save gates.",
}


def prepare(identifier):
    path = ROOT / identifier / "automation.toml"
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8-sig"))
    if data.get("status") != "ACTIVE":
        return {"id": identifier, "state": "preserved_nonactive"}
    original = data["prompt"]
    if MARKER in original:
        return {"id": identifier, "state": "already_present"}
    remainder = original
    if QUIET in remainder:
        remainder = remainder.replace(QUIET + "\r\n\r\n", "").replace(QUIET + "\n\n", "").replace(QUIET, "")
    if identifier in PURPOSES:
        purpose = PURPOSES[identifier]
    else:
        parts = re.split(r"\r?\n\s*\r?\n", remainder, maxsplit=1)
        purpose = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""
    prompt = purpose + "\n\n" + QUIET + "\n\n" + GUARD + "\n\n" + remainder
    args = {"mode": "update", "id": identifier, "kind": data["kind"], "name": data["name"], "prompt": prompt, "rrule": data["rrule"], "status": data["status"]}
    if "notification_policy" in data:
        args["notificationPolicy"] = data["notification_policy"]
    if data["kind"] == "heartbeat":
        args["destination"] = "thread"
        args["targetThreadId"] = data["target_thread_id"]
    else:
        args.update(destination="local", executionEnvironment=data["execution_environment"], model=data["model"], reasoningEffort=data["reasoning_effort"], projectId=data["target"]["project_id"])
    (HERE / (identifier + ".before.json")).write_text(json.dumps({"path": str(path), "file_sha256": hashlib.sha256(raw).hexdigest(), "data": data}, indent=2), encoding="utf-8")
    (HERE / (identifier + ".update.json")).write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
    return {"id": identifier, "state": "prepared", "kind": data["kind"], "prompt_chars": len(prompt), "before_sha256": hashlib.sha256(raw).hexdigest()}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = prepare(sys.argv[1])
        if "--emit" in sys.argv and result["state"] == "prepared":
            print(json.dumps(json.loads((HERE / (sys.argv[1] + ".update.json")).read_text(encoding="utf-8"))))
        else:
            print(json.dumps(result))
    else:
        before = json.loads((HERE / "automations-before.json").read_text(encoding="utf-8"))
        print(json.dumps([prepare(r["directory"]) for r in before if r["data"].get("status") == "ACTIVE"]))

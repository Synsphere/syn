"""Read back one supported API update, retaining exact prompt/control comparisons."""
import json
import sys
import tomllib
from pathlib import Path

here = Path(__file__).parent
identifier = sys.argv[1]
before = json.loads((here / (identifier + ".before.json")).read_text(encoding="utf-8"))
after = tomllib.loads(Path(before["path"]).read_text(encoding="utf-8-sig"))
expected = json.loads((here / (identifier + ".update.json")).read_text(encoding="utf-8"))
diff = sorted(k for k in set(before["data"]) | set(after) if before["data"].get(k) != after.get(k))
result = {"id": identifier, "changed_fields": diff, "prompt_exact": after.get("prompt") == expected["prompt"], "preserved_controls": set(diff).issubset({"prompt", "updated_at"}), "status": after.get("status"), "target_thread_id": after.get("target_thread_id"), "exclusions_present": "Search exclusions and process ownership" in after["prompt"], "completion_rule_present": "Quiet blogworthiness record:" in after["prompt"]}
(here / (identifier + ".verification.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
if not result["prompt_exact"] or not result["preserved_controls"]:
    raise SystemExit(2)

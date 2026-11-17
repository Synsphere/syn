"""Verify exclusions against tiny local fixtures, never against Z: storage."""
import json
import shutil
import subprocess
from pathlib import Path

here = Path(__file__).parent
fixture = here / "filter-fixture"
fixture.mkdir(exist_ok=True)
shutil.copyfile(here / "generated-storage.rgignore", fixture / ".rgignore")
with (fixture / ".rgignore").open("a", encoding="utf-8") as output:
    output.write("\n*.generated.py\n")
files = ["src/keep.py", "docs/keep.md", "nested/project/src/keep.py"]
excluded = ["node_modules/package/hide.py", ".pnpm-store/hide.py", ".uv-cache/hide.py", ".venv/Lib/hide.py", "nested/project/build/hide.py", "dist/hide.py", "models/hide.py", ".tmp/hide.py", ".codex-relocated-home/sessions/hide.py", "src/skip.generated.py"]
for name in files + excluded:
    path = fixture / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("search_probe\n", encoding="utf-8")
rg = shutil.which("rg")
def run(args):
    p = subprocess.run([rg, *args], cwd=fixture, capture_output=True, text=True, timeout=5, check=True)
    return {x.replace("\\", "/").removeprefix("./") for x in p.stdout.splitlines()}
default = run(["--files", "--hidden"])
assert set(files).issubset(default)
assert not set(excluded).intersection(default)
positive = run(["--files", "--hidden", "-g", "*.py"])
assert "src/skip.generated.py" in positive
assert "nested/project/build/hide.py" not in positive
negative = run(["--files", "--hidden", "-g", "*.py", "-g", "!**/build/**", "-g", "!**/node_modules/**", "-g", "!**/models/**", "-g", "!**/*.generated.py"])
assert "nested/project/build/hide.py" not in negative
assert "src/skip.generated.py" not in negative
assert "src/keep.py" in negative
result = {"fixture": str(fixture), "fixture_only": True, "default_exclusions_pass": True, "source_and_docs_retained": True, "positive_glob_override_demonstrated": True, "later_exclusion_glob_pass": True, "z_drive_scan_started": False}
(here / "filter-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))

"""Review/default-filter extension using only two exact files and tiny C: fixtures."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
PREVIOUS = HERE.parent / "orphan-scan-cause-20261102-01a19a0f"
TARGETS = [Path(r"Z:\My Songs\.rgignore"), Path(r"C:\Users\Kieran\OneDrive\Documents\Syn\.rgignore")]
EXTRA = b"\n# Named temporary environments/build trees used by current work.\n.build-tmp/\n.build-temp/\n.smoke-venv/\n.build-venv/\n.test-venv/\nvenv-*/\n*-test-temp/\n*-test-temp-*/\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


if "--apply" in sys.argv:
    plan = json.loads((HERE / "filter-extension-plan.json").read_text(encoding="utf-8"))
    for item in plan["changes"]:
        assert item["path"] in {str(p) for p in TARGETS}
        assert sha(Path(item["path"]).read_bytes()) == item["before_sha256"], "Concurrent filter change preserved"
    receipts = []
    for item in plan["changes"]:
        target = Path(item["path"])
        data = Path(item["candidate"]).read_bytes()
        assert sha(data) == item["after_sha256"]
        target.write_bytes(data)
        assert target.read_bytes() == data
        receipts.append({"path": str(target), "sha256": sha(data), "readback_verified": True})
    (HERE / "filter-extension-receipt.json").write_text(json.dumps(receipts, indent=2), encoding="utf-8")
    print(json.dumps({"state": "applied_and_read_back", "files": receipts}))
    raise SystemExit

changes = []
for i, target in enumerate(TARGETS):
    before = target.read_bytes()
    expected = json.loads((PREVIOUS / "exclusion-install-receipt.json").read_text(encoding="utf-8"))["files"][i]["sha256"]
    assert sha(before) == expected, "Concurrent filter change preserved"
    candidate = before.rstrip() + b"\n" + EXTRA
    backup_path = HERE / f"rgignore-before-{i}.bin"
    candidate_path = HERE / f"rgignore-candidate-{i}.bin"
    backup_path.write_bytes(before)
    candidate_path.write_bytes(candidate)
    changes.append({"path": str(target), "before_sha256": sha(before), "after_sha256": sha(candidate), "backup": str(backup_path), "candidate": str(candidate_path)})

fixture = HERE / "inheritance-fixture"
fixture.mkdir(exist_ok=True)
(fixture / ".rgignore").write_bytes(candidate)
project = fixture / "owning-project"
project.mkdir(exist_ok=True)
(project / ".git").mkdir(exist_ok=True)
keep = ["src/source.py", "docs/readme.md", "tests/test_source.py"]
skip = [".build-tmp/pkg/file.py", ".smoke-venv/Lib/site-packages/file.py", "nested/node_modules/pkg/file.py", "build/file.py", "models/file.py", "some-test-temp/work/file.py"]
for name in keep + skip:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("source_probe\n", encoding="utf-8")
def discovered(args):
    proc = subprocess.run([shutil.which("rg"), "--files", "--hidden", *args], cwd=project, capture_output=True, text=True, check=True, timeout=5)
    return {p.replace("\\", "/") for p in proc.stdout.splitlines()}
ordinary = discovered([])
positive_glob = discovered(["-g", "*.py"])
assert set(keep).issubset(ordinary)
assert not set(skip).intersection(ordinary)
assert "src/source.py" in positive_glob
assert not set(skip).intersection(positive_glob)
verification = {"fixture_only": True, "parent_rgignore_inherited_inside_nested_git_boundary": True, "sources_docs_tests_retained": True, "temporary_venv_builds_excluded": True, "positive_file_glob_did_not_reopen_excluded_directories": True, "no_z_drive_scan": True}
(HERE / "filter-inheritance-verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
(HERE / "filter-extension-plan.json").write_text(json.dumps({"changes": changes}, indent=2), encoding="utf-8")
print(json.dumps({"state": "prepared", "verification": verification, "changes": [{k: v for k, v in x.items() if k not in ("candidate", "backup")} for x in changes]}))

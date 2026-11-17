"""Install the reviewed default exclusions in two explicit roots, without replacing files."""
import hashlib
import json
from pathlib import Path

here = Path(__file__).parent
data = (here / "generated-storage.rgignore").read_bytes()
targets = [Path(r"Z:\My Songs\.rgignore"), Path(r"C:\Users\Kieran\OneDrive\Documents\Syn\.rgignore")]
for target in targets:
    if target.exists():
        raise SystemExit(f"Existing file preserved; reconcile explicitly: {target}")
receipts = []
for target in targets:
    with target.open("xb") as output:
        output.write(data)
    readback = target.read_bytes()
    assert readback == data
    receipts.append({"path": str(target), "sha256": hashlib.sha256(readback).hexdigest(), "readback_verified": True})
(here / "exclusion-install-receipt.json").write_text(json.dumps({"files": receipts, "limits": ["Positive rg inclusion globs and directly named paths can override ignores", "PowerShell recursion does not use rgignore", "Not evidence of adoption by an already-running command"]}, indent=2), encoding="utf-8")
print(json.dumps(receipts))

"""Compare Python HD arithmetic/signing against pinned independent Rust/WASM."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from syn.operator.hd_keys import _derive, _pycardano


def public_vector():
    # Fixed public test entropy, never an installation or release-authority key.
    values = _derive(bytes(range(32)))
    wallet_cls, extended_cls = _pycardano()
    wallet = wallet_cls.from_entropy(bytes(range(32)).hex(), passphrase="")
    candidates = {}
    for inheritance in (0, 172941, 16777215):
        candidates[str(inheritance)] = [hashlib.sha256(bytes(wallet.derive_from_path(f"m/6969'/420'/{inheritance}'/{i}/0").public_key)).hexdigest() for i in range(1, 9)]
    signer = extended_cls(values["base_xprivate"] + values["base_public"] + values["base_chain"])
    actual = {"profile": "syn.hd-independent-reference/1", "root_public": values["root_public"].hex(),
              "base_public": values["base_public"].hex(), "root_hash": hashlib.sha256(values["root_public"]).hexdigest(),
              "base_hash": hashlib.sha256(values["base_public"]).hexdigest(),
              "signature": signer.sign(b"Syn independent Icarus operator reference v1").hex(), "candidate_hashes": candidates}
    return actual


def main():
    root = Path(__file__).resolve().parents[1]
    expected = json.loads(subprocess.check_output(["node", str(root / "tests/reference/hd/reference.cjs")], timeout=30))
    actual = public_vector()
    assert actual == expected, "Python HD provider differs from the independent Rust/WASM reference"
    output = root / "artifacts/operator/hd-reference.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"result": "passed", "reference_package": "@emurgo/cardano-serialization-lib-nodejs@15.0.3", "public_vector": actual}, sort_keys=True) + "\n")
    print(json.dumps({"result": "passed", "independent_hd_reference": True, "candidate_vectors": 24}))


if __name__ == "__main__":
    main()

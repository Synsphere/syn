"""Independent public Operator DAG proof verification; no server/keys required."""
from __future__ import annotations
import hashlib
import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src/syn/operator/dag_proofs.py"
spec = importlib.util.spec_from_file_location("syn_dag_proofs_test", SOURCE)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def leaves(count):
    return [hashlib.sha256(str(i).encode()).hexdigest() for i in range(count)]


def test_empty_root_matches_existing_dag():
    assert proof.root([]) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 31, 32, 33, 63, 64, 65])
def test_all_inclusions_verify(size):
    data = leaves(size)
    for index in range(size):
        path = proof.inclusion(data, index)
        assert proof.verify_inclusion(data[index], index, size, proof.root(data), path)
        assert len(path) <= 7
        wrong = leaves(size+1)[-1]
        assert not proof.verify_inclusion(wrong, index, size, proof.root(data), path)
        if path:
            assert not proof.verify_inclusion(data[index], index, size, proof.root(data), path[:-1])


@pytest.mark.parametrize("size", [2, 3, 4, 5, 8, 9, 16, 17, 32, 33, 65])
def test_all_consistency_proofs_verify(size):
    data = leaves(size)
    for old in range(1, size+1):
        path = proof.consistency(data, old)
        assert proof.verify_consistency(old, size, proof.root(data[:old]), proof.root(data), path)
        if old != size:
            assert not proof.verify_consistency(old, size, proof.root(data[:old]), proof.root(data), path[:-1])
            assert not proof.verify_consistency(old, size, proof.root(data[:old]), "ff" * 32, path)


def test_wrong_tree_and_malformed_inputs_rejected():
    with pytest.raises(ValueError):
        proof.inclusion(leaves(3), -1)
    with pytest.raises(ValueError):
        proof.consistency(leaves(3), 0)
    with pytest.raises(ValueError):
        proof.verify_consistency(4, 3, '00'*32, 'ff'*32, [])
    with pytest.raises(ValueError):
        proof.verify_inclusion('AA'*32, 0, 1, 'ff'*32, [])
    with pytest.raises(ValueError):
        proof.root(['ab'])

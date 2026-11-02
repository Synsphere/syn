"""Synthetic release-authority fixtures; no production key or package download."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from syn_client.consensus import canonical_bytes
from syn_client.releases import (
    MANIFEST_PROFILE, REPOSITORY, SIGNATURE_DOMAIN, SIGNATURE_PROFILE,
    ReleaseArtifact, ReleaseError, ReleaseManifest, ReleasePin, RuntimeContract,
    artifact_names, check_compatibility, verify_artifact, verify_release,
)

# Public RFC 8032 test seed, never a production release authority.
KEY = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
))
PUBLIC = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def contract():
    return RuntimeContract(
        "0.1.0", "0.1.0", ("3.12", "3.13"), ("linux-x86_64",),
        ("linux-x86_64", "windows-x86_64"), ("service",),
        ("syn.communication/1", "syn.consensus-policy/1"),
        ("http-workspace-messages", "offline-type-policy-resolution"), "20261024_0015",
    )


def manifest():
    return ReleaseManifest("0.1.0", "a" * 40, contract(), tuple(
        ReleaseArtifact(role, name, hashlib.sha256(name.encode()).hexdigest(), len(name))
        for role, name in sorted(artifact_names("0.1.0").items())
    ))


def pin(item):
    return ReleasePin("0.1.0", "a" * 40, item.digest, hashlib.sha256(PUBLIC).hexdigest())


def signature(item, *, prefix=SIGNATURE_DOMAIN):
    return canonical_bytes({
        "profile": SIGNATURE_PROFILE, "manifest_sha256": item.digest,
        "release_key_sha256": hashlib.sha256(PUBLIC).hexdigest(),
        "signature": KEY.sign(prefix + item.canonical).hex(),
    })


def compatible(item, **changes):
    options = dict(component="runtime", platform="linux-x86_64", python_version="3.13",
                   required_profiles=("syn.communication/1",),
                   required_capabilities=("http-workspace-messages",), required_mode="service")
    options.update(changes)
    return check_compatibility(item, item.runtime, **options)


def test_release_round_trip_has_fixed_identity_and_immutable_nested_contracts():
    item = manifest()
    assert item.to_dict()["profile"] == MANIFEST_PROFILE
    assert item.to_dict()["repository"] == REPOSITORY
    assert item.to_dict()["tag"] == "v0.1.0"
    assert ReleaseManifest.from_bytes(item.canonical) == item
    assert RuntimeContract.from_dict(item.runtime.to_dict()) == item.runtime
    with pytest.raises(FrozenInstanceError):
        item.version = "9.0.0"
    exported = item.to_dict()
    exported["runtime"]["node_modes"].append("agent")
    assert item.runtime.node_modes == ("service",)
    assert "agent" not in item.to_dict()["runtime"]["node_modes"]


def test_release_signature_uses_independent_authority_and_exact_file_digest():
    item = manifest()
    assert PUBLIC.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    assert item.digest == hashlib.sha256(item.canonical).hexdigest()
    assert verify_release(item.canonical, signature(item), pin(item), PUBLIC) == item


def test_frozen_interoperability_vector_matches_exact_bytes_digests_and_signature():
    vector = json.loads((Path(__file__).parent / "fixtures/release-v1.json").read_text(encoding="utf-8"))
    item = manifest()
    assert item.to_dict() == vector["manifest"]
    assert item.canonical == vector["canonical_manifest_utf8"].encode()
    assert item.digest == vector["manifest_sha256"]
    assert PUBLIC.hex() == vector["public_key"]
    assert signature(item) == canonical_bytes(vector["signature_envelope"])
    pinned = ReleasePin(item.version, item.source_commit, vector["manifest_sha256"], vector["release_key_sha256"])
    assert verify_release(item.canonical, signature(item), pinned, bytes.fromhex(vector["public_key"])) == item


@pytest.mark.parametrize("prefix", [b"", b"syn-agent-proof-v1\0", b"syn.governed-policy-record/2\0"])
def test_other_signature_domains_cannot_authorize_release(prefix):
    item = manifest()
    with pytest.raises(ReleaseError, match="signature verification"):
        verify_release(item.canonical, signature(item, prefix=prefix), pin(item), PUBLIC)


@pytest.mark.parametrize("field,value", [
    ("version", "0.2.0"), ("source_commit", "b" * 40),
    ("manifest_sha256", "b" * 64), ("release_key_sha256", "b" * 64),
])
def test_downloaded_manifest_cannot_replace_local_pins(field, value):
    item = manifest()
    with pytest.raises(ReleaseError, match="independently selected"):
        verify_release(item.canonical, signature(item), replace(pin(item), **{field: value}), PUBLIC)


@pytest.mark.parametrize("change", [
    {"profile": "syn.release-signature/2"}, {"signature": "00" * 64},
    {"signature": "a" * 127}, {"manifest_sha256": "b" * 64},
    {"release_key_sha256": "b" * 64}, {"public_key": PUBLIC.hex()},
])
def test_untrusted_or_invalid_signature_envelopes_hold(change):
    item = manifest()
    envelope = json.loads(signature(item))
    envelope.update(change)
    with pytest.raises(ReleaseError):
        verify_release(item.canonical, canonical_bytes(envelope), pin(item), PUBLIC)


def test_artifact_bytes_are_verified_before_any_installer_can_use_them():
    item = manifest().artifacts[0]
    verify_artifact(item, item.filename.encode())
    with pytest.raises(ReleaseError):
        verify_artifact(item, b"changed")
    with pytest.raises(ReleaseError):
        verify_artifact(item, bytearray(item.filename.encode()))


@pytest.mark.parametrize("version", ["latest", "v0.1.0", "01.1.0", "0.1", "0.1.0+local", "0.1.0.dev1", "0.1.0rc0", "0.1.0/../../x"])
def test_release_versions_are_exact_and_never_select_floating_or_local_source(version):
    with pytest.raises(ReleaseError):
        artifact_names(version)


def test_preview_release_has_exact_python_distribution_version_and_tag():
    names = artifact_names("0.1.1rc1")
    assert names["runtime-wheel"] == "syn-0.1.1rc1-py3-none-any.whl"


@pytest.mark.parametrize("change", [
    {"profile": "syn.release-manifest/2"}, {"repository": "another/syn"},
    {"tag": "latest"}, {"version": True}, {"source_commit": "main"},
    {"future_field": 1}, {"artifacts": []},
])
def test_unknown_or_changed_manifest_identity_is_refused(change):
    data = manifest().to_dict()
    data.update(change)
    with pytest.raises(ReleaseError):
        ReleaseManifest.from_bytes(canonical_bytes(data))


@pytest.mark.parametrize("change", [
    {"filename": "../syn_client-0.1.0.tar.gz"}, {"filename": "syn-0.1.0.zip"},
    {"size": True}, {"size": 0}, {"size": 536_870_913},
    {"sha256": "A" * 64}, {"role": "unknown"}, {"future_field": None},
])
def test_artifact_shape_path_and_size_are_closed(change):
    data = manifest().to_dict()
    data["artifacts"][0].update(change)
    with pytest.raises(ReleaseError):
        ReleaseManifest.from_bytes(canonical_bytes(data))


def test_duplicate_roles_and_noncanonical_json_do_not_have_ambiguous_signed_bytes():
    data = manifest().to_dict()
    data["artifacts"][1] = data["artifacts"][0]
    with pytest.raises(ReleaseError):
        ReleaseManifest.from_bytes(canonical_bytes(data))
    for raw in (manifest().canonical + b"\n", json.dumps(manifest().to_dict()).encode(),
                b'{"profile":1,"profile":2}', b'{"profile":1.0}', b"[]", b"x" * 65_537):
        with pytest.raises(ReleaseError):
            ReleaseManifest.from_bytes(raw)


@pytest.mark.parametrize("change", [
    {"client_version": "0.2.0"}, {"profile": "syn.runtime-contract/2"},
    {"python_versions": ["3.14"]}, {"runtime_platforms": ["linux-arm64"]},
    {"node_modes": ["service", "service"]}, {"node_modes": []},
    {"node_modes": ["arbitrary-code"]}, {"node_modes": [None]},
    {"capabilities": [{}]}, {"future": 1}, {"database_revision": "head"},
])
def test_runtime_descriptor_is_strict_and_declares_no_guessed_support(change):
    data = contract().to_dict()
    data.update(change)
    with pytest.raises(ReleaseError):
        RuntimeContract.from_dict(data)


def test_supported_service_and_windows_client_compatibility():
    item = manifest()
    assert compatible(item) is None
    assert compatible(item, component="client", platform="windows-x86_64", required_mode=None) is None


@pytest.mark.parametrize("change", [
    {"platform": "windows-x86_64"}, {"platform": "linux-arm64"},
    {"python_version": "3.14"}, {"required_mode": "operator"},
    {"required_mode": "agent"}, {"component": "client"},
    {"component": "syn-node"}, {"required_profiles": ("syn.communication/2",)},
    {"required_capabilities": ("encrypted-private-pubsub",)},
    {"required_capabilities": ["http-workspace-messages"]},
    {"required_profiles": ("syn.communication/1", "syn.communication/1")},
])
def test_unavailable_operator_agent_modes_and_unsupported_profiles_never_fallback(change):
    with pytest.raises(ReleaseError):
        compatible(manifest(), **change)


def test_installed_runtime_descriptor_must_equal_selected_release():
    item = manifest()
    with pytest.raises(ReleaseError, match="differs"):
        check_compatibility(item, replace(item.runtime, node_modes=("agent", "service")),
                            component="runtime", platform="linux-x86_64", python_version="3.13",
                            required_profiles=(), required_capabilities=(), required_mode="agent")


def test_signature_verification_missing_crypto_dependency_holds(monkeypatch):
    import builtins
    original = builtins.__import__

    def without_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("synthetic missing extra")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    item = manifest()
    with pytest.raises(ReleaseError, match="governance"):
        verify_release(item.canonical, signature(item), pin(item), PUBLIC)

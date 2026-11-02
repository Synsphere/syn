"""Release authority, no-overwrite staging and actual signed-byte readback gates."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from syn.release import runtimeContract
from syn_client.releases import ReleaseArtifact, ReleaseError, ReleaseManifest, artifact_names
from scripts import stage_release as release

SOURCE = "1" * 40


class FakeGitHub:
    def __init__(self):
        self.remote = None
        self.reference = None
        self.saved = {}
        self.created = 0
        self.downloaded = 0

    def release(self, tag):
        return self.remote

    def tag(self, tag):
        return self.reference

    def create_draft(self, tag, source, files, notes):
        self.created += 1
        self.saved = {p.name: p.read_bytes() for p in files}
        self.remote = {"id": 42, "html_url": "https://github.com/Synsphere/syn/releases/tag/" + tag,
                       "tag_name": tag, "draft": True, "immutable": False, "target_commitish": source,
                       "assets": [{"name": name, "size": len(raw)} for name, raw in self.saved.items()]}
        self.notes = notes.read_text()

    def download(self, tag, expected, directory):
        self.downloaded += 1
        assert set(self.saved) == expected
        for name, raw in self.saved.items():
            (directory / name).write_bytes(raw)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    authority = release.Authority("synthetic-release-authority", public, hashlib.sha256(public).hexdigest())
    artifacts = []
    for role, filename in sorted(artifact_names("0.1.0").items()):
        raw = ("synthetic package " + role).encode()
        (tmp_path / filename).write_bytes(raw)
        artifacts.append(ReleaseArtifact(role, filename, hashlib.sha256(raw).hexdigest(), len(raw)))
    manifest = ReleaseManifest("0.1.0", SOURCE, runtimeContract(), tuple(artifacts))
    (tmp_path / "syn-release-0.1.0.json").write_bytes(manifest.canonical)
    monkeypatch.setattr(release, "build_manifest", lambda directory, source: manifest)
    return SimpleNamespace(path=tmp_path, manifest=manifest, seed=seed, authority=authority, github=FakeGitHub())


def stage(value):
    return release.stage(value.path, SOURCE, "v0.1.0", value.seed, value.authority, value.github)


def test_stage_signs_reads_back_every_asset_and_never_publishes(bundle):
    evidence = stage(bundle)
    assert bundle.github.created == 1 and bundle.github.downloaded == 1
    assert evidence["published"] is False and evidence["immutable"] is False
    assert evidence["qualification"] == "verified_signed_draft_not_an_official_release"
    assert len(evidence["assets"]) == 7
    assert evidence["manifest_sha256"] == bundle.manifest.digest
    serialized = json.dumps(evidence)
    assert bundle.seed.hex() not in serialized
    assert base64.b64encode(bundle.seed).decode() not in serialized
    assert bundle.seed.hex() not in bundle.github.notes
    assert base64.b64encode(bundle.seed).decode() not in bundle.github.notes
    assert bundle.github.remote["draft"] is True


@pytest.mark.parametrize("existing", ["release", "tag"])
def test_existing_release_draft_or_tag_never_gets_replaced(bundle, existing):
    if existing == "release":
        bundle.github.remote = {"draft": True}
    else:
        bundle.github.reference = {"object": {"sha": SOURCE}}
    with pytest.raises(ReleaseError, match="already exists"):
        stage(bundle)
    assert bundle.github.created == 0
    assert not (bundle.path / "syn-release-0.1.0.sig.json").exists()


def test_wrong_private_key_never_reaches_a_remote_write(bundle):
    bundle.seed = b"x" * 32
    with pytest.raises(ReleaseError, match="does not match"):
        stage(bundle)
    assert bundle.github.created == 0


@pytest.mark.parametrize("change", ["extra", "package", "manifest", "tag", "directory"])
def test_changed_or_ambiguous_candidates_fail_before_remote_write(bundle, change):
    if change == "extra":
        (bundle.path / "private-backup.pdf").write_bytes(b"must never be uploaded")
    elif change == "package":
        (bundle.path / bundle.manifest.artifacts[0].filename).write_bytes(b"tampered")
    elif change == "manifest":
        (bundle.path / "syn-release-0.1.0.json").write_bytes(bundle.manifest.canonical + b"\n")
    elif change == "directory":
        target = bundle.path / bundle.manifest.artifacts[0].filename
        target.unlink()
        target.mkdir()
    with pytest.raises(ReleaseError):
        release.stage(bundle.path, SOURCE, "v0.2.0" if change == "tag" else "v0.1.0", bundle.seed, bundle.authority, bundle.github)
    assert bundle.github.created == 0


def test_published_readback_requires_an_immutable_release_and_exact_commit(bundle):
    stage(bundle)
    bundle.github.remote.update(draft=False, immutable=True)
    bundle.github.reference = {"object": {"type": "commit", "sha": SOURCE}}
    _, pin = release.sign_manifest(bundle.manifest, bundle.seed, bundle.authority)
    evidence = release.verify_published("v0.1.0", pin, bundle.authority, bundle.github)
    assert evidence["published"] is True and evidence["immutable"] is True
    assert bundle.github.created == 1 and bundle.github.downloaded == 2


@pytest.mark.parametrize("change", ["draft", "mutable", "commit", "asset", "payload", "size", "signature-size", "boolean-size"])
def test_publication_cannot_be_inferred_from_an_upload_or_wrong_metadata(bundle, change):
    stage(bundle)
    bundle.github.remote.update(draft=False, immutable=True)
    bundle.github.reference = {"object": {"type": "commit", "sha": SOURCE}}
    if change == "draft":
        bundle.github.remote["draft"] = True
    elif change == "mutable":
        bundle.github.remote["immutable"] = False
    elif change == "commit":
        bundle.github.reference["object"]["sha"] = "2" * 40
    elif change == "asset":
        bundle.github.remote["assets"].pop()
    elif change == "payload":
        bundle.github.saved[bundle.manifest.artifacts[0].filename] = b"replaced"
    elif change == "signature-size":
        next(x for x in bundle.github.remote["assets"] if x["name"].endswith(".sig.json"))["size"] = 1
    elif change == "boolean-size":
        bundle.github.remote["assets"][0]["size"] = True
    else:
        bundle.github.remote["assets"][0]["size"] += 1
    _, pin = release.sign_manifest(bundle.manifest, bundle.seed, bundle.authority)
    with pytest.raises(ReleaseError):
        release.verify_published("v0.1.0", pin, bundle.authority, bundle.github)
    assert bundle.github.created == 1


def test_committed_authority_contains_only_a_matching_public_identity():
    authority = release.load_authority(release.ROOT / "release/authority.json")
    assert len(authority.public_key) == 32
    assert authority.fingerprint == hashlib.sha256(authority.public_key).hexdigest()
    assert authority.key_id == "syn-release-2026-11-02"


@pytest.mark.parametrize("change", [
    {"private_key": "forbidden"}, {"profile": "unknown"}, {"key_id": ""},
    {"public_key": "x" * 64}, {"release_key_sha256": "0" * 64},
])
def test_invalid_or_private_authority_fields_are_refused(tmp_path, change):
    policy = json.loads((release.ROOT / "release/authority.json").read_text())
    policy.update(change)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ReleaseError):
        release.load_authority(path)


@pytest.mark.parametrize("value", [None, "", "x" * 44, base64.b64encode(b"x" * 31).decode(), base64.b64encode(b"x" * 32).decode() + "\n"])
def test_private_key_encoding_rejects_wrong_length_and_noncanonical_input(value):
    with pytest.raises(ReleaseError):
        release.decode_seed(value)


def test_signing_capability_is_removed_from_all_github_child_environments(monkeypatch):
    monkeypatch.setenv(release.KEY_ENV, "synthetic-secret-not-a-production-key")
    def run(command, **kwargs):
        assert release.KEY_ENV not in kwargs["env"]
        assert command[0] == "gh"
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(release.subprocess, "run", run)
    assert release.GitHub().api("releases?per_page=100") == []


@pytest.mark.parametrize("field,value", [
    ("GITHUB_REPOSITORY", "other/syn"), ("GITHUB_REF", "refs/heads/untrusted"),
    ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_SHA", "2" * 40),
])
def test_unadmitted_workflow_context_never_even_executes_git(monkeypatch, field, value):
    for name, selected in {"GITHUB_REPOSITORY": "Synsphere/syn", "GITHUB_REF": "refs/heads/master", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SOURCE}.items():
        monkeypatch.setenv(name, selected)
    monkeypatch.setenv(field, value)
    monkeypatch.setattr(release.subprocess, "check_output", lambda *a, **k: pytest.fail("unadmitted child execution"))
    with pytest.raises(ReleaseError, match="exact master"):
        release.require_workflow_context(SOURCE)


def test_asset_metadata_rejects_duplicate_names(bundle):
    stage(bundle)
    remote = deepcopy(bundle.github.remote)
    remote["assets"][0]["name"] = remote["assets"][1]["name"]
    with pytest.raises(ReleaseError, match="names or duplicates"):
        release.metadata(remote, bundle.manifest, draft=True)

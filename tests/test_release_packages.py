"""Package-boundary and dependency-confusion cases for release candidates."""
from io import BytesIO
import tarfile
import zipfile

import pytest
from syn_client.releases import ReleaseError

from scripts.build_release_contract import _safe_names, check_requirements, check_sdist, check_wheel


def metadata(name="syn-client", version="0.1.0"):
    return (f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: >=3.12\n"
            'Provides-Extra: governance\nRequires-Dist: cryptography==50.0.2 ; extra == "governance"\n').encode()


def wheel(tmp_path, **changes):
    info = "syn_client-0.1.0.dist-info"
    files = {f"syn_client/{x}": b"# synthetic fixture\n" for x in
             ("__init__.py", "releases.py", "consensus.py", "governance.py", "dag_signing.py")}
    files.update({f"{info}/METADATA": metadata(), f"{info}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n", f"{info}/RECORD": b""})
    files.update(changes)
    path = tmp_path / "syn_client-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in files.items():
            if raw is not None:
                archive.writestr(name, raw)
    return path


def test_minimal_wheel_fixture_has_correct_role_resources_and_metadata(tmp_path):
    check_wheel(wheel(tmp_path), "syn-client", "0.1.0")


@pytest.mark.parametrize("changes", [
    {"syn_client/releases.py": None},
    {"syn_client/config.toml": b"private configuration"},
    {"syn_client/models/weights.bin": b"private model"},
    {"syn_client/.secrets/account": b"private secret"},
    {"../outside.py": b"path escape"},
    {"syn_client/./releases.py": b"aliased module"},
    {"other/__init__.py": b"unexpected module"},
    {"syn_client-0.1.0.dist-info/METADATA": metadata(version="0.2.0")},
    {"syn_client-0.1.0.dist-info/METADATA": metadata() + b"Requires-Dist: requests==2.0\n"},
    {"syn_client-0.1.0.dist-info/METADATA": metadata().replace(b"==50.0.2", b">=50.0.2")},
    {"syn_client-0.1.0.dist-info/WHEEL": b"Tag: cp313-none-win_amd64\n"},
])
def test_bad_wheel_content_cannot_become_an_official_candidate(tmp_path, changes):
    with pytest.raises(ReleaseError):
        check_wheel(wheel(tmp_path, **changes), "syn-client", "0.1.0")


@pytest.mark.parametrize("names", [
    ["x", "X"], ["x", "x"], ["./x"], ["a//b"], ["/absolute"],
    ["C:/outside"], ["a\\b"], ["a/../b"], ["a/.env"], ["syn.sql"],
    ["a/syn.zip"], ["private.key"], ["models/model.bin"], ["a/line\nbreak"], [],
])
def test_unsafe_archive_names_hold_without_extraction(names):
    with pytest.raises(ReleaseError):
        _safe_names(names)


def test_source_archive_is_checked_without_extracting_members(tmp_path):
    path = tmp_path / "syn_client-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        item = tarfile.TarInfo("syn_client-0.1.0/PKG-INFO")
        raw = metadata()
        item.size = len(raw)
        archive.addfile(item, BytesIO(raw))
    check_sdist(path, "syn-client", "0.1.0")
    with tarfile.open(path, "w:gz") as archive:
        link = tarfile.TarInfo("syn_client-0.1.0/linked")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    with pytest.raises(ReleaseError):
        check_sdist(path, "syn-client", "0.1.0")
    assert not (tmp_path / "outside").exists()


def test_hashed_export_has_only_exact_third_party_requirements():
    raw = ("# uv export\nexample==1.2.3 ; python_version >= '3.12' \\\n    --hash=sha256:" + "a" * 64 + " \\\n    --hash=sha256:" + "b" * 64 + "\n").encode()
    check_requirements(raw)


@pytest.mark.parametrize("line", [
    "syn==0.1.0", "syn-client==0.1.0", "syn_client==0.1.0", "-e .", "../local.whl",
    "example @ https://example.test/package.whl", "--index-url https://example.test",
    "example>=1", "example==1.*", "example==1 --hash=md5:1234", "-r other.txt",
])
def test_source_syn_or_unpinned_dependency_fallback_is_refused(line):
    if "--hash=" not in line:
        line += " --hash=sha256:" + "a" * 64
    with pytest.raises(ReleaseError):
        check_requirements((line + "\n").encode())


@pytest.mark.parametrize("raw", [b"", b"# empty\n", b"example==1\n", b"\xff"])
def test_absent_unhashed_or_invalid_dependency_exports_hold(raw):
    with pytest.raises(ReleaseError):
        check_requirements(raw)

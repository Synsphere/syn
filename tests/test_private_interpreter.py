"""Protect the copied interpreter without changing a hosted tool or foreign link."""
import os
import stat

import pytest

from scripts.private_interpreter import copy_private_python


@pytest.mark.skipif(os.name != "posix", reason="Native POSIX executable ownership/modes")
def test_private_copy_removes_foreign_write_without_following_links(tmp_path):
    prefix = tmp_path / "managed-python"
    prefix.mkdir(mode=0o775)
    binary = prefix / "bin"
    binary.mkdir(mode=0o775)
    source = binary / "python3"
    source.write_bytes(b"synthetic managed executable; never executed")
    source.chmod(0o775)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"untouched")
    foreign.chmod(0o666)
    (prefix / "foreign-link").symlink_to(foreign)
    selected = copy_private_python(source, prefix, tmp_path / "private-python")
    assert selected.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(selected.stat().st_mode) == 0o755
    assert stat.S_IMODE(source.stat().st_mode) == 0o775
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o666
    assert (selected.parents[1] / "foreign-link").is_symlink()
    with pytest.raises(FileExistsError):
        copy_private_python(source, prefix, selected.parents[1])


def test_interpreter_outside_selected_prefix_is_rejected_before_copy(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    outside = tmp_path / "not-in-prefix"
    outside.write_bytes(b"synthetic")
    destination = tmp_path / "must-not-create"
    with pytest.raises(ValueError, match="ambiguous"):
        copy_private_python(outside, prefix, destination)
    assert not destination.exists()

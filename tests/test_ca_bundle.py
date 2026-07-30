from __future__ import annotations

import hashlib
from importlib.resources import files


EXPECTED_SHA256 = "3ff344e30b9b1ed2971044eabb438a08f2e2245ddb5f8ab1a3ad8b63ab4eaf91"


def test_packaged_ca_bundle_matches_published_checksum() -> None:
    bundle = files("syn").joinpath("cacert.pem").read_bytes()
    checksum = files("syn").joinpath("cacert.pem.sha256").read_text().split()[0]
    assert hashlib.sha256(bundle).hexdigest() == EXPECTED_SHA256 == checksum
    assert bundle.count(b"-----BEGIN CERTIFICATE-----") == 119

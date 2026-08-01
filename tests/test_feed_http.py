from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from OpenSSL import SSL, crypto
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from syn.feed_jobs import (
    HttpWitness,
    _RetryingTlsConnection,
    _certificateDetails,
    timedHttpGet,
)


def test_tls_cleanup_does_not_mask_the_original_handshake_error() -> None:
    rawSocket = Mock()
    address = (2, 1, 6, "", ("93.184.216.34", 443))
    connection = Mock()
    connection.do_handshake.side_effect = RuntimeError("original handshake failure")
    connection.shutdown.side_effect = RuntimeError("shutdown while in init")
    with (
        patch("syn.feed_jobs.socket.getaddrinfo", return_value=[address]),
        patch("syn.feed_jobs.socket.socket", return_value=rawSocket),
        patch("syn.feed_jobs.SSL.Connection", return_value=connection),
        pytest.raises(RuntimeError, match="original handshake failure"),
    ):
        timedHttpGet("https://example.com/feed", 1, 1000)


def test_get_follows_301_and_302_redirects_and_logs_each_request() -> None:
    def response(url, status, location=None):
        headers = {"location": [location]} if location else {}
        return HttpWitness(
            url, status, "redirect" if location else "OK", "93.184.216.34",
            {}, headers, b"feed", {"startedAt": url + ":start", "finishedAt": url + ":end", "stages": []},
        )

    responses = [
        response("http://example.com/feed", 301, "/moved"),
        response("http://example.com/moved", 302, "https://feeds.example.com/final"),
        response("https://feeds.example.com/final", 200),
    ]
    logs = []
    with patch("syn.feed_jobs._timedSingleHttpGet", side_effect=responses) as request:
        witness = timedHttpGet("http://example.com/feed", 1, 1000, logger=logs.append)
    assert request.call_count == 3
    assert witness.url == "https://feeds.example.com/final"
    assert witness.status == 200
    assert [item["status"] for item in witness.redirects] == [301, 302]
    assert len(witness.timings["requests"]) == 3
    assert any("Following HTTP 301 redirect" in entry for entry in logs)
    assert any("Opening HTTP request 3" in entry for entry in logs)


def test_tls_connection_retries_want_read_instead_of_failing() -> None:
    connection = Mock()
    connection.do_handshake.side_effect = [SSL.WantReadError(), None]
    rawSocket = Mock()
    wrapped = _RetryingTlsConnection(connection, rawSocket, 1)
    with patch("syn.feed_jobs.socketSelect.select", return_value=([rawSocket], [], [])):
        wrapped.do_handshake()
    assert connection.do_handshake.call_count == 2


def test_certificate_details_include_type_fingerprint_and_public_key() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "feed.example")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(42)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    details = _certificateDetails(crypto.X509.from_cryptography(certificate), 0)
    assert details["certificateType"] == "X.509"
    assert details["chainPosition"] == 0
    assert details["serialNumber"] == "42"
    assert details["publicKeyType"] == "RSAPublicKey"
    assert details["publicKeySizeBits"] == 2048
    assert details["publicKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")
    assert details["sha256Fingerprint"] == certificate.fingerprint(hashes.SHA256()).hex()

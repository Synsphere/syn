from __future__ import annotations

import base64
from functools import partial
import gzip
from importlib.resources import as_file, files
import ipaddress
import json
import select as socketSelect
import socket
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import HTTP
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from OpenSSL import SSL
from cryptography.hazmat.primitives import hashes, serialization
from service_identity.pyopenssl import verify_hostname
from sqlalchemy import text

from .database import createEngine, createSessionFactory
from .models import Ember, Event, EventTarget, ObjectRelationship, ObjectType
from .refs import ObjectRef

FEED_WITNESS_HANDLER = "witness-stale-feeds"
_TIMESTAMP_NAMES = frozenset(
    {
        "date", "date_modified", "date_published", "issued", "modified",
        "pubdate", "published", "updated",
    }
)
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization", "cookie", "proxy-authorization", "proxy-authenticate",
        "set-cookie", "x-api-key",
    }
)


@dataclass(frozen=True, slots=True)
class FeedCandidate:
    feedRef: str
    url: str
    lastRequestedAt: datetime | None
    entryTimestamps: tuple[datetime, ...]

    @property
    def activityScore(self) -> float:
        return feedActivityScore(self.lastRequestedAt, self.entryTimestamps)


@dataclass(frozen=True, slots=True)
class HttpWitness:
    url: str
    status: int
    reason: str
    serverIp: str
    requestHeaders: dict[str, str]
    responseHeaders: dict[str, list[str]]
    body: bytes
    timings: dict[str, Any]
    tlsCertificates: tuple[dict[str, Any], ...] = ()
    redirects: tuple[dict[str, Any], ...] = ()


def feedActivityScore(
    lastRequestedAt: datetime | None, entryTimestamps: tuple[datetime, ...]
) -> float:
    if lastRequestedAt is None or not entryTimestamps:
        return 0.0
    newest = max(entryTimestamps)
    windowStart = newest - timedelta(days=7)
    updateCount = sum(windowStart <= value <= newest for value in entryTimestamps)
    lagHours = max((lastRequestedAt - newest).total_seconds() / 3600, 1.0)
    return updateCount / lagHours


def parseFeedEntryTimestamps(body: bytes, contentType: str | None = None) -> tuple[datetime, ...]:
    values: list[datetime] = []
    mediaType = (contentType or "").split(";", 1)[0].strip().casefold()
    if "json" in mediaType or body.lstrip().startswith((b"{", b"[")):
        try:
            _collectJsonTimestamps(json.loads(body), values)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    else:
        try:
            root = ET.fromstring(body)
            for element in root.iter():
                name = element.tag.rsplit("}", 1)[-1].casefold()
                if name in _TIMESTAMP_NAMES and element.text:
                    parsed = _parseTimestamp(element.text)
                    if parsed is not None:
                        values.append(parsed)
        except ET.ParseError:
            pass
    return tuple(sorted(set(values)))


def _collectJsonTimestamps(value: Any, output: list[datetime]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _TIMESTAMP_NAMES and isinstance(child, str):
                parsed = _parseTimestamp(child)
                if parsed is not None:
                    output.append(parsed)
            else:
                _collectJsonTimestamps(child, output)
    elif isinstance(value, list):
        for child in value:
            _collectJsonTimestamps(child, output)


def _parseTimestamp(value: str) -> datetime | None:
    cleaned = value.strip()
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(cleaned)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _oldestFeedCandidates(databaseUrl: str, limit: int) -> list[FeedCandidate]:
    engine = createEngine(databaseUrl, migrate=False)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    '''
                    SELECT thing.id::text AS "feedRef",
                           COALESCE(thing.knowledge->>'canonicalUrl',
                                    thing.knowledge->>'feedUrl') AS url,
                           witness.knowledge->'request'->>'finishedAt' AS "lastRequestedAt",
                           witness.knowledge->'parsed'->'entryTimestamps' AS "entryTimestamps"
                    FROM things thing
                    LEFT JOIN LATERAL (
                        SELECT event.knowledge, event."occurredAt"
                        FROM events event
                        WHERE event.knowledge->>'witnessKind' = 'feed'
                          AND event.knowledge->>'feedRef' = thing.id::text
                        ORDER BY event."occurredAt" DESC
                        LIMIT 1
                    ) witness ON true
                    WHERE EXISTS (
                              SELECT 1 FROM "objectTypesInherited" inherited
                              WHERE inherited."objectRef" = thing.id::"objectRef"
                                AND inherited."typeRef" = 'syn://type/feed'::"typeRef"
                          )
                      AND COALESCE(thing.knowledge->>'canonicalUrl',
                                   thing.knowledge->>'feedUrl') IS NOT NULL
                    ORDER BY (
                        SELECT max(value::timestamptz)
                        FROM jsonb_array_elements_text(
                            COALESCE(witness.knowledge->'parsed'->'entryTimestamps', '[]'::jsonb)
                        ) AS value
                    ) ASC NULLS FIRST, thing.id
                    LIMIT :limit
                    '''
                ),
                {"limit": limit},
            ).mappings()
            candidates = []
            for row in rows:
                requested = _parseTimestamp(row["lastRequestedAt"]) if row["lastRequestedAt"] else None
                timestamps = tuple(
                    parsed
                    for raw in (row["entryTimestamps"] or [])
                    if (parsed := _parseTimestamp(raw)) is not None
                )
                candidates.append(
                    FeedCandidate(row["feedRef"], row["url"], requested, timestamps)
                )
            return candidates
    finally:
        engine.dispose()


def evaluateStaleFeeds(
    databaseUrl: str,
    shortlistSize: int = 100,
    remaining: int = 50,
    timeoutSeconds: float = 30,
    maximumBytes: int = 10_000_000,
    dryRun: bool = False,
) -> dict[str, Any]:
    if shortlistSize <= remaining or remaining < 0:
        raise ValueError("shortlistSize must be greater than remaining, which must be non-negative")
    candidates = _oldestFeedCandidates(databaseUrl, shortlistSize)
    requested: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    logs: list[str] = []

    def log(message: str) -> None:
        entry = f"{datetime.now(UTC).isoformat()} {message}"
        logs.append(entry)
        print(entry, flush=True)

    log(
        f"Selected {len(candidates)} feeds with the oldest parsed updates; "
        f"will process {max(len(candidates) - remaining, 0)}"
    )
    while len(candidates) > remaining:
        candidate = max(candidates, key=lambda item: (item.activityScore, item.feedRef))
        candidates.remove(candidate)
        log(
            f"Selected {candidate.feedRef} with activity score "
            f"{candidate.activityScore:.6f}; {len(candidates)} feeds will remain after this attempt"
        )
        try:
            log(f"Requesting {candidate.url}")
            witness = timedHttpGet(candidate.url, timeoutSeconds, maximumBytes, logger=log)
            log(
                f"Received HTTP {witness.status} from {witness.url} "
                f"({len(witness.body)} bytes via {witness.serverIp})"
            )
            contentType = next(iter(witness.responseHeaders.get("content-type", [])), None)
            entryCount = len(
                parseFeedEntryTimestamps(
                    _decodeBody(witness.body, witness.responseHeaders), contentType
                )
            )
            eventRef = None
            if dryRun:
                log(f"Parsed {entryCount} entry timestamps; dry run, so no event was written")
            else:
                eventRef, entryCount = _storeWitness(databaseUrl, candidate, witness)
                log(f"Parsed {entryCount} entry timestamps and created {eventRef}")
            requested.append(
                {
                    "feedRef": candidate.feedRef,
                    "url": candidate.url,
                    "activityScore": candidate.activityScore,
                    "eventRef": eventRef,
                    "entryTimestampCount": entryCount,
                    "status": witness.status,
                    "effectiveUrl": witness.url,
                    "redirects": list(witness.redirects),
                }
            )
        except Exception as error:
            details = _exceptionDetails(error)
            log(
                f"Request failed for {candidate.url}: {details['type']}: "
                f"{details['repr']}\n{details['traceback']}"
            )
            failures.append(
                {
                    "feedRef": candidate.feedRef,
                    "url": candidate.url,
                    "activityScore": candidate.activityScore,
                    "remainingAfterAttempt": len(candidates),
                    "error": details,
                }
            )
    log(f"Finished with {len(requested)} successful requests, {len(failures)} failures, and {len(candidates)} remaining")
    return {
        "shortlisted": min(shortlistSize, len(candidates) + len(requested) + len(failures)),
        "requested": requested,
        "failures": failures,
        "remaining": len(candidates),
        "logs": logs,
        "dryRun": dryRun,
    }


def _exceptionDetails(error: BaseException, seen: set[int] | None = None) -> dict[str, Any]:
    visited = seen or set()
    if id(error) in visited:
        return {"type": f"{type(error).__module__}.{type(error).__qualname__}", "cycle": True}
    visited.add(id(error))
    details: dict[str, Any] = {
        "type": f"{type(error).__module__}.{type(error).__qualname__}",
        "message": str(error),
        "repr": repr(error),
        "arguments": [_jsonSafe(value) for value in error.args],
        "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    }
    notes = getattr(error, "__notes__", None)
    if notes:
        details["notes"] = list(notes)
    if error.__cause__ is not None:
        details["cause"] = _exceptionDetails(error.__cause__, visited)
    elif error.__context__ is not None and not error.__suppress_context__:
        details["context"] = _exceptionDetails(error.__context__, visited)
    return details


def _jsonSafe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonSafe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonSafe(item) for key, item in value.items()}
    return repr(value)


def _storeWitness(
    databaseUrl: str, candidate: FeedCandidate, witness: HttpWitness
) -> tuple[str, int]:
    objects, eventRef, entryCount = _buildWitnessObjects(candidate, witness)
    engine = createEngine(databaseUrl, migrate=False)
    sessions = createSessionFactory(engine)
    try:
        with sessions.begin() as session:
            principals = [item for item in objects if isinstance(item, (Event, Ember))]
            links = [item for item in objects if not isinstance(item, (Event, Ember))]
            session.add_all(principals)
            session.flush()
            session.add_all(links)
    finally:
        engine.dispose()
    return eventRef, entryCount


def _buildWitnessObjects(
    candidate: FeedCandidate, witness: HttpWitness
) -> tuple[list[Any], str, int]:
    contentType = next(iter(witness.responseHeaders.get("content-type", [])), None)
    decodedBody = _decodeBody(witness.body, witness.responseHeaders)
    entryTimestamps = parseFeedEntryTimestamps(decodedBody, contentType)
    identity = uuid4()
    eventRef = str(ObjectRef.local("event", f"feed-witness-{identity}"))
    observationRef = str(ObjectRef.local("ember", f"http-observation-{identity}"))
    networkStatsRef = str(ObjectRef.local("ember", f"network-stats-{identity}"))
    occurredAt = datetime.now(UTC)
    eventKnowledge = {
        "witnessKind": "feed",
        "feedRef": candidate.feedRef,
        "feedUrl": candidate.url,
        "activityScore": candidate.activityScore,
        "observationRef": observationRef,
        "networkStatsRef": networkStatsRef,
    }
    observationKnowledge = {
        "observedAt": occurredAt.isoformat(),
        "feedRef": candidate.feedRef,
        "feedUrl": candidate.url,
        "request": {
            "startedAt": witness.timings["startedAt"],
            "finishedAt": witness.timings["finishedAt"],
            "headersSent": _redactHeaders(witness.requestHeaders),
            "headersReceived": _redactHeaders(witness.responseHeaders),
            "effectiveUrl": witness.url,
            "redirects": list(witness.redirects),
        },
        "httpStatus": witness.status,
        "httpReason": witness.reason,
        "response": {
            "rawBase64": base64.b64encode(witness.body).decode("ascii"),
            "byteLength": len(witness.body),
        },
        "parsed": {
            "entryTimestamps": [value.isoformat() for value in entryTimestamps],
            "entryTimestampCount": len(entryTimestamps),
        },
    }
    networkStats = {
        "serverIp": witness.serverIp,
        "timings": witness.timings,
        "requestCount": len(witness.timings.get("requests", [])),
        "redirectCount": len(witness.redirects),
        "transferredBytes": len(witness.body),
    }
    objects: list[Any] = [
        Event(id=eventRef, name="feedWitness", occurredAt=occurredAt, knowledge=eventKnowledge),
        Ember(id=observationRef, name="httpObservationResult", knowledge=observationKnowledge),
        Ember(id=networkStatsRef, name="networkStats", stats=networkStats, statsUpdated=occurredAt),
        ObjectType(objectRef=eventRef, typeRef="syn://type/observation"),
        ObjectType(objectRef=observationRef, typeRef="syn://type/observation-result"),
        ObjectType(objectRef=networkStatsRef, typeRef="syn://type/network-measurement"),
        EventTarget(eventRef=eventRef, targetRef=candidate.feedRef, role="feed", position=0),
        EventTarget(eventRef=eventRef, targetRef=candidate.url, role="feed-url", position=1),
        EventTarget(eventRef=eventRef, targetRef=observationRef, role="observation-result", position=2),
        EventTarget(eventRef=eventRef, targetRef=networkStatsRef, role="network-stats", position=3),
        ObjectRelationship(
            sourceRef=eventRef, targetRef=observationRef,
            relationshipType="has-observation-result", properties={},
        ),
        ObjectRelationship(
            sourceRef=eventRef, targetRef=networkStatsRef,
            relationshipType="has-network-stats", properties={},
        ),
        ObjectRelationship(
            sourceRef=observationRef, targetRef=networkStatsRef,
            relationshipType="measured-with", properties={},
        ),
    ]
    return objects, eventRef, len(entryTimestamps)


def _redactHeaders(headers: dict[str, Any]) -> dict[str, Any]:
    return {
        key: "[REDACTED]" if key.casefold() in _SENSITIVE_HEADERS else value
        for key, value in headers.items()
    }


def timedHttpGet(
    url: str,
    timeoutSeconds: float,
    maximumBytes: int,
    *,
    maximumRedirects: int = 10,
    logger: Any = None,
) -> HttpWitness:
    currentUrl = url
    hops: list[HttpWitness] = []
    redirects: list[dict[str, Any]] = []
    for redirectCount in range(maximumRedirects + 1):
        if logger:
            logger(f"Opening HTTP request {redirectCount + 1}: {currentUrl}")
        witness = _timedSingleHttpGet(currentUrl, timeoutSeconds, maximumBytes)
        hops.append(witness)
        location = next(iter(witness.responseHeaders.get("location", [])), None)
        if witness.status not in (301, 302) or not location:
            combinedTimings = {
                "startedAt": hops[0].timings["startedAt"],
                "finishedAt": witness.timings["finishedAt"],
                "requests": [
                    {
                        "url": hop.url,
                        "status": hop.status,
                        "timings": hop.timings,
                        "tlsCertificates": list(hop.tlsCertificates),
                    }
                    for hop in hops
                ],
            }
            return HttpWitness(
                witness.url, witness.status, witness.reason, witness.serverIp,
                witness.requestHeaders, witness.responseHeaders, witness.body,
                combinedTimings, witness.tlsCertificates, tuple(redirects),
            )
        if redirectCount == maximumRedirects:
            raise ValueError(f"Feed exceeded the maximum of {maximumRedirects} redirects")
        nextUrl = urljoin(currentUrl, location)
        if urlsplit(nextUrl).scheme not in ("http", "https"):
            raise ValueError(f"Refusing redirect to unsupported URL: {nextUrl}")
        redirect = {"status": witness.status, "from": currentUrl, "to": nextUrl}
        redirects.append(redirect)
        if logger:
            logger(f"Following HTTP {witness.status} redirect from {currentUrl} to {nextUrl}")
        currentUrl = nextUrl
    raise AssertionError("redirect loop terminated unexpectedly")


def _timedSingleHttpGet(url: str, timeoutSeconds: float, maximumBytes: int) -> HttpWitness:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError("Feed URL must be an HTTP(S) URL without credentials")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    clockStart = time.perf_counter_ns()
    startedAt = datetime.now(UTC)
    stages: list[dict[str, Any]] = []

    def mark(name: str, **details: Any) -> None:
        stages.append({"stage": name, "offsetNanoseconds": time.perf_counter_ns() - clockStart, **details})

    mark("dnsStarted", host=host)
    addresses = _requestCall(
        lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        url, "DNS resolution", stages,
    )
    mark("dnsCompleted", addresses=[item[4][0] for item in addresses])
    address = next((item for item in addresses if ipaddress.ip_address(item[4][0]).is_global), None)
    if address is None:
        raise ValueError("Feed hostname did not resolve to a public IP address")
    family, socktype, protocol, _, endpoint = address
    rawSocket = socket.socket(family, socktype, protocol)
    rawSocket.settimeout(timeoutSeconds)
    connection: Any = rawSocket
    tlsConnection = False
    tlsCertificates: tuple[dict[str, Any], ...] = ()
    try:
        mark("tcpConnectStarted", ip=endpoint[0], port=port)
        _requestCall(lambda: rawSocket.connect(endpoint), url, "TCP connection", stages)
        mark("tcpConnectCompleted")
        if parsed.scheme == "https":
            context = SSL.Context(SSL.TLS_CLIENT_METHOD)
            caBundle = files("syn").joinpath("cacert.pem")
            with as_file(caBundle) as caBundlePath:
                context.load_verify_locations(str(caBundlePath))
            context.set_verify(SSL.VERIFY_PEER, lambda _c, _x, _e, _d, ok: ok)
            context.set_info_callback(
                lambda conn, where, result: mark(
                    "tlsState", state=conn.get_state_string().decode("ascii", "replace"),
                    flags=where, result=result,
                )
            )
            tlsObject = SSL.Connection(context, rawSocket)
            connection = _RetryingTlsConnection(tlsObject, rawSocket, timeoutSeconds)
            tlsConnection = True
            tlsObject.set_connect_state()
            tlsObject.set_tlsext_host_name(host.encode("idna"))
            mark("tlsHandshakeStarted")
            _requestCall(connection.do_handshake, url, "TLS handshake", stages)
            _requestCall(
                lambda: verify_hostname(tlsObject, host),
                url, "TLS certificate hostname verification", stages,
            )
            mark("tlsHandshakeCompleted", protocol=tlsObject.get_protocol_version_name())
            tlsCertificates = tuple(
                _certificateDetails(certificate, position)
                for position, certificate in enumerate(tlsObject.get_peer_cert_chain() or ())
            )
            mark("tlsCertificateChainCaptured", certificates=len(tlsCertificates))

        requestHeaders = {
            "Host": host if port in (80, 443) else f"{host}:{port}",
            "User-Agent": "SynFeedWitness/0.1",
            "Accept": "application/feed+json, application/rss+xml, application/atom+xml, application/json, application/xml, text/xml, */*",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        }
        request = f"GET {path} HTTP/1.1\r\n".encode("ascii") + b"".join(
            f"{key}: {value}\r\n".encode("ascii") for key, value in requestHeaders.items()
        ) + b"\r\n"
        mark("httpRequestHeadersStarted")
        _requestCall(
            lambda: connection.sendall(request), url, "HTTP request header transfer", stages
        )
        mark("httpRequestHeadersCompleted")
        headerBytes, initialBody = _requestCall(
            lambda: _receiveHeaders(connection, maximumBytes, mark),
            url, "HTTP response header transfer", stages,
        )
        status, reason, responseHeaders = _parseResponseHeaders(headerBytes)
        body = _requestCall(
            lambda: _receiveBody(connection, initialBody, responseHeaders, maximumBytes, mark),
            url, "HTTP response body transfer", stages,
        )
        finishedAt = datetime.now(UTC)
        mark("dataTransferCompleted", bytes=len(body))
        return HttpWitness(
            url, status, reason, endpoint[0], requestHeaders, responseHeaders, body,
            {"startedAt": startedAt.isoformat(), "finishedAt": finishedAt.isoformat(), "stages": stages},
            tlsCertificates,
        )
    finally:
        mark("connectionCloseStarted")
        try:
            try:
                if tlsConnection:
                    connection.shutdown()
            except Exception:
                # Cleanup must never replace the request/handshake exception that
                # explains why this witness failed.
                pass
            try:
                connection.close()
            except Exception:
                pass
        finally:
            mark("connectionClosed")


def _requestCall(operation: Any, url: str, action: str, stages: list[dict[str, Any]]) -> Any:
    try:
        return operation()
    except Exception as error:
        error.add_note(f"Feed URL: {url}")
        error.add_note(f"Failed action: {action}")
        error.add_note("Completed timing stages: " + json.dumps(stages, default=str))
        raise


def _certificateDetails(certificate: Any, position: int) -> dict[str, Any]:
    value = certificate.to_cryptography()
    publicKey = value.public_key()
    details: dict[str, Any] = {
        "chainPosition": position,
        "certificateType": "X.509",
        "version": value.version.name,
        "serialNumber": str(value.serial_number),
        "subject": value.subject.rfc4514_string(),
        "issuer": value.issuer.rfc4514_string(),
        "notValidBefore": value.not_valid_before_utc.isoformat(),
        "notValidAfter": value.not_valid_after_utc.isoformat(),
        "sha256Fingerprint": value.fingerprint(hashes.SHA256()).hex(),
        "signatureAlgorithmOid": value.signature_algorithm_oid.dotted_string,
        "signatureAlgorithm": value.signature_algorithm_oid._name,
        "publicKeyType": type(publicKey).__name__,
        "publicKeyPem": publicKey.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii"),
    }
    keySize = getattr(publicKey, "key_size", None)
    if keySize is not None:
        details["publicKeySizeBits"] = keySize
    curve = getattr(publicKey, "curve", None)
    if curve is not None:
        details["publicKeyCurve"] = curve.name
    return details


class _RetryingTlsConnection:
    def __init__(self, connection: SSL.Connection, rawSocket: socket.socket, timeout: float) -> None:
        self.connection = connection
        self.rawSocket = rawSocket
        self.timeout = timeout

    def _retry(self, operation: Any) -> Any:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                return operation()
            except SSL.WantReadError:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not socketSelect.select([self.rawSocket], [], [], remaining)[0]:
                    raise TimeoutError("TLS operation timed out waiting to read")
            except SSL.WantWriteError:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not socketSelect.select([], [self.rawSocket], [], remaining)[1]:
                    raise TimeoutError("TLS operation timed out waiting to write")

    def do_handshake(self) -> None:
        self._retry(self.connection.do_handshake)

    def sendall(self, data: bytes) -> None:
        sent = 0
        while sent < len(data):
            sent += self._retry(lambda: self.connection.send(data[sent:]))

    def recv(self, size: int) -> bytes:
        return self._retry(lambda: self.connection.recv(size))

    def shutdown(self) -> Any:
        return self._retry(self.connection.shutdown)

    def close(self) -> None:
        self.connection.close()


def _receiveHeaders(connection: Any, maximumBytes: int, mark: Any) -> tuple[bytes, bytes]:
    data = bytearray()
    mark("httpResponseHeadersStarted")
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            raise ConnectionError("Connection closed before HTTP response headers completed")
        data.extend(chunk)
        if len(data) > min(maximumBytes, 1_048_576):
            raise ValueError("HTTP response headers are too large")
    headers, body = bytes(data).split(b"\r\n\r\n", 1)
    mark("httpResponseHeadersCompleted")
    if body:
        mark("dataTransferStarted")
    return headers + b"\r\n", body


def _parseResponseHeaders(raw: bytes) -> tuple[int, str, dict[str, list[str]]]:
    first, fields = raw.split(b"\r\n", 1)
    parts = first.decode("iso-8859-1").split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ValueError("Invalid HTTP status line")
    message = BytesParser(policy=HTTP).parsebytes(fields)
    headers: dict[str, list[str]] = {}
    for key, value in message.items():
        headers.setdefault(key.casefold(), []).append(value)
    return int(parts[1]), parts[2] if len(parts) == 3 else "", headers


def _receiveBody(
    connection: Any, initial: bytes, headers: dict[str, list[str]], maximumBytes: int, mark: Any
) -> bytes:
    data = bytearray(initial)
    if not initial:
        mark("dataTransferStarted")
    transferEncoding = ",".join(headers.get("transfer-encoding", [])).casefold()
    contentLength = next(iter(headers.get("content-length", [])), None)
    if "chunked" in transferEncoding:
        return _decodeChunked(connection, data, maximumBytes)
    expected = int(contentLength) if contentLength is not None else None
    while expected is None or len(data) < expected:
        chunk = connection.recv(min(65536, (expected - len(data)) if expected else 65536))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > maximumBytes:
            raise ValueError("Feed response exceeded maximumBytes")
    return bytes(data[:expected] if expected is not None else data)


def _decodeChunked(connection: Any, buffered: bytearray, maximumBytes: int) -> bytes:
    source = bytes(buffered)
    output = bytearray()
    while True:
        while b"\r\n" not in source:
            source += connection.recv(65536)
        sizeLine, source = source.split(b"\r\n", 1)
        size = int(sizeLine.split(b";", 1)[0], 16)
        if size == 0:
            return bytes(output)
        while len(source) < size + 2:
            source += connection.recv(65536)
        output.extend(source[:size])
        source = source[size + 2:]
        if len(output) > maximumBytes:
            raise ValueError("Feed response exceeded maximumBytes")


def _decodeBody(body: bytes, headers: dict[str, list[str]]) -> bytes:
    encoding = ",".join(headers.get("content-encoding", [])).casefold()
    return gzip.decompress(body) if "gzip" in encoding else body


def defaultJobHandlers(databaseUrl: str) -> dict[str, Any]:
    """Build process-safe handlers without exposing database credentials in job arguments."""
    return {FEED_WITNESS_HANDLER: partial(evaluateStaleFeeds, databaseUrl)}

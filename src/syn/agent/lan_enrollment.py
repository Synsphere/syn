"""Operator-signed LAN advertisements and route-bound confidential pledges."""

from __future__ import annotations

import hashlib
import time

from syn.operator.adoption_state import EnrollmentJournal
from syn.operator.custody import _agent_key_lock
from syn.operator.enrollment import OFFER_DOMAIN
from syn.operator.service import OperatorServer
from syn_client.lan_enrollment import (
    ADVERTISEMENT_DOMAIN,
    ADVERTISEMENT_PROFILE,
    private_endpoint,
    verify_advertisement,
)
from syn_client.operator_protocol import SynProtocolError, canonical, exact

from .enrollment import _operator_pulse, create_offer, open_offer

SUBMISSION_PROFILE = "syn.lan-pledge-submission/1"


def advertise(config_path, *, host, port):
    endpoint = private_endpoint(host, port)
    server = OperatorServer(config_path)
    with _agent_key_lock(
        server.directory, server.signer.service, server.signer.account + ".operator"
    ):
        return _signed_advertisement(server, endpoint)


def _signed_advertisement(server, endpoint):
    body = {
        "profile": ADVERTISEMENT_PROFILE,
        "endpoint": endpoint,
        "pulse": _operator_pulse(server),
    }
    signature = server.signer.sign(ADVERTISEMENT_DOMAIN + canonical(body)).hex()
    return {"advertisement": body, "signature": signature}


def _digest(advertisement):
    return hashlib.sha256(ADVERTISEMENT_DOMAIN + canonical(advertisement)).hexdigest()


def submit_offer(config, directory, advertisement, *, host, port):
    now = int(time.time())
    selected = verify_advertisement(
        advertisement,
        expected_operator=config["expected_operator"],
        network_id=config["network_id"],
        host=host,
        port=port,
        now=now,
    )
    offer = create_offer(
        config, directory, selected["pulse"], now=now, advertisement_sha256=_digest(advertisement)
    )
    return {"profile": SUBMISSION_PROFILE, "route": advertisement, "offer": offer}


def validate_submission(config_path, value, *, host, port):
    server = OperatorServer(config_path)
    with _agent_key_lock(
        server.directory, server.signer.service, server.signer.account + ".operator"
    ):
        _, pledge, _ = _checked_submission(server, value, host=host, port=port, now=int(time.time()))
        return {
            "profile": "syn.lan-pledge-validation/1",
            "pledge_offer_sha256": offer_digest(pledge),
            "pending_persisted": False,
            "agent_activation_authorized": False,
        }


def offer_digest(pledge):
    return hashlib.sha256(OFFER_DOMAIN + canonical(pledge["offer"])).hexdigest()


def _checked_submission(server, value, *, host, port, now):
    submission = exact(value, {"profile", "route", "offer"}, "LAN pledge submission")
    if submission["profile"] != SUBMISSION_PROFILE:
        raise SynProtocolError("Unknown LAN pledge submission profile")
    advertisement = submission["route"]
    route = verify_advertisement(
        advertisement,
        expected_operator=server.expected_hash,
        network_id=server.config["network_id"],
        host=host,
        port=port,
        now=now,
    )
    offer = exact(submission["offer"], {"pulse", "header", "ciphertext"}, "LAN pledge offer")
    if canonical(offer["pulse"]) != canonical(route["pulse"]):
        raise SynProtocolError("LAN pledge substitutes the advertised pulse")
    authority = server.state.read()
    EnrollmentJournal(
        server.directory, server.signer.service, server.signer.account, server.expected_hash
    ).read()
    pulse, pledge = open_offer(server, offer, now=now, advertisement_sha256=_digest(advertisement))
    return pulse, pledge, authority

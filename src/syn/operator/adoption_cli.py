"""Explicit, exclusive operator maintenance for canonical Agent enrollment."""
from __future__ import annotations

import time

from syn_client.operator_protocol import SynProtocolError, exact
from .adoption_state import EnrollmentJournal
from .custody import _agent_key_lock
from .service import OperatorServer


def approve_selection(selected):
    from .unlock import _private_terminal
    with _private_terminal() as terminal:
        terminal.write("Assign one selected leaf to an independently provisioned Agent.\n")
        terminal.write("Device policy: host-bound virtual device; no hardware attestation.\n")
        terminal.write("Selected public-key hash: " + selected["selected_public_key_hash"] + "\n")
        phrase = "ADOPT " + selected["offer_sha256"][:12] + " " + str(selected["independence"])
        terminal.write("Type " + phrase + " to authorize this one assignment: ")
        terminal.flush()
        return terminal.readline(128).strip() == phrase


def operate(config_path, operation, payload=None, *, approve=approve_selection):
    from syn.agent.enrollment import initialize_operator_enrollment, operator_pulse, open_offer, reserve_offer
    from .enrollment import terminal_approval
    server = OperatorServer(config_path)
    if operation == "adoption-init":
        return initialize_operator_enrollment(server)
    if operation == "agent-pulse":
        return operator_pulse(config_path)
    if operation == "agent-reserve":
        request = exact(payload, {"offer", "operator_byte"}, "reservation request")
        return reserve_offer(config_path, request["offer"], request["operator_byte"], approve=terminal_approval)
    with _agent_key_lock(server.directory, server.signer.service, server.signer.account + ".operator"):
        server.state.read(recover=True)
        journal = EnrollmentJournal(server.directory, server.signer.service, server.signer.account, server.expected_hash)
        journal.read(recover=True)
        now = int(time.time())
        from syn.agent.adoption import grant_selected_key, seal_selected_key, confirm_selected_key
        if operation == "agent-grant":
            request = exact(payload, {"offer", "candidates", "selection"}, "selected enrollment")
            signed_pulse, pledge = open_offer(server, request["offer"], now=now)
            selected = journal.select(server=server, signed_pulse=signed_pulse, signed_offer=pledge,
                signed_candidates=request["candidates"], signed_selection=request["selection"], now=now, approve=approve)
            return grant_selected_key(selection=selected, authorizer=journal, signer=server.signer, ledger=journal, now=now)
        if operation == "agent-seal":
            request = exact(payload, {"grant", "request"}, "selected key transfer")
            return seal_selected_key(grant_packet=request["grant"], request_packet=request["request"], signer=server.signer,
                                     authorizer=journal, ledger=journal, now=now)
        if operation == "agent-confirm":
            request = exact(payload, {"grant", "key_proof"}, "selected key confirmation")
            return confirm_selected_key(grant_packet=request["grant"], key_proof=request["key_proof"], signer=server.signer,
                authorizer=journal, ledger=journal, journal=journal, now=now)
        if operation == "agent-group":
            from syn.agent.group import seal_group_key
            request = exact(payload, {"grant", "request", "group_id", "group_epoch"}, "initial group assignment")
            return seal_group_key(server=server, journal=journal, grant_packet=request["grant"], request_packet=request["request"],
                identifier=request["group_id"], epoch=request["group_epoch"], now=now, approve=approve_group)
    raise SynProtocolError("Unknown canonical Agent enrollment operation")


def approve_group(decision):
    from .unlock import _private_terminal
    with _private_terminal() as terminal:
        terminal.write("Assign only the current group epoch key; no parent derivation authority.\n")
        phrase = "GROUP " + decision["group_id"] + " " + str(decision["group_epoch"]) + " " + decision["selected_public_key_hash"][:12]
        terminal.write("Type " + phrase + " to authorize: ")
        terminal.flush()
        return terminal.readline(128).strip() == phrase

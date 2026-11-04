"""Exercise installed LAN enrollment without giving a sandbox an Internet socket."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import subprocess
import sys
import termios
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

from syn.agent.lan_transport import deliver, discover
from syn.operator import lan_inbox
from syn.operator.lan_transport import serve
from syn_client.operator_protocol import SynProtocolError, canonical, parse

HOST = "192.168.77.1"
AGENT = "192.168.77.2"
SUBNET = "192.168.77.0/24"
PORT = 45121


def prepare_network():
    if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        raise AssertionError("LAN qualification must run in a disposable network namespace")
    for args in (
        ("link", "set", "lo", "up"),
        ("link", "add", "syn-lan0", "type", "veth", "peer", "name", "syn-lan1"),
        ("addr", "add", HOST + "/24", "brd", "+", "dev", "syn-lan0"),
        ("addr", "add", AGENT + "/24", "brd", "+", "dev", "syn-lan0"),
        ("link", "set", "syn-lan1", "up"),
        ("link", "set", "syn-lan0", "up"),
    ):
        subprocess.run(["ip", *args], check=True, timeout=5)


@contextmanager
def listener(config):
    ready, stopped = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as worker:
        running = worker.submit(
            serve,
            config,
            host=HOST,
            port=PORT,
            subnet=SUBNET,
            ready=ready.set,
            stopped=stopped.is_set,
        )
        try:
            if not ready.wait(5):
                if running.done():
                    running.result()
                raise AssertionError("Installed LAN listener failed to start")
            yield
        finally:
            stopped.set()
        running.result()


def _operator_argv(operation, config, offer, *flags):
    return [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "syn.operator",
        operation,
        "--config",
        str(config),
        "--offer-sha256",
        offer,
        *flags,
    ]


def _approved(argv, prompt):
    """Supply only an ephemeral fixture's public approval phrase to its own PTY."""
    master, slave = pty.openpty()

    def foreground():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpgrp())

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(slave,),
            preexec_fn=foreground,
        )
        try:
            terminal = bytearray()
            deadline = time.monotonic() + 12
            while process.poll() is None and time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                terminal.extend(os.read(master, 2048))
                if len(terminal) > 8192:
                    raise AssertionError("Operator approval prompt exceeded its bound")
                match = prompt.search(terminal)
                if match:
                    os.write(master, match[1] + b"\n")
                    out, _ = process.communicate(timeout=8)
                    if process.returncode == 0:
                        return parse(out)
                    break
            raise AssertionError(
                "Installed operator command did not accept its private approval phrase"
            )
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
    finally:
        os.close(slave)
        os.close(master)


def enroll(node, config, pin, uid):
    public_config = {"expected_operator": pin, "network_id": "r04-fixture"}
    scope = {"local_address": AGENT, "subnet": SUBNET}
    with listener(config):
        if uid == 53102:
            try:
                discover({**public_config, "expected_operator": "0" * 64}, port=PORT, **scope)
            except SynProtocolError:
                pass
            else:
                raise AssertionError("LAN discovery accepted an incorrect independent pin")
        advertisement = discover(public_config, port=PORT, **scope)
        # The host handles only public route metadata and encrypted wire bytes.
        packet = node.command(
            "offer",
            {
                "profile": "syn.lan-enrollment-request/1",
                "advertisement": advertisement,
                "host": HOST,
                "port": PORT,
            },
        )
        pending = deliver(public_config, packet, **scope)
        assert pending["pending_persisted"] is True
        assert pending["agent_activation_authorized"] is False
    with listener(config):
        assert deliver(public_config, packet, **scope) == pending

    offer = pending["pledge_offer_sha256"]
    assert any(row["pledge_offer_sha256"] == offer for row in lan_inbox.pending(config)["pending"])
    reserve_argv = _operator_argv("lan-reserve", config, offer, "--operator-byte", str(uid % 256))
    refused = subprocess.run(
        reserve_argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=12,
        start_new_session=True,
    )
    assert refused.returncode == 2 and not refused.stdout, "Reservation proceeded without a TTY"
    candidate_prompt = re.compile(
        rb"Type (RESERVE " + offer[:12].encode() + rb" [0-9a-f]{6}) to approve:"
    )
    candidates = _approved(reserve_argv, candidate_prompt)
    assert candidates["candidates"]["pledge_offer_sha256"] == offer
    selection = node.command("choose", candidates)

    path = config.parent / ("lan-selection-" + str(uid) + ".json")
    with open_private(path) as request:
        request.write(canonical({"candidates": candidates, "selection": selection}))
    try:
        grant_argv = _operator_argv("lan-grant", config, offer, "--request", str(path))
        refusal = subprocess.run(
            grant_argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=12,
            start_new_session=True,
        )
        assert refusal.returncode == 2 and not refusal.stdout, "Adoption proceeded without a TTY"
        grant = _approved(
            grant_argv,
            re.compile(
                rb"Type (ADOPT "
                + offer[:12].encode()
                + rb" [1-8]) to authorize this one assignment:"
            ),
        )
    finally:
        path.unlink()
    return grant, {
        "offer_sha256": offer,
        "pending_retry_after_listener_restart": True,
        "operator_reservation_required_tty": True,
        "operator_adoption_required_separate_tty": True,
        "wrong_operator_pin_refused": uid == 53102,
    }


def open_private(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")

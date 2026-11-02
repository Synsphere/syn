"""Real private-terminal behavior; fixed public input, no native credential access."""
from __future__ import annotations

import errno
import json
import os
import select
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native controlling terminals")


def _terminal_process(program, *, response=None):
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()

    def controlling_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpgrp())

    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", program],
        stdin=slave, stdout=slave, stderr=slave, preexec_fn=controlling_terminal,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    part = os.read(master, 4096)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not part:
                    break
                output.extend(part)
                assert len(output) < 16384
                if response is not None and b"Private probe: " in output:
                    os.write(master, response)
                    response = None
            elif process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0, output.decode(errors="replace")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)


def test_real_foreground_duplex_terminal_opens_and_password_is_not_echoed():
    output = _terminal_process(
        """
import json
from syn.operator.unlock import _private_terminal, _password
with _private_terminal() as terminal:
    value = _password("Private probe: ", terminal)
    print("RESULT=" + json.dumps({"accepted": value == "public-test-phrase", "tty": terminal.isatty()}), flush=True)
""",
        response=b"public-test-phrase\n",
    )
    assert b"public-test-phrase" not in output
    assert json.loads(output.split(b"RESULT=", 1)[1].strip()) == {"accepted": True, "tty": True}


def test_background_process_group_is_refused_before_prompting():
    output = _terminal_process(
        """
import os
from syn.operator import unlock
from syn_client.operator_protocol import SynProtocolError
unlock.os.tcgetpgrp = lambda descriptor: os.getpgrp() + 1
try:
    unlock._private_terminal()
except SynProtocolError:
    print("BACKGROUND_REFUSED", flush=True)
else:
    raise AssertionError("Background process admitted")
"""
    )
    assert b"BACKGROUND_REFUSED" in output


def test_missing_controlling_terminal_is_refused_before_prompting():
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", """
from syn.operator.unlock import _private_terminal
from syn_client.operator_protocol import SynProtocolError
try:
    _private_terminal()
except SynProtocolError:
    print("NO_TERMINAL_REFUSED")
else:
    raise AssertionError("Missing controlling terminal admitted")
"""],
        stdin=subprocess.DEVNULL, capture_output=True, start_new_session=True, timeout=15,
    )
    assert result.returncode == 0
    assert b"NO_TERMINAL_REFUSED" in result.stdout

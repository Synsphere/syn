"""Interactive first-run setup and a supervised native operator for syn-server.

Only the existing initialize() function creates keys. Nothing regenerates an
identity after partial provisioning, a locked keystore, or an invalid config.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time


def operator_path(explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    configured = os.environ.get("SYN_OPERATOR_CONFIG")
    if configured:
        return Path(configured).expanduser().absolute()
    from .operator.custody import native_home
    return native_home()/".local/state/syn/operator/operator.json"


def ensure_operator(explicit=None):
    path = operator_path(explicit)
    from .operator.service import initialize, load_config
    if path.exists() or path.is_symlink():
        load_config(path)  # Reject invalid, unsafe or mismatched existing identity.
        print(f"Using existing Syn operator: {path}", flush=True)
        return path
    if path.name != "operator.json":
        raise RuntimeError("A new operator configuration must be named operator.json")
    if path.parent.exists() and any(path.parent.iterdir()):
        raise RuntimeError(f"Operator setup is incomplete or ambiguous at {path.parent}; refusing to replace identity/state")
    print(f"No operator is provisioned at {path}. Starting operator setup.", flush=True)
    if not sys.stdin.isatty():
        raise RuntimeError("First-run operator setup needs a local interactive terminal. Run uv run syn-server there, "
                           "or select a provisioned --operator-config. --api-only explicitly disables operator mode.")
    print("A new identity will be stored in your native OS keystore. Existing identities will not be replaced.", flush=True)
    try:
        answer = input("Create a NEW operator identity here? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise RuntimeError("Operator setup cancelled; no identity was created")
        network = input("Operator network identifier [syn]: ").strip() or "syn"
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError("Operator setup cancelled; no identity was created") from None
    # Make every newly created ancestor private; never chmod an existing one.
    missing = []
    cursor = path.parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    account = "node-"+hashlib.sha256(os.fsencode(path.parent)).hexdigest()[:24]
    try:
        initialize(path.parent, service="syn.operator", account=account, network_id=network)
    except Exception as error:
        raise RuntimeError("Operator setup could not complete. Check/unlock the native OS keystore with syn-operator unlock. "
                           "An incomplete installation is retained; no keys are silently replaced.") from error
    load_config(path)
    print(f"Operator identity provisioned: {path}", flush=True)
    return path


def ensure_database_config(path):
    """Create only a missing config, never overwrite a user's existing settings."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        return
    print(f"No Syn database configuration at {path}.", flush=True)
    url = os.environ.get("SYN_DATABASE_URL")
    if not url:
        if not sys.stdin.isatty():
            raise RuntimeError("Create config.toml with [database].url, or set SYN_DATABASE_URL, before unattended startup")
        print("Enter an existing PostgreSQL database URL; Syn will install/migrate its schema, not install PostgreSQL.", flush=True)
        try:
            url = getpass.getpass("PostgreSQL URL (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("Database setup cancelled") from None
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not parsed.database:
        raise ValueError("Syn requires a PostgreSQL URL with a database name")
    text = "[database]\nurl = "+json.dumps(url)+"\nautoMigrate = true\necho = false\n"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    print(f"Created private database configuration: {path}", flush=True)


# Use ONLY native Linux locations: WSL may import the Windows PATH, where a
# Windows uv.exe or script named `uv` can be found but cannot run under sh.
# Never install binaries automatically or fall back to Windows custody.
_WSL_UV_COMMAND = (
    'set -eu\n'
    'cd -- "$1"\n'
    'shift\n'
    'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/snap/bin"\n'
    'export UV_PROJECT_ENVIRONMENT=.venv-wsl\n'
    'uv_path="$(command -v uv || true)"\n'
    'if [ -z "$uv_path" ] || [ ! -x "$uv_path" ]; then\n'
    '  printf "%s\\n" "No executable Linux uv found in WSL. Install uv inside WSL " "(not Windows), then retry syn-server." >&2\n'
    '  exit 127\n'
    'fi\n'
    'if ! "$uv_path" --version >/dev/null 2>&1; then\n'
    '  printf "%s\\n" "WSL uv failed its executable check at: $uv_path" "Install or repair the Linux uv binary inside WSL." >&2\n'
    '  exit 126\n'
    'fi\n'
    'exec "$uv_path" run "$@"\n'
)


def handoff_windows(options):
    """Run native operator custody inside installed WSL; never install software."""
    if platform.system() != "Windows" or options.api_only:
        return False
    executable = shutil.which("wsl.exe")
    if executable is None:
        raise RuntimeError("Operator mode requires an installed WSL distribution (or native Linux/macOS). WSL was not found.")
    def translate(path):
        result = subprocess.run([executable, "--exec", "wslpath", "-a", str(Path(path).absolute())],
            check=True, capture_output=True, text=True, timeout=15)
        translated = result.stdout.strip()
        if not translated.startswith("/") or "\n" in translated:
            raise RuntimeError("Could not translate the worktree path for WSL")
        return translated
    cwd, config = translate(Path.cwd()), translate(options.config)
    args = ["syn-server", "--config", config, "--host", options.host, "--port", str(options.port),
            "--log-level", options.logLevel]
    if options.operator_config is not None:
        args += ["--operator-config", translate(options.operator_config)]
    print("Starting native Syn operator setup inside WSL. WSL must have uv, PostgreSQL access and an unlocked Secret Service.", flush=True)
    # Positional shell arguments are quoted, never interpolated. A separate venv
    # prevents Linux uv from replacing the Windows worktree's .venv.
    command = _WSL_UV_COMMAND
    result = subprocess.run([executable, "--exec", "sh", "-c", command, "syn-wsl", cwd, *args], check=False)
    if result.returncode:
        raise RuntimeError(f"Native WSL syn-server exited with status {result.returncode}; no fallback identity was created")
    return True


class ManagedOperator:
    """One operator thread; the API's scheduler exclusively owns node awareness."""
    def __init__(self, path):
        from .operator.service import OperatorServer
        self.server = OperatorServer(path)
        self.thread = None
        self.fault = None

    def _serve(self):
        try:
            self.server.serve(awareness_managed=True)
        except BaseException as error:
            self.fault = error

    def check(self):
        if self.fault is not None:
            raise RuntimeError("Native operator stopped; API readiness is fenced") from self.fault
        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError("Native operator is not running")

    def start(self):
        from syn_client.operator import operator_status
        from .release import runtimeContract
        self.thread = threading.Thread(target=self._serve, name="syn-operator", daemon=True)
        self.thread.start()
        try:
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                self.check()
                if self.server.path.exists():
                    # Readiness is authenticated, not merely a socket/file existence test.
                    try:
                        response = operator_status(socket_path=self.server.path, expected_hash=self.server.expected_hash,
                            expected_version=runtimeContract().runtime_version,
                            configuration_sha256=self.server.config_sha, owner_uid=os.getuid())
                    except (FileNotFoundError, ConnectionRefusedError):
                        time.sleep(.05)
                        continue
                    if (response["status"]["boot_id"] != self.server.boot_id
                            or response["status"]["pid"] != os.getpid()):
                        raise RuntimeError("A different operator already owns the endpoint")
                    return
                time.sleep(.05)
            raise RuntimeError("Operator did not produce authenticated readiness")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.server.stopped.set()
        if self.thread is not None:
            self.thread.join(30)
            if self.thread.is_alive():
                raise RuntimeError("Native operator did not shut down")

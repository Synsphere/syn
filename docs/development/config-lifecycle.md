# Syn configuration upgrades and recovery

A Syn `config.toml` is an **override file**, not a feature manifest. Values
omitted from a valid older configuration inherit the current application
defaults. QUIC, WebRTC Direct, WebTransport, WebSocket, DNS4, mDNS, UPnP, and
Amino remain enabled by default. An explicit `false` remains an opt-out; Syn
must never silently discard an administrator's settings or replace credentials.

## Non-destructive preflight

From the complete source checkout in the Linux/WSL environment:

```sh
uv run --locked syn-server --check-config --config /home/syn/.local/state/syn/config.toml
```

Select the actual path used by the running Syn instance. `--check-config`
loads the full effective configuration and prints a JSON summary of feature
switches. It starts no services, opens no database connection, and cannot
create an Operator. Database passwords and URLs are not included in its output.
`enabled-not-probed` and `configured-not-probed` do **not** imply live network,
Bluetooth or database availability. Live transport readiness still requires
runtime tests under the actual WSL/network topology.

## Recovery behavior

After a successful configuration load at startup, Syn records the complete,
fully validated config under:

```
${XDG_STATE_HOME:-~/.local/state}/syn/config-recovery/<sha256-of-config-path>/last-good.toml
```

The recovery directory is owner-only (0700) and each copy is private (0600),
because it contains database credentials. Syn never copies operator keys or
`operator.json`. A new snapshot is written atomically **only** after the
current configuration passes full validation; a bad config never overwrites
the last-good snapshot. New schema capabilities take effect through defaults
without rewriting existing valid TOML.

If a later user edit breaks the TOML, ordinary startup **fails safely** and
preserves the current config. It does not try to infer the user's intentions or
silently restore anything. To explicitly recover after inspecting the problem:

```sh
uv run --locked syn-server --restore-config --config /home/syn/.local/state/syn/config.toml
```

This command refuses valid, missing, symlinked or concurrently changed input;
requires a *previously validated* snapshot; saves the damaged file in the
private recovery directory; and atomically publishes the known-good copy.
It prints the preserved damaged-copy path, so an operator can inspect changes
before discarding them. Recovery does not connect to the database, touch the
operator identity, or launch background services.

Snapshots are local to the WSL user and absolute config path; moving a config
or changing the Linux account deliberately does not import an unrelated copy.
If the private recovery directory has unsafe permissions, Syn warns and
continues normal startup **without updating a snapshot**.

## Tests / CI

`tests/test_config_lifecycle.py` runs in the existing Python 3.12/3.13 CI
application job alongside `tests/test_config.py`, `tests/test_wsl_bootstrap.py`
and the other offline suites. It checks old configs inheriting current defaults,
explicit opt-outs, private copies, invalid-file preservation, the explicit
restore path, and prevention of operator setup when config validation fails.
Real Windows-host bridge and public-network readiness are separate integration
tests; the config checker makes no claim to have exercised them.

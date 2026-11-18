# Startup repair after runtime network recovery

This follow-up addresses the startup failures reported after commit
`2c526d17120a94c115391ff51b01886670a2f6d0`. Apply it on top of that version.
It repairs existing runtime behavior and does not introduce new DAG data types.

## Cardano and uvloop

The previous connection change passed `happy_eyeballs_delay` and `interleave`
to `asyncio.open_connection` unconditionally. The installed uvloop rejects
those keywords, producing the reported `TypeError` before any connection can
complete. That unexpected exception becomes a Cardano runtime fault and is
sufficient to trigger the node's readiness fence.

The connector now checks whether the active event loop supports those optional
arguments. On loops that do, it uses the native connection race. On other
loops, it resolves and alternates IPv4/IPv6 addresses, starts bounded staggered
socket attempts and transfers the winning socket to the stream. The existing
overall connection deadline covers resolution and the race. Losing sockets
are closed on success, failure and cancellation.

Local protocol tests exercise connection, handshake and tip queries under
both asyncio and actual uvloop, including a stalled IPv6 attempt followed by
working IPv4. These tests do not establish live Cardano peer availability.

## Windows custody transport

The broker deliberately serializes vault requests. The old pipe client allowed
only 750 milliseconds to connect to that single instance, even when a previous
request was awaiting a native approval dialog. It also discarded the transport
reason when PowerShell exited unsuccessfully. This is a confirmed contention
defect, but the old log cannot prove it was the cause of this particular failure.

Runtime requests now have a separate bounded queue wait of up to 300 seconds,
followed by a 30-second response budget, or 300 seconds for an operation that
can require approval. `sign` receives the approval budget too. The response
budget covers writing, flushing and reading; Python's outer deadline includes
both budgets and process startup/cleanup margin. The launcher's initial health
probe retains its short connection timeout.

The bridge emits fixed diagnostic codes for connection, submission and response
failures. Python reports only recognized codes and never includes arbitrary
PowerShell stderr. A submitted request is never automatically replayed after a
timeout or broken connection: its result may be unknown. Root custody, public
key pinning, native approval and the prohibition on WSL secret fallback remain
in force.

A pending approval can still delay another request or orderly shutdown until
its bounded wait ends. This repair preserves the existing serial broker rather
than introducing concurrent vault access. Native Windows pipe integration tests
are included and require a Windows host; Linux test results do not validate the
PowerShell bridge on the reported machine.

## System D-Bus access after dropping privileges

The bootstrap's private `077` umask could be inherited by the SysV D-Bus start
script. Its creation of the public `/run/dbus` directory could therefore leave
it messagebus-owned with mode `0700`: root could access it, but the non-root Syn
account could not. This matches the reported root/non-root symptom; the actual
directory mode on the reported host has not been inspected here.

Service startup now uses a subshell-local `022` umask. The enclosing Syn process
retains `077`. An existing `/run/dbus` is repaired only for the exact known case:
mode `0700`, the system messagebus owner/group, no explicit filesystem ACL, a
real directory and a stable inode. The helper changes that directory alone to
`0755`. Unexpected permissions or ownership produce a diagnostic and leave
Bluetooth optional; no D-Bus policy, socket or private Syn file is changed.

This repairs bus access, not hardware attachment. BlueZ still requires a
Bluetooth controller visible inside Linux.

## Recovery from a previous failed startup job

An earlier failed Operator DAG job could leave its schedule's failure count
set. A successful synchronous startup resync previously did not clear that
count, so the readiness check could fence the restarted process again.

Startup now reconciles that exact schedule while holding scheduler preparation
ownership. Only a successful current resync records a new succeeded Job and
clears its schedule's failure marker. Previous failed Jobs retain their error
history. Queued or running remnants belonging to that same schedule are marked
as superseded by the new verified Job; other schedules and scopes are untouched.
The next run is scheduled relative to completion instead of immediately
duplicating the startup resync. A failed current resync cannot clear the old
failure, and later failures still trigger readiness fencing.

If startup is canceled, the scheduler waits for its preparation or recovery
worker to finish before releasing the database lease, including when shutdown
requests cancellation repeatedly. This prevents an abandoned worker from
committing recovery changes after another scheduler has taken ownership.

The recovery tests run with default isolated SQLite fixtures, or against a
disposable PostgreSQL database by setting
`SYN_RUNTIME_RECOVERY_TEST_DATABASE_URL` when running
`tests/test_runtime_recovery.py`. The PostgreSQL path creates a unique test
schema per database test and uses the actual Job table and status type.

## Readiness diagnostics

The fence now identifies the failing component and a bounded exception type
chain, for example:

```text
Syn node lost readiness; API is fenced (component=cardano; exception=RuntimeError <- TypeError)
```

These diagnostics exclude raw exception messages and request or peer payloads.
They preserve the existing readiness checks.

## UPnP still requires the correct WSL networking path

The reported `WSL is using nat networking` message is a separate diagnosis.
The enabled DrayTek UPnP setting alone cannot forward through the additional
Windows-to-WSL boundary. This startup repair does not change the host's
networking mode, router or firewall.

Follow [runtime network recovery](runtime-network-recovery.md#wsl-inspect-first-then-enable-mirrored-networking)
to inspect and, on a supported Windows host, enable mirrored mode. The helper
makes a backup of an existing `.wslconfig`. Its setting affects all WSL 2
distributions and takes effect after saving work, stopping those workloads and
running `wsl --shutdown` from Windows PowerShell. Check the active mode again
after restart; a saved configuration is not proof that the running VM changed.

The same guide documents fixed-port forwarding when mirrored networking is
unavailable and the distinction between a verified router mapping and an
externally reachable listener.

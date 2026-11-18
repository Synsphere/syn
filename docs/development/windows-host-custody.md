# Windows-host Syn Operator custody (experimental)

This profile moves the *root Operator authority* out of WSL. On Windows,
`Start-Syn.cmd` / `Start-Syn.ps1` starts a Windows user-process broker backed by
DPAPI (CurrentUser), then starts the existing Syn protocol/node in WSL. The
server's Operator signatures, Agent candidate public hashes, device commitments,
and private group derivation are evaluated on Windows. The broker never has a
`get` or `export` RPC for root entropy or extended private keys. Its named pipe
has an explicit Windows-user DACL and rejects remote clients.

## First launch

1. Start from a full Syn checkout using `Start-Syn.cmd`. The launcher obtains
   Windows `uv` with permission if missing, installs the isolated `host-custody`
   project's pinned dependencies, starts a SID-restricted broker and checks its
   health. Linux `uv`/dependencies remain separately installed in WSL.
2. Syn's ordinary WSL operator setup still requests approval for creating a
   new identity and the logical `network_id`. The Windows host independently
   asks permission for the new root identity. Windows stores the seed only
   inside a user-DPAPI-encrypted, atomic vault beneath `%LOCALAPPDATA%/Syn/HostCustody`.
3. Non-root Syn operational key/value slots are also stored in that Windows
   vault. On WSL the broker-backed keyring adapter replaces the Linux Secret
   Service, including Agent operational slots. Linux-native deployments still
   use Secret Service and macOS retains Keychain.
4. WSL uses the existing scheduler, identity, socket RPC, Events, Embers,
   ledger, and protocols. Network key derivations and signatures preserve the
   pinned Icarus/Ed25519-BIP32 v2 paths and byte formats.

`uv run syn-server` from Windows is routed through `Start-Syn.ps1` as well.
`--api-only` deliberately runs without an Operator or host broker.

## Restarting after a vault sharing violation

Win32 error 32 at `CreateFile(... authority.lock ...)` means another process
still holds a conflicting open handle. The mere existence of the lock file is
normal; deleting it does not resolve ownership. Keep `authority.lock` and
`vault-v1.dpapi` intact. This is a broker startup failure before the WSL node
and its Bluetooth/Cardano services start.

The launcher now uses `uv` to prepare and locate its Windows Python interpreter,
then starts the broker through that interpreter directly. It tracks the actual
lock-owning Python process, drains its output asynchronously, and stops/waits for
that process during cleanup. A broker started by the launcher also watches a
retained handle to its exact PowerShell owner, pinned by PID plus creation
FILETIME. If the launcher/console disappears, the broker exits and Windows
releases the vault and pipe handles. A reused PID is not accepted as a new owner.

An older broker already left running does not acquire the new watcher just
because files were updated. If another Syn launcher is running, close that
instance normally when you intend to stop it. Then inspect remaining brokers
from the checkout root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Stop-SynHostCustody.ps1
```

The helper only lists matching Python broker processes owned by the current
Windows user. Once the displayed PID is identified as the unwanted remaining
broker, stop that specific process, replacing `12345` with its actual PID:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Stop-SynHostCustody.ps1 -ProcessId 12345
.\Start-Syn.cmd
```

Add `-WhatIf` to preview the selected stop. The helper parses Windows command-line
arguments, verifies the exact module and bounded pipe name, checks the current
user SID and process creation time, and retains the process handle across its
checks and stop. It neither kills unrelated Python processes nor changes any
vault files. No process is stopped by the default listing command. If no matching
broker is shown, do not guess a PID; an explicitly launched recovery process or
another application may be holding the file instead.

Concurrent launchers still cannot independently own the same Windows authority.
An occupied vault now produces a concise diagnostic and exit status 3 instead
of an unhandled sharing-violation traceback. Unavailable owner monitoring fails
closed. Normal shutdown, failed initialization and explicit recovery operations
release their vault handles; startup never regenerates or replaces an existing
root identity as a repair action.

Direct interactive `.ps1` runs restore the previous custody bridge environment
variables when they finish, so the shell is not left pointing at that run's
closed pipe. The Windows-specific subprocess/CIM acceptance tests in
`tests/test_host_custody_recovery.py` use disposable sleeping fixture processes
and never access a real DPAPI vault. They require a Windows runner.

## Trust and hardware limitations

- DPAPI **does not imply a non-exportable hardware key**. The Windows Python
  process decrypts root entropy in memory to perform Ed25519-BIP32 derivation.
  This version does not claim TPM sealing, HSM guarantees, constant-time secret
  handling or protection against a compromised Windows account.
- WSL interoperability can launch Windows executables as the same Windows user.
  ACL-only isolation from a compromised WSL account is consequently **not**
  guaranteed. This broker refuses generic raw signing, restricts signing domains,
  and requires a Windows confirmation for sensitive signatures and first use of
  group-epoch keys. Operator status and short-lived enrollment pulse signing
  remain unattended. For stronger assurance, require a separate Windows
  security principal, a separate HSM service, or hardware-backed authorization.
- The current Syn group-sealing protocol needs an epoch **group key** in WSL to
  encrypt it for the chosen Agent. The broker authorizes that disclosure to WSL
  on the Windows desktop. It never discloses *root Operator entropy*, but the
  derived group key is not confined to Windows. A later protocol could move the
  entire group-key sealing procedure into the broker.
- Startup fails closed if the broker, Windows interop or the identity pin is
  unavailable. It never recreates a missing identity in WSL as a fallback.
- A browser-delivered script or altered checkout can still control the
  WSL service; inspect and sign/pin installed releases before production use.
- This code has no Windows hardware acceptance evidence. Qualify real Windows
  pipe DACLs, DPAPI recovery, WSLENV path translation, and signing vectors
  before using with material of high value.

## Existing Linux-held Operator identities and migration

**Do not point an existing Linux operator.json at an empty Windows vault.**
An existing identity must not be replaced, regenerated or silently reclassified.
The broker refuses to use an unmatched base-public-key hash. An explicit Windows
root import from an independently saved 24-word Syn recovery phrase is available:

```powershell
cd .\host-custody
uv run --project . -- python -m syn_host_custody.broker --import-recovery `
  --service syn.operator --account YOUR_EXISTING_ACCOUNT `
  --expected-base-hash YOUR_EXISTING_BASE_PUBLIC_KEY_HASH
```

The recovery phrase is entered directly into the **Windows console** and is not
passed via WSL/IPC, the command line or an environment variable. The broker
re-derives the original public key and verifies the independent pin before
writing a new DPAPI-wrapped root. An export command is also available with
`--export-recovery` and the same service/account/hash arguments, but is Windows
console-only and requires a Windows confirmation.

**Root import alone does not migrate the existing operational state anchor,
Agent credentials, approval history or encrypted checkpoint.** That complete
migration workflow is not automated by this patch. Existing installations
should remain on their original native custody backend until a separately
reviewed state migration is performed. Never delete the original Linux keyring
or state files to "fix" a pin failure. Retain an offline backup of the 24-word
recovery phrase; DPAPI alone does not protect against Windows-profile loss.

## Provider contracts

The WSL `HostHdOperatorSigner` has the same limited operations as Syn's native
`PythonHdOperatorSigner`: public identity, bounded domain-specific signing,
8 public candidate hashes and device commitments; it adds an approved group
key derivation method used by Syn's existing sealing pipeline. The general
host-keyring proxy intentionally rejects every HD root/private slot. The
broker refuses a second identity for the same service/account and holds an
exclusive Windows vault lock. Windows-only recovery is never served via IPC.

There is no network/TCP listener, unrestricted signing endpoint or general key
export operation. All subprocess invocation arguments are bounded and passed
without shell interpolation of request JSON.

## Remaining qualification gates

- Windows 10/11 and WSL 2 installation and named-pipe ACL test as logged-in user
- Headless D-Bus-free first Operator provisioning and three restarts with one hash
- Independent PyCardano vectors (base signature, candidate paths, group HKDF)
- Error handling across process death, suspended Windows account and locked DPAPI
- Agent group rollover, backup/recovery and PostgreSQL integration
- Native macOS/Linux regression, Windows/WSL bootstrap CI and package locking

These must pass before a signed stable Syn release advertises Windows-host
custody as production-qualified.

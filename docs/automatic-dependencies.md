# Syn automatic dependency setup (Windows / WSL)

`Start-Syn.cmd` or `Start-Syn.ps1` launches `scripts/bootstrap-wsl.sh` in the
installed WSL distribution. A Windows `uv run syn-server` also uses that
bootstrap when native operator mode is requested. A clean Debian/Ubuntu WSL
installation can provision its prerequisites on first use, without requiring
manual `apt`, `uv`, D-Bus, keyring or PostgreSQL commands.

## What it installs and why

* OS packages, when missing: `ca-certificates`, `curl`, `python3`, `dbus`,
  `dbus-user-session`, `gnome-keyring`, `libsecret-tools`, `sudo`, `iw`,
  `bluez`, `libusb-1.0-0` and `usbutils` for basic radio discovery. These are
  installed through the distro's signed package manager, not from unknown
  downloaded scripts. If a new *local* database is needed, also install
  `postgresql` and `postgresql-client`.
* Linux `uv` is installed per user from a versioned official HTTPS installer
  only if no valid native Linux `uv` is already available. Syn never executes
  a Windows program found on WSL's inherited `PATH`.
* `uv run --extra position-native` resolves and installs the project's Python
  dependencies in an isolated virtual environment under the Linux user's home.
  The Windows `.venv` is not shared.
* A D-Bus session and GNOME Keyring Secret Service are started for the node.
  The operator supplies a **nonempty** existing keyring password, or chooses a
  new encrypted keyring passphrase of at least 12 characters. Neither a blank
  password nor a plaintext-fallback keyring is permitted.
* If no database config and no `SYN_DATABASE_URL` exist, Syn starts the default
  existing PostgreSQL cluster (creating it through the distro package when
  initially installed). It creates only an absent local `syn_<username>`
  database and a matching login role; connections use Unix-socket peer auth.
  The generated private config lives in the Linux home, not the Windows volume.
  Syn tests Unix-socket peer authentication as the operator account and refuses
  to seize an existing database owned by a different role.

## First-run consent and security

Installing OS packages requires approval and `sudo` or root. If the WSL distro
starts as root, Syn picks its sole existing non-root human user or offers to
create a `syn` user; it **never runs operator custody as root**. Syn does not
silently modify WSL's default user, replace existing identities, wipe keyrings,
change PostgreSQL authentication policy, or overwrite an existing configuration.

The operator setup still explicitly asks whether to create a **new Syn identity**
and which network identifier to use. These are security-sensitive choices, not
ordinary software dependencies. The `syn` default network is fine for a first
installation.

`SYN_BOOTSTRAP_OFFLINE=1` disallows network installation. Existing fully
provisioned dependencies can run offline. Explicit database URLs or config
files are used as provided rather than overwritten; a broken existing database
is reported as an error instead of being replaced.

## Supported environments

The automatic system-package provider is currently for Ubuntu and Debian WSL.
Other Linux distros need an OS-package provider. The Windows host must have WSL
and at least one Linux distribution installed; first-time Windows feature
installation may require administrator approval and a reboot. Secrets setup
requires an interactive console for a first-time password or unlock; fully
unattended first-run identity creation is deliberately unsupported.

Radio hardware drivers and vendor firmware are not all interchangeable or
available through an OS package manager. Optional device-specific packages
remain dependent on detected hardware and supported provider adapters.

## Starting and recovery

Double-click `Start-Syn.cmd`, or from PowerShell:

```powershell
.\Start-Syn.ps1
```

On a clean system follow the **one-time** system-package/user/keyring/identity
prompts. Afterwards the same launcher starts the operator, API and the scheduled
position, clock and radio discovery work.

If a step fails, fix the reported service or permission issue and relaunch.
Previously provisioned keys, databases, and configuration are preserved. No
`git reset`, `git clean`, or identity regeneration is required.

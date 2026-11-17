# Start Syn on Windows with one launcher

Run `Start-Syn.cmd` (double-click) or `./Start-Syn.ps1` in PowerShell from a
**source checkout**. Both run the native Linux operator + API inside the default
WSL distribution. `-ApiOnly` explicitly skips the operator. `-Distribution Ubuntu`
selects a different installed WSL distribution. No Windows Python/uv is required.

The launch sequence is:

1. Verify that WSL and its Linux distribution can run `sh` and `wslpath`. If WSL
   is absent, Windows may require a one-time **administrator-approved**
   `wsl --install` and restart; Syn does not silently elevate or reboot.
2. Keep WSL's `PATH` restricted to Linux locations. Reuse a working native
   Linux `uv` or download the versioned official installer from
   `https://astral.sh/uv/0.13.8/install.sh`, install for the current **Linux
   user only**, without `sudo` or shell-profile edits. The executable is checked
   before use. `SYN_BOOTSTRAP_OFFLINE=1` disables download (for offline testing).
3. Create/reuse a per-checkout Linux virtual environment under
   `~/.local/state/syn/venvs/` rather than using Windows' `.venv`. Run
   `uv run --extra position-native syn-server`, which installs project
   dependencies as needed.
4. Syn's existing first-run routine confirms operator identity creation, never
   overwrites partial or locked identity state, and prompts for a PostgreSQL URL
   if no config exists. Syn creates/migrates its schema, then starts operator,
   clock, position, radio awareness and API services.

Existing `uv run syn-server` on Windows now uses the **same** Linux bootstrap
script, so a missing WSL `uv` is repaired there too.

## What is and isn't automatic

The launcher automatically handles Python/uv and normal project dependency
installation inside an already installed WSL distribution. It **does not**
automatically create or replace key material, install PostgreSQL, install a WSL
distribution, or bypass a locked Secret Service. These operations need the
existing setup prompts or OS administrator action. For Ubuntu, if no PostgreSQL
or Secret Service is installed, install/configure those OS services first;
operator secrets stay in the Linux user keystore, not the Windows worktree.

The Linux installer runs a version-specific script retrieved over HTTPS from
Astral. The download uses TLS but is not independently signature-verified by
this launcher. Review or pin the source according to your deployment policy.

If Syn exits, `Start-Syn.cmd` keeps its window open with the actual error. Neither
launcher changes Git branches, deletes configuration, or force-resets an identity.

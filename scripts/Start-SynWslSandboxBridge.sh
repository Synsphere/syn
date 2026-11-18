#!/usr/bin/env bash
# Start the WSL-local IGD, optionally mDNS/approved other multicast groups.
# The WSL-side bridge secret is readable only by the dedicated synbridge user.
set -euo pipefail
if ! command -v ip >/dev/null || ! command -v python3 >/dev/null; then
    echo 'iproute2 and python3 are required.' >&2
    exit 1
fi
if ! id synbridge >/dev/null 2>&1; then
    echo 'Run Set-SynWslSandboxBridge.ps1 -Action Provision from Windows first.' >&2
    exit 1
fi
host_ip="$(ip -4 route show default | awk '/^default via / {print $3}' | sort -u)"
if [[ ! "$host_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'Expected one Windows WSL default gateway IPv4 address.' >&2
    exit 1
fi
sandbox_ip="$(ip -4 route get "$host_ip" | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | sort -u)"
if [[ ! "$sandbox_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'Expected one WSL IPv4 source address.' >&2
    exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# No root access or Syn custody key is required at runtime. Any positional
# extra flags (--mdns, --multicast-group ...) are validated by the Python CLI.
exec sudo -u synbridge -- python3 "$script_dir/syn_wsl_sandbox_igd.py" \
    --sandbox-ip "$sandbox_ip" --host-ip "$host_ip" \
    --secret /var/lib/syn-wsl-bridge/secret.hex "$@"

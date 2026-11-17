#!/bin/sh
# Syn's native Linux bootstrap. Invoked by Start-Syn.ps1/.cmd or Windows handoff.
# Never uses a Windows executable, runs as the existing WSL user, and does not
# create/replace keystore identities or database credentials.
set -eu
umask 077

syn_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd -- "$syn_root"

# WSL often appends Windows executable directories to PATH. Never inherit them.
PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/snap/bin"
export PATH

find_linux_uv() {
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" \
                     /usr/local/bin/uv /usr/bin/uv /snap/bin/uv; do
        if [ -f "$candidate" ] && [ -x "$candidate" ] && \
           "$candidate" --version >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

uv_bin=$(find_linux_uv || true)
if [ -z "$uv_bin" ]; then
    if [ "${SYN_BOOTSTRAP_OFFLINE:-0}" = 1 ]; then
        echo 'Syn: Linux uv is missing, and SYN_BOOTSTRAP_OFFLINE=1 prevents installation.' >&2
        exit 127
    fi

    # Versioned official installer, no sudo, no shell-profile modification.
    # HTTPS authenticates the download; a detached-signature verification is not
    # currently provided by this bootstrap. Review the installer if required.
    uv_version=0.13.8
    installer_url="https://astral.sh/uv/$uv_version/install.sh"
    temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/syn-uv.XXXXXXXX")
    trap 'rm -rf -- "$temp_dir"' 0
    trap 'exit 130' 2
    trap 'exit 143' 15
    echo "Syn: installing Linux uv $uv_version for this WSL user from $installer_url"
    if command -v curl >/dev/null 2>&1; then
        if ! curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
             "$installer_url" --output "$temp_dir/install.sh"; then
            echo 'Syn: could not download the official Linux uv installer.' >&2
            exit 69
        fi
    elif command -v wget >/dev/null 2>&1; then
        if ! wget --https-only --quiet "$installer_url" -O "$temp_dir/install.sh"; then
            echo 'Syn: could not download the official Linux uv installer.' >&2
            exit 69
        fi
    else
        echo 'Syn: curl or wget is required to install uv in WSL.' >&2
        exit 69
    fi
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$temp_dir/install.sh"
    uv_bin=$(find_linux_uv || true)
    if [ -z "$uv_bin" ]; then
        echo 'Syn: Linux uv installation did not produce a working executable.' >&2
        exit 126
    fi
fi

# Keep all Linux wheels and virtual environments in the Linux filesystem, not
# the Windows checkout's .venv. A stable, per-checkout namespace avoids sharing
# environments between unrelated Syn installations.
if command -v sha256sum >/dev/null 2>&1; then
    project_id=$(printf '%s' "$syn_root" | sha256sum | cut -c1-20)
else
    echo 'Syn: sha256sum is required to isolate the WSL virtual environment.' >&2
    exit 69
fi
venv_home="$HOME/.local/state/syn/venvs"
mkdir -p "$venv_home"
export UV_PROJECT_ENVIRONMENT="$venv_home/$project_id"
printf 'Syn: Linux uv at %s; environment %s\n' "$uv_bin" "$UV_PROJECT_ENVIRONMENT"
printf 'Syn: syncing dependencies and starting operator/API services...\n'

# syn-server continues the existing interactive operator and PostgreSQL setup.
# The awareness scheduler and schema migrations are handled by Syn itself.
exec "$uv_bin" run --project "$syn_root" --extra position-native -- syn-server "$@"

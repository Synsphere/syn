#!/bin/sh
# Native WSL bootstrap for Syn. Never use Windows tools for Linux custody.
# Installs missing OS/Python dependencies, starts a local database when needed,
# and opens a real, password-protected Secret Service for the node session.
set -eu
umask 077

syn_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
script="$syn_root/scripts/bootstrap-wsl.sh"
cd -- "$syn_root"
PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
export PATH

fail() { printf 'Syn bootstrap: %s\n' "$*" >&2; exit 1; }
notice() { printf 'Syn bootstrap: %s\n' "$*"; }
confirm() {
    printf '%s [y/N] ' "$1" >&2
    if [ ! -t 0 ]; then
        fail 'An interactive terminal is required for new system packages/accounts. Existing installations can start unattended.'
    fi
    answer=''
    IFS= read -r answer || true
    case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# All inputs are handled as argv; no shell-evaluation of user supplied paths.
config_path="$syn_root/config.toml"
expect_config=0
explicit_config=0
api_only=0
for arg do
    if [ "$expect_config" = 1 ]; then
        config_path="$arg"
        expect_config=0
    elif [ "$arg" = '--api-only' ]; then
        api_only=1
    elif [ "$arg" = '--config' ]; then
        expect_config=1
        explicit_config=1
    fi
done
[ "$expect_config" = 0 ] || fail '--config requires a file path'
case "$config_path" in /*) ;; *) config_path="$syn_root/$config_path" ;; esac

need_local_db=0
if [ ! -e "$config_path" ] && [ ! -L "$config_path" ] && [ -z "${SYN_DATABASE_URL:-}" ]; then
    need_local_db=1
fi

as_root() {
    if [ "$(id -u)" = 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail 'A privileged package/database action is required, but sudo is unavailable. Run WSL setup under an administrator account.'
    fi
}
run_postgres() {
    if [ "$(id -u)" = 0 ]; then
        runuser -u postgres -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -u postgres "$@"
    else
        fail 'PostgreSQL provisioning needs sudo or root.'
    fi
}

install_system_packages() {
    command -v apt-get >/dev/null 2>&1 ||
        fail 'Automatic OS dependency installation currently supports Debian/Ubuntu WSL (apt). Other distributions need an explicit provider.'
    command -v dpkg-query >/dev/null 2>&1 || fail 'Cannot check installed Debian packages.'
    packages='ca-certificates curl python3 dbus dbus-user-session gnome-keyring libsecret-tools sudo passwd util-linux iw bluez libusb-1.0-0 usbutils'
    if [ "$need_local_db" = 1 ]; then
        packages="$packages postgresql postgresql-client"
    fi
    missing=''
    for package in $packages; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
            missing="$missing $package"
        fi
    done
    [ -n "$missing" ] || return 0
    [ "${SYN_BOOTSTRAP_OFFLINE:-0}" != 1 ] || fail "Missing OS packages:$missing (offline mode)."
    notice "Missing OS packages:$missing"
    confirm 'Install missing Debian/Ubuntu packages (sudo/apt-get)?' || fail 'Dependency installation was declined; no identity was created.'
    as_root apt-get update
    # All names come from the fixed, reviewed package manifest above.
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
}

valid_account() {
    # PostgreSQL role and Linux username are later quoted, but accept only a
    # conservative ASCII subset; no shell, SQL, or option characters.
    case "$1" in ''|*[!a-z0-9_-]*|[!a-z_]* ) return 1 ;; esac
    [ "${#1}" -le 24 ]
}

select_nonroot_account() {
    # Never silently move an existing root-owned identity to a different user.
    [ ! -e /root/.local/state/syn/operator/operator.json ] ||
        fail 'Existing root-owned operator identity detected. Do not create a second identity; handle migration explicitly.'
    if [ -L /etc/syn/bootstrap-user ]; then
        fail 'Root-owned Syn account selection must not be a symlink.'
    fi
    if [ -e /etc/syn/bootstrap-user ]; then
        [ "$(stat -c %u /etc/syn/bootstrap-user)" = 0 ] ||
            fail 'Syn account selection has an unexpected owner.'
        user=$(cat /etc/syn/bootstrap-user)
    elif [ -n "${SYN_WSL_USER:-}" ]; then
        user=$SYN_WSL_USER
    else
        # Prefer the distro's existing human user when unambiguous.
        candidates=$(getent passwd | awk -F: '$3>=1000 && $3<60000 && $6 ~ /^\/home\// && $7 !~ /(false|nologin)$/ { print $1 }')
        candidate_count=$(printf '%s\n' "$candidates" | grep -c . || true)
        if [ "$candidate_count" = 1 ]; then
            user=$candidates
        else
            user=syn
        fi
    fi
    valid_account "$user" || fail "Unsafe Linux account identifier: $user"
    if ! id "$user" >/dev/null 2>&1; then
        confirm "Create the non-root Linux user '$user' for Syn custody?" || fail 'A non-root account is required for keyring custody.'
        useradd --create-home --shell /bin/bash "$user"
    fi
    [ "$(id -u "$user")" != 0 ] || fail 'Refusing root as the native operator identity owner.'
    mkdir -p /etc/syn
    if [ ! -e /etc/syn/bootstrap-user ]; then
        printf '%s\n' "$user" > /etc/syn/bootstrap-user
        chmod 0644 /etc/syn/bootstrap-user
    fi
    printf '%s' "$user"
}

ensure_local_database() {
    user=$1
    valid_account "$user" || fail 'Invalid local database role'
    command -v pg_lsclusters >/dev/null 2>&1 || fail 'PostgreSQL cluster manager is missing after installation.'
    cluster=$(pg_lsclusters -h | awk '$2=="main" && $3=="5432" { print $1 " " $2 " " $3; exit }')
    [ -n "$cluster" ] || fail 'No standard local PostgreSQL main cluster on port 5432; use an explicit database URL instead.'
    # This is output from the local PostgreSQL cluster manager, not arbitrary input.
    set -- $cluster
    version=$1; name=$2; port=$3
    case "$version:$name:$port" in *[!a-zA-Z0-9_.:-]*) fail 'Unexpected PostgreSQL cluster metadata' ;; esac
    if ! pg_isready -q -h /var/run/postgresql -p "$port"; then
        notice "Starting existing local PostgreSQL cluster $version/$name."
        as_root pg_ctlcluster "$version" "$name" start
    fi
    pg_isready -q -h /var/run/postgresql -p "$port" || fail 'Local PostgreSQL did not become ready.'

    db="syn_$user"
    # SQL identifiers are restricted above and double-quoted. Never drop or
    # change an existing role/database or relax peer authentication.
    role=$(run_postgres psql -p "$port" -Atqc "SELECT 1 FROM pg_roles WHERE rolname='$user'")
    if [ "$role" != 1 ]; then
        notice "Creating dedicated local PostgreSQL login role: $user"
        run_postgres psql -p "$port" -v ON_ERROR_STOP=1 -c "CREATE ROLE \"$user\" LOGIN" >/dev/null
    fi
    owner=$(run_postgres psql -p "$port" -Atqc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='$db'")
    if [ -z "$owner" ]; then
        notice "Creating local PostgreSQL database: $db"
        run_postgres createdb -p "$port" --owner="$user" "$db"
    elif [ "$owner" != "$user" ]; then
        fail "Database $db belongs to $owner; refusing to take ownership."
    fi
    # Confirm Unix-socket peer authentication works as the intended non-root
    # operator. Never weaken pg_hba.conf or generate a default DB password.
    if [ "$(id -u)" = 0 ]; then
        check=$(runuser -u "$user" -- psql -h /var/run/postgresql -p "$port" -d "$db" -Atqc 'SELECT 1') ||
            fail "Local PostgreSQL peer authentication failed for $user."
    else
        check=$(psql -h /var/run/postgresql -p "$port" -d "$db" -Atqc 'SELECT 1') ||
            fail "Local PostgreSQL peer authentication failed for $user."
    fi
    [ "$check" = 1 ] || fail 'Local PostgreSQL database did not pass its health check.'
    SYN_DATABASE_URL="postgresql+psycopg:///$db?host=/var/run/postgresql&port=$port"
    export SYN_DATABASE_URL
}

# On WSL installations that default to root, put the application and its
# Secret Service in a real non-root user's home. Do not alter WSL's default user.
if [ "$(id -u)" = 0 ]; then
    install_system_packages
    target_user=$(select_nonroot_account)
    if [ "$need_local_db" = 1 ]; then
        ensure_local_database "$target_user"
    fi
    notice "Starting Syn under non-root WSL account: $target_user"
    # A root-owned session bus/runtime directory must not be inherited by the
    # unprivileged node. Preserve SYN_DATABASE_URL but recreate user session IPC.
    exec runuser -u "$target_user" -- env -u DBUS_SESSION_BUS_ADDRESS \
         -u XDG_RUNTIME_DIR -u SYN_BOOTSTRAP_SECRETS_READY \
         -u SYN_BOOTSTRAP_DBUS_ATTEMPTED sh "$script" "$@"
fi

install_system_packages
if [ "$need_local_db" = 1 ]; then
    ensure_local_database "$(id -un)"
    # The default fresh-install config lives in the Linux home directory,
    # not the Windows volume. Explicit --config is still honored below.
fi

find_linux_uv() {
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /usr/local/bin/uv /usr/bin/uv /snap/bin/uv; do
        if [ -f "$candidate" ] && [ -x "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

uv_bin=$(find_linux_uv || true)
if [ -z "$uv_bin" ]; then
    [ "${SYN_BOOTSTRAP_OFFLINE:-0}" != 1 ] || fail 'Linux uv is missing and offline mode disallows downloads.'
    uv_version=0.13.8
    installer_url="https://astral.sh/uv/$uv_version/install.sh"
    temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/syn-uv.XXXXXXXX")
    trap 'rm -rf -- "$temp_dir"' 0
    notice "Installing native Linux uv $uv_version (per-user, no sudo)."
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
         "$installer_url" --output "$temp_dir/install.sh" || fail 'Could not download the official uv installer.'
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$temp_dir/install.sh"
    uv_bin=$(find_linux_uv || true)
    [ -n "$uv_bin" ] || fail 'Linux uv installation did not provide a runnable executable.'
fi

# A headless WSL shell has no session bus until one is created. Keep the bus and
# Secret Service alive for the complete syn-server process, including its jobs.
if [ "$api_only" = 0 ] && [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    runtime_dir="$HOME/.local/state/syn/xdg-runtime"
    [ ! -L "$runtime_dir" ] || fail 'XDG runtime directory must not be a symlink.'
    mkdir -p "$runtime_dir"
    chmod 0700 "$runtime_dir"
    XDG_RUNTIME_DIR="$runtime_dir"
    export XDG_RUNTIME_DIR
fi
if [ "$api_only" = 0 ] && [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    [ "${SYN_BOOTSTRAP_DBUS_ATTEMPTED:-0}" != 1 ] || fail 'D-Bus could not start.'
    export SYN_BOOTSTRAP_DBUS_ATTEMPTED=1
    notice 'Starting a private D-Bus session for the native Secret Service.'
    exec dbus-run-session -- sh "$script" "$@"
fi

# Fresh private sessions require the operator to unlock a protected keyring.
# The passphrase is never persisted, placed in argv, or echoed to the terminal.
if [ "$api_only" = 0 ] && [ "${SYN_BOOTSTRAP_SECRETS_READY:-0}" != 1 ]; then
    python3 "$syn_root/scripts/bootstrap-keyring.py" || fail 'Native Secret Service was not unlocked.'
    export SYN_BOOTSTRAP_SECRETS_READY=1
fi

if command -v sha256sum >/dev/null 2>&1; then
    project_id=$(printf '%s' "$syn_root" | sha256sum | cut -c1-20)
else
    fail 'sha256sum is missing.'
fi
venv_home="$HOME/.local/state/syn/venvs"
mkdir -p "$venv_home"
export UV_PROJECT_ENVIRONMENT="$venv_home/$project_id"
notice "Running with $uv_bin and private environment $UV_PROJECT_ENVIRONMENT"

# Prefer a Linux-owned config location for a first installation. Do not move,
# edit, or replace an existing explicit or project configuration.
if [ "$explicit_config" = 0 ] && [ "$config_path" = "$syn_root/config.toml" ] && [ ! -e "$config_path" ]; then
    config_path="$HOME/.local/state/syn/config.toml"
    mkdir -p "$(dirname "$config_path")"
    set -- "$@" --config "$config_path"
fi
notice 'Syncing Syn Python dependencies, then starting operator/API and awareness services.'
exec "$uv_bin" run --project "$syn_root" --extra position-native -- syn-server "$@"

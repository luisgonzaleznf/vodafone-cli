#!/bin/sh
# Install the `vodafone` CLI (vodafone-cli) as a uv tool, pinned to a release tag:
#
#   curl -fsSL https://raw.githubusercontent.com/luisgonzaleznf/vodafone-cli/main/install.sh | sh
#
# Installs uv with Astral's official installer if it is missing, then runs `uv tool install`.
# Safe to re-run: the same release is left as it is, another release replaces it.
#
# Environment:
#   VODAFONE_CLI_VERSION  release to install, e.g. 0.1.0 (installs the tag v<version>)
#   VODAFONE_CLI_REF      git ref to install instead of the release tag (branch, tag or commit)
#   VODAFONE_CLI_SOURCE   git repository URL (default: the GitHub repository over https; use
#                         ssh://git@github.com/luisgonzaleznf/vodafone-cli.git for SSH auth).
#                         Only plain https://, ssh:// and file:// URLs are accepted: no password,
#                         token, query or fragment. git's credential helpers supply credentials.
#   VODAFONE_CLI_FORCE    1 replaces an editable (development) install of vodafone-cli, and any
#                         other `vodafone` executable in uv's bin directory
# uv's own variables (UV_TOOL_DIR, UV_TOOL_BIN_DIR, UV_INSTALL_DIR, ...) apply as usual.

set -eu

DEFAULT_VERSION=0.2.0
DEFAULT_SOURCE=https://github.com/luisgonzaleznf/vodafone-cli.git
UV_INSTALLER=https://astral.sh/uv/install.sh

say() {
    printf '%s\n' "$*"
}

die() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        die "curl or wget is needed to download $1"
    fi
}

uv_install_dir() {
    # The directory Astral's installer installs uv into, with the installer's own precedence.
    dir=${UV_INSTALL_DIR:-${CARGO_DIST_FORCE_INSTALL_DIR:-${UV_UNMANAGED_INSTALL:-}}}
    if [ -n "$dir" ]; then
        # A forced directory that is Cargo's home keeps the older cargo-home layout.
        if [ "$dir" = "${CARGO_HOME:-$HOME/.cargo}" ]; then
            dir=$dir/bin
        fi
    elif [ -n "${XDG_BIN_HOME:-}" ]; then
        dir=$XDG_BIN_HOME
    elif [ -n "${XDG_DATA_HOME:-}" ]; then
        dir=$XDG_DATA_HOME/../bin
    else
        dir=$HOME/.local/bin
    fi
    printf '%s\n' "$dir"
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    # Not on PATH (yet): where the installer puts uv, then its default and pre-0.5 locations.
    for candidate in "$(uv_install_dir)/uv" "$HOME/.local/bin/uv" "${CARGO_HOME:-$HOME/.cargo}/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

plain_name() {
    # Letters, digits, ".", "_" and "-" only: a host name, a user name or a port.
    case $1 in
        "" | *[![:alnum:]._-]*) return 1 ;;
    esac
}

plain_path() {
    # An absolute path of letters, digits and "._~/+-" only.
    case $1 in
        /*) ;;
        *) return 1 ;;
    esac
    case $1 in
        *[![:alnum:]._~/+-]*) return 1 ;;
    esac
}

is_plain_source() {
    # Only https://host[:port]/path, ssh://[user@]host[:port]/path and file:///path pass. With no
    # password, query, fragment, escape or backslash allowed, nothing secret or malformed can reach
    # this script's output, uv's output or uv's tool receipt.
    case $1 in
        file:///*)
            plain_path "${1#file://}"
            return
            ;;
        https://* | ssh://*) ;;
        *) return 1 ;;
    esac
    rest=${1#*://}
    authority=${rest%%/*}
    [ "$authority" != "$rest" ] || return 1 # no path: names no repository
    plain_path "/${rest#*/}" || return 1
    case $1 in
        # ssh:// may name the user (git@host), which is not a secret.
        ssh://*@*)
            plain_name "${authority%%@*}" || return 1
            authority=${authority#*@}
            ;;
    esac
    case $authority in
        *:*)
            case ${authority#*:} in
                "" | *[!0-9]*) return 1 ;;
            esac
            authority=${authority%%:*}
            ;;
    esac
    plain_name "$authority"
}

install_uv() {
    say "uv not found: installing it with Astral's installer ($UV_INSTALLER)"
    # Downloaded in full before it runs, so a failed download never runs a partial script.
    installer=$(download "$UV_INSTALLER") || die "could not download $UV_INSTALLER"
    printf '%s\n' "$installer" | sh || die "the uv installer failed"
}

is_editable_install() {
    # uv records an editable (development) install in the tool's receipt.
    receipt="$("$1" tool dir)/vodafone-cli/uv-receipt.toml"
    [ -f "$receipt" ] && grep -q 'editable = ' "$receipt"
}

main() {
    version=${VODAFONE_CLI_VERSION:-$DEFAULT_VERSION}
    ref=${VODAFONE_CLI_REF:-v${version#v}}
    source=${VODAFONE_CLI_SOURCE:-$DEFAULT_SOURCE}
    source=${source#git+}
    force=${VODAFONE_CLI_FORCE:-0}

    # Checked before anything runs: uv installs under $HOME, and the source and ref are printed,
    # passed to uv and saved in its tool receipt. Neither value is echoed when it is refused.
    [ -n "${HOME:-}" ] || die "HOME is not set"
    if ! is_plain_source "$source"; then
        die "VODAFONE_CLI_SOURCE must be a plain https://, ssh:// or file:// git URL, without a password, token, query or fragment; let git supply credentials (for example \`gh auth setup-git\`)"
    fi
    case $ref in
        "" | -* | *[![:alnum:]._/+-]*) die "VODAFONE_CLI_REF and VODAFONE_CLI_VERSION must be a plain tag, branch or commit" ;;
    esac

    if ! uv=$(find_uv); then
        install_uv
        uv=$(find_uv) || die "uv was installed but cannot be found; open a new shell and run this script again"
    fi

    if [ "$force" != 1 ] && is_editable_install "$uv"; then
        die "vodafone-cli is installed in editable (development) mode; leaving it alone. Set VODAFONE_CLI_FORCE=1 to replace it with $ref."
    fi

    say "Installing vodafone-cli $ref from $source"
    set -- tool install "vodafone-cli @ git+$source@$ref"
    if [ "$force" = 1 ]; then
        set -- "$@" --force
    fi
    "$uv" "$@" </dev/null || die "uv tool install failed"

    bin_dir=$("$uv" tool dir --bin)
    installed=$("$bin_dir/vodafone" --version </dev/null) || die "$bin_dir/vodafone does not run"
    say "Installed vodafone $installed in $bin_dir"

    # uv itself warns when $bin_dir is not on PATH; it does not say when another copy shadows it.
    found=$(command -v vodafone || true)
    if [ -n "$found" ] && [ "$found" != "$bin_dir/vodafone" ]; then
        say "Note: $found comes first on your PATH; this install is $bin_dir/vodafone"
    fi

    say ""
    say "Next step: sign in with"
    say "  vodafone auth login"
}

main "$@"

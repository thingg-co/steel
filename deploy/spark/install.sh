#!/usr/bin/env bash
set -euo pipefail

# Install Steel as an always-on systemd user service on the Spark. Crypto trades around the
# clock, so unlike Flint there is no timer: the service starts at boot and restarts on failure.
#
# Note: `loginctl enable-linger alex` must be run once so user services keep running without an
# active login session. The Spark is a headless box that may reboot or log out.

USER="${USER:-alex}"

LINGER_STATUS=$(loginctl show-user "$USER" -p Linger 2>/dev/null || echo "Linger=no")
if [[ "$LINGER_STATUS" != "Linger=yes" ]]; then
    echo "Error: Linger is not enabled for user $USER"
    echo "Run this command once to enable user services:"
    echo ""
    echo "    loginctl enable-linger $USER"
    echo ""
    exit 1
fi

# steel-start sets STEEL_COMPILE=1, and on the first train step Triton builds a CUDA helper
# against Python.h. Without the venv Python's dev headers that step fails and Steel trains
# eager (about half speed on the GB10). Say it here; an OS reinstall drops the package.
PY="$(dirname "$0")/../../.venv/bin/python"
if [[ -x "$PY" ]]; then
    read -r INC PKG < <("$PY" -c 'import sys, sysconfig; print(sysconfig.get_paths()["include"], f"python{sys.version_info[0]}.{sys.version_info[1]}-dev")')
    if [[ ! -f "$INC/Python.h" ]]; then
        echo "Warning: $INC/Python.h is missing, so torch.compile cannot build Triton's CUDA helper"
        echo "and Steel will train eager. Install the headers for the venv's Python:"
        echo ""
        echo "    sudo apt install $PKG"
        echo ""
    fi
fi

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/bin"
install -m 0755 "$(dirname "$0")/steel-start" "$HOME/.local/bin/steel-start"
install -m 0755 "$(dirname "$0")/steel-handoff" "$HOME/.local/bin/steel-handoff"
cp "$(dirname "$0")/steel.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now steel.service

echo "Steel service installed and running:"
systemctl --user status steel.service --no-pager | head -5
echo "logs: journalctl --user -u steel -f"

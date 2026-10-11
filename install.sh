#!/usr/bin/env bash
# ==============================================================================
# Google Drive for Linux - Unified Installer Script
# Supports:
#   1. System-wide Debian (.deb) package installation (via apt/dpkg with sudo)
#   2. User-mode installation (no root or sudo privileges required)
#   3. Direct execution or piped via curl:
#      curl -fsSL https://aisgit.armellini.net/aelmark/google-drive/raw/branch/main/install.sh | bash
# ==============================================================================
set -euo pipefail

# Text formatting
BOLD="\033[1m"
GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

REPO_BASE_URL="https://aisgit.armellini.net/aelmark/google-drive/raw/branch/main"
DEB_FILENAME="google-drive-desktop_1.12_all.deb"
DEB_REMOTE_URL="${REPO_BASE_URL}/dist/${DEB_FILENAME}"

echo -e "${BLUE}${BOLD}======================================================${RESET}"
echo -e "${BLUE}${BOLD}      Google Drive for Linux - Installation           ${RESET}"
echo -e "${BLUE}${BOLD}======================================================${RESET}"

# Parse command line flags
MODE="auto" # auto, system, user
for arg in "$@"; do
    case "${arg}" in
        --user)
            MODE="user"
            ;;
        --system)
            MODE="system"
            ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --system   Force system-wide installation via apt/dpkg (requires sudo)"
            echo "  --user     Install locally into ~/.local (no root/sudo required)"
            echo "  --help     Show this help message"
            exit 0
            ;;
    esac
done

# Check Python 3 version
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: Python 3 is required but not installed.${RESET}"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))')
if [ "${PY_OK}" -ne 1 ]; then
    echo -e "${RED}Error: Python 3.10 or newer is required (found ${PY_VER}).${RESET}"
    exit 1
fi

# Detect SCRIPT_DIR
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
fi

# Decide installation mode
if [ "${MODE}" = "auto" ]; then
    if [ "$(id -u)" -eq 0 ]; then
        MODE="system"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        MODE="system"
    elif [ -f /etc/debian_version ] && command -v sudo >/dev/null 2>&1; then
        # Ask user or try system
        MODE="system"
    else
        MODE="user"
    fi
fi

# ==============================================================================
# Mode 1: System-wide Debian (.deb) package install
# ==============================================================================
install_system() {
    echo -e "${CYAN}-> Performing system-wide installation via Debian package...${RESET}"

    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            SUDO="sudo"
        else
            echo -e "${YELLOW}Warning: sudo not available, falling back to --user install.${RESET}"
            install_user
            return
        fi
    fi

    TEMP_DIR=$(mktemp -d /tmp/gdrive-install.XXXXXX)
    trap 'rm -rf "${TEMP_DIR}"' EXIT

    DEB_PATH=""
    # Check local repository directories first
    if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/dist/${DEB_FILENAME}" ]; then
        DEB_PATH="${SCRIPT_DIR}/dist/${DEB_FILENAME}"
    elif [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/${DEB_FILENAME}" ]; then
        DEB_PATH="${SCRIPT_DIR}/${DEB_FILENAME}"
    elif [ -f "./dist/${DEB_FILENAME}" ]; then
        DEB_PATH="$(pwd)/dist/${DEB_FILENAME}"
    fi

    # If local build script exists and deb missing, build it
    if [ -z "${DEB_PATH}" ] && [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/packaging/build_deb.sh" ]; then
        echo -e "${YELLOW}-> Building Debian package from source...${RESET}"
        "${SCRIPT_DIR}/packaging/build_deb.sh"
        DEB_PATH="${SCRIPT_DIR}/dist/${DEB_FILENAME}"
    fi

    # If still no deb, download it from Forgejo repository
    if [ -z "${DEB_PATH}" ] || [ ! -f "${DEB_PATH}" ]; then
        echo -e "${YELLOW}-> Downloading ${DEB_FILENAME} from repository...${RESET}"
        DEB_PATH="${TEMP_DIR}/${DEB_FILENAME}"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL "${DEB_REMOTE_URL}" -o "${DEB_PATH}"
        elif command -v wget >/dev/null 2>&1; then
            wget -q "${DEB_REMOTE_URL}" -O "${DEB_PATH}"
        else
            echo -e "${RED}Error: Neither curl nor wget is available to download the package.${RESET}"
            exit 1
        fi
    fi

    echo -e "${GREEN}-> Installing package: ${DEB_PATH}${RESET}"
    ${SUDO} apt-get update -qq || true
    ${SUDO} apt-get install -y --fix-broken "${DEB_PATH}"

    # Enable systemd user service and activate for the desktop user session
    TARGET_USER="${SUDO_USER:-$(id -un)}"
    TARGET_UID="$(id -u "${TARGET_USER}" 2>/dev/null || echo "")"

    if [ -n "${TARGET_UID}" ] && [ "${TARGET_UID}" -ge 1000 ] 2>/dev/null && [ -d "/run/user/${TARGET_UID}" ]; then
        echo -e "${GREEN}-> Activating Google Drive service for user ${TARGET_USER}...${RESET}"
        if [ "$(id -u)" -eq 0 ]; then
            su - "${TARGET_USER}" -c "XDG_RUNTIME_DIR=/run/user/${TARGET_UID} systemctl --user daemon-reload && XDG_RUNTIME_DIR=/run/user/${TARGET_UID} systemctl --user enable --now google-drive" 2>/dev/null || \
            su - "${TARGET_USER}" -c "nohup /usr/bin/google-drive --daemon >/dev/null 2>&1 &" || true
        else
            systemctl --user daemon-reload || true
            systemctl --user enable --now google-drive || true
        fi
    fi
}

# ==============================================================================
# Mode 2: User-mode installation (No root or sudo required)
# ==============================================================================
install_user() {
    echo -e "${CYAN}-> Performing user-mode installation into ~/.local...${RESET}"

    USER_BIN="${HOME}/.local/bin"
    USER_LIB="${HOME}/.local/share/google-drive/lib/linux_drive"
    USER_APPS="${HOME}/.local/share/applications"
    USER_ICONS="${HOME}/.local/share/icons/hicolor"
    USER_SYSTEMD="${HOME}/.config/systemd/user"
    USER_TYPELIBS="${HOME}/.local/lib/girepository-1.0"

    mkdir -p "${USER_BIN}" "${USER_LIB}" "${USER_APPS}" "${USER_SYSTEMD}" "${USER_TYPELIBS}"

    # Find source files
    SRC_DIR=""
    if [ -n "${SCRIPT_DIR}" ] && [ -d "${SCRIPT_DIR}/linux_drive" ]; then
        SRC_DIR="${SCRIPT_DIR}"
    elif [ -d "./linux_drive" ]; then
        SRC_DIR="$(pwd)"
    fi

    # If running piped via curl and source not available, clone or extract deb
    if [ -z "${SRC_DIR}" ]; then
        TEMP_DIR=$(mktemp -d /tmp/gdrive-user-install.XXXXXX)
        trap 'rm -rf "${TEMP_DIR}"' EXIT
        echo -e "${YELLOW}-> Fetching package components...${RESET}"
        DEB_TEMP="${TEMP_DIR}/${DEB_FILENAME}"
        curl -fsSL "${DEB_REMOTE_URL}" -o "${DEB_TEMP}"
        dpkg-deb -x "${DEB_TEMP}" "${TEMP_DIR}/extracted"
        
        cp -r "${TEMP_DIR}/extracted/usr/lib/python3/dist-packages/linux_drive/"* "${USER_LIB}/"
        cp -r "${TEMP_DIR}/extracted/usr/share/icons/"* "${HOME}/.local/share/icons/" 2>/dev/null || true
        cp -r "${TEMP_DIR}/extracted/usr/share/google-drive/typelibs/"* "${USER_TYPELIBS}/" 2>/dev/null || true
    else
        # Copy Python files from source
        echo -e "-> Copying Python package files..."
        cp -r "${SRC_DIR}/linux_drive/"*.py "${USER_LIB}/"
        # Copy icons
        if [ -d "${SRC_DIR}/assets/icons" ]; then
            for size in 32 64 128 256 512 1024; do
                mkdir -p "${USER_ICONS}/${size}x${size}/apps"
                if [ -f "${SRC_DIR}/assets/icons/google-drive-${size}.png" ]; then
                    cp "${SRC_DIR}/assets/icons/google-drive-${size}.png" "${USER_ICONS}/${size}x${size}/apps/google-drive.png"
                fi
            done
        fi
        # Copy GObject typelibs
        if [ -d "${SRC_DIR}/assets/typelibs" ]; then
            echo -e "-> Copying GObject typelibs..."
            cp -r "${SRC_DIR}/assets/typelibs/"* "${USER_TYPELIBS}/" 2>/dev/null || true
        fi
    fi

    # Create wrappers in ~/.local/bin
    echo -e "-> Creating CLI and GUI binary launchers in ${USER_BIN}..."

    cat << 'EOF' > "${USER_BIN}/google-drive"
#!/usr/bin/env python3
import sys
import os
import subprocess
from pathlib import Path

# Add user lib to path
lib_dir = Path.home() / ".local" / "share" / "google-drive" / "lib"
if str(lib_dir) not in sys.path:
    sys.path.insert(0, str(lib_dir))

from linux_drive.drivectl import is_daemon_running, ensure_daemon_running

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        from linux_drive.ipc_daemon import DriveIpcDaemon
        daemon = DriveIpcDaemon()
        daemon.start()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--tray":
        from linux_drive.tray_indicator import main as tray_main
        tray_main()
        return

    # Check if daemon is running, if not start it in background
    if not is_daemon_running():
        ensure_daemon_running(timeout=4.0)

    # Ensure tray indicator is running in the background for active desktop
    try:
        from linux_drive.tray_indicator import is_tray_running
        if not is_tray_running():
            subprocess.Popen([sys.executable, "-m", "linux_drive.tray_indicator"], start_new_session=True)
    except Exception:
        pass

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        from linux_drive.drivectl import cmd_status
        cmd_status(None)
        return

    from linux_drive.preferences_dialog import main as pref_main
    pref_main()

if __name__ == '__main__':
    main()
EOF
    chmod 755 "${USER_BIN}/google-drive"

    cat << 'EOF' > "${USER_BIN}/drivectl"
#!/usr/bin/env python3
import sys
from pathlib import Path

lib_dir = Path.home() / ".local" / "share" / "google-drive" / "lib"
if str(lib_dir) not in sys.path:
    sys.path.insert(0, str(lib_dir))

from linux_drive.drivectl import main
if __name__ == '__main__':
    main()
EOF
    chmod 755 "${USER_BIN}/drivectl"

    cat << 'EOF' > "${USER_BIN}/dfd_mcp_server"
#!/usr/bin/env python3
import sys
from pathlib import Path

lib_dir = Path.home() / ".local" / "share" / "google-drive" / "lib"
if str(lib_dir) not in sys.path:
    sys.path.insert(0, str(lib_dir))

from linux_drive.dfd_mcp_server import main
if __name__ == '__main__':
    main()
EOF
    chmod 755 "${USER_BIN}/dfd_mcp_server"

    # Create desktop launcher
    cat << EOF > "${USER_APPS}/google-drive.desktop"
[Desktop Entry]
Name=Google Drive
GenericName=File Synchronizer
Comment=Google Drive Desktop & MCP Server for Linux
Exec=${USER_BIN}/google-drive
Icon=google-drive
Terminal=false
Type=Application
Categories=Network;FileTransfer;Utility;
Keywords=google;drive;cloud;storage;sync;mcp;
StartupNotify=true
EOF
    chmod 644 "${USER_APPS}/google-drive.desktop"

    # Create user systemd service & enable on reboot
    mkdir -p "${USER_SYSTEMD}/default.target.wants"
    cat << EOF > "${USER_SYSTEMD}/google-drive.service"
[Unit]
Description=Google Drive for Linux - Core IPC Daemon & MCP Service
Documentation=https://aisgit.armellini.net/aelmark/google-drive
After=network.target

[Service]
Type=simple
ExecStart=${USER_BIN}/google-drive --daemon
Restart=on-failure
RestartSec=5s
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    chmod 644 "${USER_SYSTEMD}/google-drive.service"
    ln -sf "../google-drive.service" "${USER_SYSTEMD}/default.target.wants/google-drive.service"

    # Create XDG desktop autostart entry for graphical login reboot
    USER_AUTOSTART="${HOME}/.config/autostart"
    mkdir -p "${USER_AUTOSTART}"
    cat << EOF > "${USER_AUTOSTART}/google-drive-autostart.desktop"
[Desktop Entry]
Type=Application
Name=Google Drive
Comment=Google Drive Desktop Background Sync Daemon & System Tray Indicator
Exec=${USER_BIN}/google-drive --tray
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
    chmod 644 "${USER_AUTOSTART}/google-drive-autostart.desktop"

    # Reload systemd and enable/start service immediately
    if command -v systemctl >/dev/null 2>&1 && [ -d "/run/user/$(id -u)" ]; then
        echo -e "${GREEN}-> Enabling Google Drive user service in systemd...${RESET}"
        systemctl --user daemon-reload || true
        systemctl --user enable --now google-drive || true
    fi

    # Activate System Tray Indicator for current graphical desktop session
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        echo -e "${GREEN}-> Activating Google Drive System Tray Indicator...${RESET}"
        "${USER_BIN}/drivectl" tray start >/dev/null 2>&1 || true
    fi

    # Update desktop database
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "${USER_APPS}" 2>/dev/null || true
    fi

    # Check PATH warning
    if [[ ":$PATH:" != *":${USER_BIN}:"* ]]; then
        echo -e "${YELLOW}Note: Add ~/.local/bin to your PATH to run 'drivectl' or 'google-drive' directly:${RESET}"
        echo -e "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    fi
}

# Run chosen mode
if [ "${MODE}" = "system" ]; then
    install_system
else
    install_user
fi

echo ""
echo -e "${GREEN}${BOLD}✓ Google Drive Desktop successfully installed!${RESET}"
echo ""
echo -e "${BOLD}Quick Usage Guide:${RESET}"
echo -e "  • ${BLUE}Launch GUI Preferences:${RESET}      google-drive  (or from GNOME App Menu)"
echo -e "  • ${BLUE}System Tray Indicator:${RESET}       drivectl tray [status|start|stop|restart]"
echo -e "  • ${BLUE}CLI Status & Quota:${RESET}          drivectl status"
echo -e "  • ${BLUE}Authenticate Account:${RESET}        drivectl auth login"
echo -e "  • ${BLUE}Track a local folder:${RESET}        drivectl add ~/Documents"
echo -e "  • ${BLUE}Background Service:${RESET}          systemctl --user status google-drive"
echo -e "  • ${BLUE}Model Context Protocol (MCP):${RESET} dfd_mcp_server"
echo ""
echo -e "${BLUE}${BOLD}======================================================${RESET}"

# Run initial status (run as invoking desktop user if running via sudo)
echo -e "${CYAN}-> Verifying active service status...${RESET}"
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    su - "${SUDO_USER}" -c "drivectl status" || true
elif command -v drivectl >/dev/null 2>&1; then
    drivectl status || true
elif [ -x "${HOME}/.local/bin/drivectl" ]; then
    "${HOME}/.local/bin/drivectl" status || true
fi

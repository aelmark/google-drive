#!/usr/bin/env bash
# ==============================================================================
# Google Drive for Linux - Uninstaller Script
# Removes Google Drive desktop app, services, and optional configuration
# ==============================================================================
set -euo pipefail

BOLD="\033[1m"
GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

PURGE=false
for arg in "$@"; do
    if [ "${arg}" = "--purge" ]; then
        PURGE=true
    fi
done

echo -e "${BLUE}${BOLD}======================================================${RESET}"
echo -e "${BLUE}${BOLD}      Google Drive for Linux - Uninstallation         ${RESET}"
echo -e "${BLUE}${BOLD}======================================================${RESET}"

# 1. Stop background service and processes
echo -e "${YELLOW}-> Stopping background service and running processes...${RESET}"
if command -v systemctl >/dev/null 2>&1 && [ -d "/run/user/$(id -u)" ]; then
    systemctl --user stop google-drive || true
    systemctl --user disable google-drive || true
fi

pkill -f "linux_drive/ipc_daemon.py" || true
pkill -f "linux_drive.ipc_daemon" || true

IPC_SOCK="/tmp/drivefs_ipc.$(id -u)"
if [ -S "${IPC_SOCK}" ] || [ -f "${IPC_SOCK}" ]; then
    rm -f "${IPC_SOCK}" || true
fi

# 2. Remove system-wide Debian package if installed
if dpkg -s google-drive-desktop >/dev/null 2>&1; then
    echo -e "${GREEN}-> Removing system Debian package (google-drive-desktop)...${RESET}"
    SUDO=""
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    fi
    ${SUDO} apt-get remove -y google-drive-desktop || ${SUDO} dpkg -r google-drive-desktop || true
fi

# 3. Remove user-mode files if present
echo -e "${GREEN}-> Cleaning up user-mode files...${RESET}"
rm -f "${HOME}/.local/bin/google-drive"
rm -f "${HOME}/.local/bin/drivectl"
rm -f "${HOME}/.local/bin/dfd_mcp_server"
rm -f "${HOME}/.local/share/applications/google-drive.desktop"
rm -f "${HOME}/.config/autostart/google-drive-autostart.desktop"
rm -f "${HOME}/.config/systemd/user/google-drive.service"
rm -f "${HOME}/.config/systemd/user/default.target.wants/google-drive.service"
rm -rf "${HOME}/.local/share/google-drive/lib"

if command -v systemctl >/dev/null 2>&1 && [ -d "/run/user/$(id -u)" ]; then
    systemctl --user daemon-reload || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${HOME}/.local/share/applications" 2>/dev/null || true
fi

# 4. Handle purge (tokens, database, cache)
if [ "${PURGE}" = true ]; then
    echo -e "${YELLOW}-> Purging configuration, authentication tokens, and state databases...${RESET}"
    rm -rf "${HOME}/.config/google-drive"
    rm -rf "${HOME}/.local/share/google-drive"
else
    echo -e "${CYAN}Note: Configuration and tokens preserved in ~/.config/google-drive.${RESET}"
    echo -e "      (Pass --purge to remove all account tokens and cache databases)."
fi

echo ""
echo -e "${GREEN}${BOLD}✓ Google Drive Desktop successfully uninstalled.${RESET}"
echo ""

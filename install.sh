#!/usr/bin/env bash
# ==============================================================================
# Google Drive for Linux - One-Line Installer Script
# Can be run locally or via:
#   curl -fsSL <url>/install.sh | bash
# ==============================================================================
set -euo pipefail

# Text formatting
BOLD="\033[1m"
GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "${BLUE}${BOLD}======================================================${RESET}"
echo -e "${BLUE}${BOLD}      Google Drive for Linux - Installation           ${RESET}"
echo -e "${BLUE}${BOLD}======================================================${RESET}"

# Check if running with sudo/root permissions available
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo -e "${RED}Error: Root or sudo access is required to install system packages.${RESET}"
        exit 1
    fi
fi

# Detect OS
if [ ! -f /etc/debian_version ]; then
    echo -e "${YELLOW}Warning: This package is optimized for Debian, Ubuntu, Pop!_OS, and derivatives.${RESET}"
fi

# Find deb package location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
DEB_PATH=""

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/dist/google-drive-desktop_1.0.0_all.deb" ]; then
    DEB_PATH="${SCRIPT_DIR}/dist/google-drive-desktop_1.0.0_all.deb"
elif [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/google-drive-desktop_1.0.0_all.deb" ]; then
    DEB_PATH="${SCRIPT_DIR}/google-drive-desktop_1.0.0_all.deb"
elif [ -f "./dist/google-drive-desktop_1.0.0_all.deb" ]; then
    DEB_PATH="$(pwd)/dist/google-drive-desktop_1.0.0_all.deb"
fi

# If deb not found locally, build it or download it
if [ -z "${DEB_PATH}" ] || [ ! -f "${DEB_PATH}" ]; then
    if [ -f "${SCRIPT_DIR}/packaging/build_deb.sh" ]; then
        echo -e "${YELLOW}-> Building fresh Debian package...${RESET}"
        "${SCRIPT_DIR}/packaging/build_deb.sh"
        DEB_PATH="${SCRIPT_DIR}/dist/google-drive-desktop_1.0.0_all.deb"
    else
        echo -e "${RED}Error: Could not locate google-drive-desktop_1.0.0_all.deb.${RESET}"
        exit 1
    fi
fi

echo -e "${GREEN}-> Installing package: ${DEB_PATH}${RESET}"
# Install deb package using apt to automatically resolve any missing dependencies
${SUDO} apt-get update -qq || true
${SUDO} apt-get install -y --fix-broken "${DEB_PATH}"

# Configure systemd user service if running in user session
if command -v systemctl >/dev/null 2>&1 && [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" -o -d "/run/user/$(id -u)" ]; then
    echo -e "${GREEN}-> Enabling Google Drive user service...${RESET}"
    systemctl --user daemon-reload || true
    systemctl --user enable --now google-drive || true
fi

echo ""
echo -e "${GREEN}${BOLD}✓ Google Drive Desktop successfully installed!${RESET}"
echo ""
echo -e "${BOLD}Usage Guide:${RESET}"
echo -e "  • ${BLUE}Launch GUI Preferences:${RESET}      google-drive  (or from GNOME App Menu)"
echo -e "  • ${BLUE}CLI Status & Tracking:${RESET}       drivectl status"
echo -e "  • ${BLUE}Track a local folder:${RESET}        drivectl add ~/Documents"
echo -e "  • ${BLUE}Background Service:${RESET}          systemctl --user status google-drive"
echo -e "  • ${BLUE}Model Context Protocol (MCP):${RESET} dfd_mcp_server"
echo ""
echo -e "${BLUE}${BOLD}======================================================${RESET}"

# Run status check if drivectl is now on PATH
if command -v drivectl >/dev/null 2>&1; then
    drivectl status || true
fi

#!/usr/bin/env bash
# ==============================================================================
# Google Drive for Linux - Update Script
# Updates existing installation to the latest version from Forgejo repository
# ==============================================================================
set -euo pipefail

BOLD="\033[1m"
GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

echo -e "${BLUE}${BOLD}======================================================${RESET}"
echo -e "${BLUE}${BOLD}      Google Drive for Linux - Update Utility          ${RESET}"
echo -e "${BLUE}${BOLD}======================================================${RESET}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
REPO_BASE_URL="https://aisgit.armellini.net/aelmark/google-drive/raw/branch/main"
DEB_FILENAME="google-drive-desktop_1.0.0_all.deb"
DEB_REMOTE_URL="${REPO_BASE_URL}/dist/${DEB_FILENAME}"

# 1. If running inside git clone, pull newest changes
if [ -n "${SCRIPT_DIR}" ] && [ -d "${SCRIPT_DIR}/.git" ]; then
    echo -e "${GREEN}-> Pulling latest updates from git repository...${RESET}"
    git -C "${SCRIPT_DIR}" pull --rebase || true

    if [ -f "${SCRIPT_DIR}/packaging/build_deb.sh" ]; then
        echo -e "${GREEN}-> Rebuilding Debian package...${RESET}"
        "${SCRIPT_DIR}/packaging/build_deb.sh"
    fi

    # Run installer in appropriate mode
    if dpkg -s google-drive-desktop >/dev/null 2>&1; then
        echo -e "${GREEN}-> Updating system-wide package...${RESET}"
        "${SCRIPT_DIR}/install.sh" --system
    else
        echo -e "${GREEN}-> Updating user-mode installation...${RESET}"
        "${SCRIPT_DIR}/install.sh" --user
    fi
else
    # Standalone updater: download latest deb or files
    echo -e "${YELLOW}-> Checking installed package status...${RESET}"
    if dpkg -s google-drive-desktop >/dev/null 2>&1; then
        TEMP_DIR=$(mktemp -d /tmp/gdrive-update.XXXXXX)
        trap 'rm -rf "${TEMP_DIR}"' EXIT
        DEB_PATH="${TEMP_DIR}/${DEB_FILENAME}"
        echo -e "${GREEN}-> Downloading latest Debian package...${RESET}"
        curl -fsSL "${DEB_REMOTE_URL}" -o "${DEB_PATH}"

        SUDO=""
        if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            SUDO="sudo"
        fi
        ${SUDO} apt-get install -y --reinstall --fix-broken "${DEB_PATH}"
    else
        # User-mode update via curl
        echo -e "${GREEN}-> Updating user-mode installation via remote installer...${RESET}"
        curl -fsSL "${REPO_BASE_URL}/install.sh" | bash -s -- --user
    fi
fi

# 2. Restart background service
if command -v systemctl >/dev/null 2>&1 && [ -d "/run/user/$(id -u)" ]; then
    echo -e "${GREEN}-> Restarting background service...${RESET}"
    systemctl --user daemon-reload || true
    systemctl --user restart google-drive || true
fi

echo ""
echo -e "${GREEN}${BOLD}✓ Google Drive successfully updated to latest version!${RESET}"
echo ""

if command -v drivectl >/dev/null 2>&1; then
    drivectl status || true
elif [ -x "${HOME}/.local/bin/drivectl" ]; then
    "${HOME}/.local/bin/drivectl" status || true
fi

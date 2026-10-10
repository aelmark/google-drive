#!/usr/bin/env bash
# ==============================================================================
# Google Drive for Linux - Debian Package Builder
# Builds a standards-compliant google-drive-desktop_1.0.0_all.deb package.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PKG_NAME="google-drive-desktop"
PKG_VERSION="1.0.0"
PKG_ARCH="all"
BUILD_DIR="${REPO_ROOT}/dist/${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}"
DEB_FILE="${REPO_ROOT}/dist/${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}.deb"

echo "=== Building Debian Package: ${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH} ==="

# Clean build directory
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/DEBIAN"
mkdir -p "${BUILD_DIR}/usr/bin"
mkdir -p "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive"
mkdir -p "${BUILD_DIR}/usr/lib/systemd/user"
mkdir -p "${BUILD_DIR}/usr/share/applications"
mkdir -p "${BUILD_DIR}/usr/share/pixmaps"
mkdir -p "${BUILD_DIR}/usr/share/doc/${PKG_NAME}"

# Icons directories
for size in 32 64 128 256 512 1024; do
    mkdir -p "${BUILD_DIR}/usr/share/icons/hicolor/${size}x${size}/apps"
done

# 1. Copy Python module files
echo "-> Copying Python package files..."
cp "${REPO_ROOT}/linux_drive/__init__.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/config.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/ipc_wire.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/ipc_daemon.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/dfd_mcp_server.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/drivectl.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/preferences_dialog.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"
cp "${REPO_ROOT}/linux_drive/oauth.py" "${BUILD_DIR}/usr/lib/python3/dist-packages/linux_drive/"

# 2. Create Executable CLI & GUI wrappers in /usr/bin
echo "-> Creating binary wrappers in /usr/bin..."

cat << 'EOF' > "${BUILD_DIR}/usr/bin/google-drive"
#!/usr/bin/env python3
import sys
import subprocess
from linux_drive.drivectl import is_daemon_running

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        from linux_drive.ipc_daemon import DriveIpcDaemon
        daemon = DriveIpcDaemon()
        daemon.start()
        return

    # Check if daemon is running, if not start it in background
    if not is_daemon_running():
        subprocess.Popen([sys.executable, "-m", "linux_drive.ipc_daemon"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        from linux_drive.drivectl import cmd_status
        cmd_status(None)
        return

    from linux_drive.preferences_dialog import main as pref_main
    pref_main()

if __name__ == '__main__':
    main()
EOF
chmod 755 "${BUILD_DIR}/usr/bin/google-drive"

cat << 'EOF' > "${BUILD_DIR}/usr/bin/drivectl"
#!/usr/bin/env python3
import sys
from linux_drive.drivectl import main
if __name__ == '__main__':
    main()
EOF
chmod 755 "${BUILD_DIR}/usr/bin/drivectl"

cat << 'EOF' > "${BUILD_DIR}/usr/bin/dfd_mcp_server"
#!/usr/bin/env python3
import sys
from linux_drive.dfd_mcp_server import main
if __name__ == '__main__':
    main()
EOF
chmod 755 "${BUILD_DIR}/usr/bin/dfd_mcp_server"

# 3. Systemd User Service
echo "-> Creating systemd user service..."
cat << 'EOF' > "${BUILD_DIR}/usr/lib/systemd/user/google-drive.service"
[Unit]
Description=Google Drive for Linux - Core IPC Daemon & MCP Service
Documentation=https://github.com/google-drive-linux
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/google-drive --daemon
Restart=on-failure
RestartSec=5s
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
chmod 644 "${BUILD_DIR}/usr/lib/systemd/user/google-drive.service"

# 4. Desktop Application Launcher
echo "-> Creating desktop launcher..."
cat << 'EOF' > "${BUILD_DIR}/usr/share/applications/google-drive.desktop"
[Desktop Entry]
Name=Google Drive
Comment=Google Drive Desktop Client & MCP Server for Linux
Exec=google-drive
Icon=google-drive
Terminal=false
Type=Application
Categories=Network;FileTransfer;Utility;
StartupNotify=true
Actions=Preferences;Status;

[Desktop Action Preferences]
Name=Preferences...
Exec=google-drive --settings

[Desktop Action Status]
Name=Check Sync Status
Exec=drivectl status
EOF
chmod 644 "${BUILD_DIR}/usr/share/applications/google-drive.desktop"

# 5. Icons in hicolor theme
echo "-> Copying icons..."
for size in 32 64 128 256 512 1024; do
    if [ -f "${REPO_ROOT}/assets/icons/google-drive-${size}.png" ]; then
        cp "${REPO_ROOT}/assets/icons/google-drive-${size}.png" \
           "${BUILD_DIR}/usr/share/icons/hicolor/${size}x${size}/apps/google-drive.png"
    elif [ "${size}" -eq 1024 ] && [ -f "${REPO_ROOT}/assets/icons/google-drive.png" ]; then
        cp "${REPO_ROOT}/assets/icons/google-drive.png" \
           "${BUILD_DIR}/usr/share/icons/hicolor/1024x1024/apps/google-drive.png"
    fi
done

# Fallback pixmap
if [ -f "${REPO_ROOT}/assets/icons/google-drive.png" ]; then
    cp "${REPO_ROOT}/assets/icons/google-drive.png" "${BUILD_DIR}/usr/share/pixmaps/google-drive.png"
fi

# 6. Documentation & Changelog
echo "-> Generating documentation and changelog..."
cat << 'EOF' > "${BUILD_DIR}/usr/share/doc/${PKG_NAME}/copyright"
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: google-drive-desktop
Source: https://github.com/google-drive-linux

Files: *
Copyright: 2026 Google Drive Linux Community <marksterling@armellini.net>
License: Apache-2.0
EOF
chmod 644 "${BUILD_DIR}/usr/share/doc/${PKG_NAME}/copyright"

cat << 'EOF' | gzip -9cn > "${BUILD_DIR}/usr/share/doc/${PKG_NAME}/changelog.gz"
google-drive-desktop (1.0.0) unstable; urgency=medium

  * Initial release for Linux.
  * Extracted and reverse-engineered from Google Drive Desktop for macOS.
  * Native clean-room implementation of length-delimited Protobuf IPC daemon.
  * Model Context Protocol (MCP) server for Claude, Gemini, Antigravity, and Cursor.
  * Native GTK3 preferences configuration dialog.
  * CLI control tool (drivectl).
  * Systemd user service integration.

 -- Google Drive Linux Community <marksterling@armellini.net>  Sat, 10 Oct 2026 13:40:00 -0400
EOF
chmod 644 "${BUILD_DIR}/usr/share/doc/${PKG_NAME}/changelog.gz"

# 7. DEBIAN control and maintainer scripts
echo "-> Writing DEBIAN control scripts..."

cat << 'EOF' > "${BUILD_DIR}/DEBIAN/control"
Package: google-drive-desktop
Version: 1.0.0
Section: utils
Priority: optional
Architecture: all
Maintainer: Google Drive Linux Community <marksterling@armellini.net>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0
Suggests: libfuse3-3, fuse3
Description: Google Drive Desktop & Model Context Protocol (MCP) Server for Linux
 Native Linux client for Google Drive Desktop supporting file streaming/mirroring,
 desktop configuration GUI, CLI control utility (drivectl), systemd integration,
 and the reverse-engineered Model Context Protocol (dfd_mcp_server) for AI pair programming.
EOF
chmod 644 "${BUILD_DIR}/DEBIAN/control"

cat << 'EOF' > "${BUILD_DIR}/DEBIAN/postinst"
#!/bin/sh
set -e

if [ "$1" = "configure" ]; then
    # Update icon caches
    if which gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
    # Update desktop database
    if which update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    # Reload systemd user daemon definition globally
    if [ -d /run/systemd/system ]; then
        systemctl --global daemon-reload || true
    fi
    echo "Google Drive Desktop installed successfully."
    echo "To start the background daemon: systemctl --user enable --now google-drive"
    echo "To open preferences: google-drive"
    echo "To control via CLI: drivectl status"
fi

exit 0
EOF
chmod 755 "${BUILD_DIR}/DEBIAN/postinst"

cat << 'EOF' > "${BUILD_DIR}/DEBIAN/prerm"
#!/bin/sh
set -e

if [ "$1" = "remove" ]; then
    pkill -f "linux_drive.ipc_daemon" || true
    pkill -f "linux_drive/ipc_daemon.py" || true
fi

exit 0
EOF
chmod 755 "${BUILD_DIR}/DEBIAN/prerm"

cat << 'EOF' > "${BUILD_DIR}/DEBIAN/postrm"
#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if which gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
    if which update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if [ -d /run/systemd/system ]; then
        systemctl --global daemon-reload || true
    fi
fi

exit 0
EOF
chmod 755 "${BUILD_DIR}/DEBIAN/postrm"

# Ensure all directories are 755
find "${BUILD_DIR}" -type d -exec chmod 755 {} +

# 8. Build Debian Package using dpkg-deb
echo "-> Assembling package with dpkg-deb..."
dpkg-deb --build --root-owner-group "${BUILD_DIR}" "${DEB_FILE}"

echo "=== Package Built Successfully: ${DEB_FILE} ==="
dpkg-deb -I "${DEB_FILE}"
ls -lh "${DEB_FILE}"

#!/usr/bin/env python3
"""
Google Drive for Linux - System Tray & AppIndicator Service
Provides a persistent status indicator in the Linux system tray / top panel,
displaying live synchronization status, version information (Version 1.12),
deployment date/time (10/11/2026 19:10), quick actions, and Preferences launcher.
"""

import os
import sys
import time
import signal
import subprocess
import threading
from pathlib import Path
from typing import Optional

# Ensure typelib path includes bundled and user directories before importing gi
TYPELIB_DIRS = [
    os.path.expanduser("~/.local/lib/girepository-1.0"),
    str(Path(__file__).resolve().parent.parent / "assets" / "typelibs"),
    "/usr/lib/x86_64-linux-gnu/girepository-1.0",
    "/usr/lib/girepository-1.0",
    "/usr/share/google-drive/typelibs",
]
existing_path = os.environ.get("GI_TYPELIB_PATH", "")
valid_paths = [d for d in TYPELIB_DIRS if os.path.isdir(d)]
if existing_path:
    valid_paths.append(existing_path)
os.environ["GI_TYPELIB_PATH"] = ":".join(valid_paths)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3
    HAS_APP_INDICATOR = True
except Exception:
    HAS_APP_INDICATOR = False

from gi.repository import Gtk, Gdk, GLib, Gio, GdkPixbuf

# Local module imports
try:
    from linux_drive import __version__, __deploy_date__
except ImportError:
    __version__ = "1.12"
    __deploy_date__ = "10/11/2026 19:10"

from linux_drive.config import (
    load_config,
    get_active_account,
    get_active_account_id,
    CONFIG_DIR,
    CONFIG_FILE,
)
from linux_drive.drivectl import is_daemon_running, ensure_daemon_running, get_socket_path
from linux_drive.ipc_wire import McpIpcRequest, McpIpcResponse, frame_message, read_framed_message

TRAY_PID_FILE = os.path.expanduser("~/.config/google-drive/tray.pid")


def find_icon_directory() -> str:
    """Finds the directory containing Google Drive status and application icons."""
    candidates = [
        str(Path(__file__).resolve().parent.parent / "assets" / "icons"),
        os.path.expanduser("~/.local/share/icons/hicolor/32x32/apps"),
        os.path.expanduser("~/.local/share/google-drive/icons"),
        "/usr/share/icons/hicolor/32x32/apps",
        "/usr/share/pixmaps",
    ]
    for d in candidates:
        if os.path.isdir(d) and (
            os.path.exists(os.path.join(d, "google-drive.png"))
            or os.path.exists(os.path.join(d, "google-drive-32.png"))
        ):
            return d
    return "/usr/share/pixmaps"


def send_desktop_notification(title: str, body: str, icon_path: Optional[str] = None):
    """Sends a desktop notification using Gio DBus Notifications without external tools."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            None,
        )
        if not icon_path or not os.path.exists(icon_path):
            icon_dir = find_icon_directory()
            icon_path = os.path.join(icon_dir, "google-drive.png")

        args = GLib.Variant(
            "(susssasa{sv}i)",
            (
                "Google Drive",
                0,
                icon_path if os.path.exists(icon_path) else "drive-harddisk",
                title,
                body,
                [],
                {},
                6000,
            ),
        )
        proxy.call_sync("Notify", args, Gio.DBusCallFlags.NONE, -1, None)
    except Exception as e:
        # Fallback to silent non-blocking operation
        pass


class GoogleDriveTray:
    """
    Ayatana AppIndicator and desktop tray controller for Google Drive for Linux.
    Displays live daemon/sync status, Version 1.12, deployment time, and quick actions.
    """

    def __init__(self, show_notification: bool = True, enable_floating_pill: bool = False):
        self.show_notification = show_notification
        self.enable_floating_pill = enable_floating_pill
        self.icon_dir = find_icon_directory()
        self.config = load_config()
        self.syncing = False
        self.last_status_text = "Checking..."
        self.floating_window: Optional[Gtk.Window] = None

        # Build Context Menu
        self.menu = self._build_menu()

        # Initialize AyatanaAppIndicator3
        self.indicator = None
        if HAS_APP_INDICATOR:
            self._init_app_indicator()
        else:
            print("Notice: AyatanaAppIndicator3 not present in current environment. Using desktop status fallback.")

        # Optional companion floating badge for desktop confirmation
        if self.enable_floating_pill:
            self._create_floating_pill()

        # Write PID file for single-instance tracking
        self._write_pid()

        # Periodic status update polling
        GLib.timeout_add_seconds(3, self._on_poll_status)

        # Send initial startup notification
        if self.show_notification:
            active_acc = get_active_account(self.config)
            acc_email = active_acc.get("email") or "msterling@armellini.com"
            send_desktop_notification(
                f"Google Drive - Version {__version__}",
                f"Google Drive is running on this device.\nDeployed: {__deploy_date__}\nAccount: {acc_email}",
                os.path.join(self.icon_dir, "google-drive.png"),
            )

    def _init_app_indicator(self):
        """Configures the AppIndicator StatusNotifierItem."""
        icon_name = "google-drive"
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "google-drive-tray",
            icon_name,
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_icon_theme_path(self.icon_dir)
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(f"Google Drive - Version {__version__}")
        self.indicator.set_menu(self.menu)

    def _build_menu(self) -> Gtk.Menu:
        """Constructs the popup context menu."""
        menu = Gtk.Menu()

        # 1. Header with App Name and Version
        item_header = Gtk.MenuItem()
        lbl_header = Gtk.Label()
        lbl_header.set_markup(
            f"<b>Google Drive</b>  <span foreground='#1a73e8' weight='bold'>Version {__version__}</span>"
        )
        lbl_header.set_xalign(0)
        item_header.add(lbl_header)
        item_header.set_sensitive(False)
        menu.append(item_header)

        # 2. Deployment Date & Time (mm/dd/yyyy hh:mm)
        item_deploy = Gtk.MenuItem()
        lbl_deploy = Gtk.Label()
        lbl_deploy.set_markup(
            f"<small><span foreground='#5f6368'>Deployed: {__deploy_date__}</span></small>"
        )
        lbl_deploy.set_xalign(0)
        item_deploy.add(lbl_deploy)
        item_deploy.set_sensitive(False)
        menu.append(item_deploy)

        menu.append(Gtk.SeparatorMenuItem())

        # 3. Live Synchronization State
        self.item_sync_state = Gtk.MenuItem()
        self.lbl_sync_state = Gtk.Label()
        self.lbl_sync_state.set_markup("<b>Status:</b> <span>Connecting to daemon...</span>")
        self.lbl_sync_state.set_xalign(0)
        self.item_sync_state.add(self.lbl_sync_state)
        self.item_sync_state.set_sensitive(False)
        menu.append(self.item_sync_state)

        # 4. Connected Account Info
        self.item_account = Gtk.MenuItem()
        self.lbl_account = Gtk.Label()
        self.lbl_account.set_markup("<small>Account: loading...</small>")
        self.lbl_account.set_xalign(0)
        self.item_account.add(self.lbl_account)
        self.item_account.set_sensitive(False)
        menu.append(self.item_account)

        menu.append(Gtk.SeparatorMenuItem())

        # 5. Quick Action: Open Google Drive Folder
        item_open = Gtk.MenuItem(label="📁 Open Google Drive Folder")
        item_open.connect("activate", self._on_open_drive_folder)
        menu.append(item_open)

        # 6. Quick Action: Sync Now
        self.item_sync_now = Gtk.MenuItem(label="↻ Sync Now")
        self.item_sync_now.connect("activate", self._on_sync_now_clicked)
        menu.append(self.item_sync_now)

        menu.append(Gtk.SeparatorMenuItem())

        # 7. Action: Preferences Dialog
        item_prefs = Gtk.MenuItem(label="⚙ Preferences...")
        item_prefs.connect("activate", self._on_open_preferences)
        menu.append(item_prefs)

        # 8. Action: About Google Drive
        item_about = Gtk.MenuItem(label="ℹ About Google Drive")
        item_about.connect("activate", self._on_show_about)
        menu.append(item_about)

        menu.append(Gtk.SeparatorMenuItem())

        # 9. Action: Quit Application
        item_quit = Gtk.MenuItem(label="Quit Google Drive")
        item_quit.connect("activate", self._on_quit_clicked)
        menu.append(item_quit)

        menu.show_all()
        return menu

    def _create_floating_pill(self):
        """Creates a modern semi-transparent desktop status pill."""
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("Google Drive Status")
        win.set_decorated(False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_app_paintable(True)

        css_provider = Gtk.CssProvider()
        css = b"""
        window {
            background-color: transparent;
        }
        .status-pill {
            background-color: rgba(255, 255, 255, 0.94);
            border: 1px solid #dadce0;
            border-radius: 18px;
            padding: 4px 12px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        }
        .status-pill:hover {
            background-color: #ffffff;
            border-color: #1a73e8;
        }
        """
        css_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        eb = Gtk.EventBox()
        eb.get_style_context().add_class("status-pill")
        eb.set_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        eb.connect("button-press-event", self._on_pill_clicked)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lbl_pill_icon = Gtk.Label()
        self.lbl_pill_icon.set_markup("<span font='13'>📁</span>")
        box.pack_start(self.lbl_pill_icon, False, False, 0)

        self.lbl_pill_text = Gtk.Label()
        self.lbl_pill_text.set_markup(
            f"<b>Google Drive</b> <small>v{__version__}</small> <span foreground='#137333'>●</span>"
        )
        box.pack_start(self.lbl_pill_text, False, False, 0)

        eb.add(box)
        win.add(eb)

        # Place in top-right corner with 24px margin
        screen = Gdk.Screen.get_default()
        if screen:
            geom = screen.get_monitor_geometry(0) if hasattr(screen, "get_monitor_geometry") else None
            if geom:
                win.move(geom.width - 230, 36)
            else:
                win.move(1600, 36)

        win.show_all()
        self.floating_window = win

    def _on_pill_clicked(self, widget, event):
        """Pops up the context menu when clicking the status pill."""
        if event.button in (1, 3):  # Left or Right click
            self.menu.popup_at_pointer(event)
            return True
        return False

    def _on_poll_status(self) -> bool:
        """Polls daemon state and updates tray menu labels and indicators."""
        try:
            self.config = load_config()
            active_acc = get_active_account(self.config)
            acc_email = active_acc.get("email") or active_acc.get("name") or "No account"
            self.lbl_account.set_markup(f"<small>Account: <b>{acc_email}</b></small>")

            daemon_running = is_daemon_running()
            if not daemon_running:
                self.lbl_sync_state.set_markup("<b>Status:</b> <span foreground='#d93025'>🔴 Daemon Stopped</span>")
                if self.floating_window and hasattr(self, "lbl_pill_text"):
                    self.lbl_pill_text.set_markup(
                        f"<b>Google Drive</b> <small>v{__version__}</small> <span foreground='#d93025'>● Stopped</span>"
                    )
                if self.indicator:
                    self.indicator.set_icon_full("google-drive", "Google Drive Stopped")
                return True

            # If daemon is running, query status or display up-to-date
            if self.syncing:
                self.lbl_sync_state.set_markup("<b>Status:</b> <span foreground='#1a73e8'>🔄 Synchronizing...</span>")
                if self.floating_window and hasattr(self, "lbl_pill_text"):
                    self.lbl_pill_text.set_markup(
                        f"<b>Google Drive</b> <small>v{__version__}</small> <span foreground='#1a73e8'>🔄 Syncing</span>"
                    )
            else:
                self.lbl_sync_state.set_markup("<b>Status:</b> <span foreground='#137333'>🟢 Up to date</span>")
                if self.floating_window and hasattr(self, "lbl_pill_text"):
                    self.lbl_pill_text.set_markup(
                        f"<b>Google Drive</b> <small>v{__version__}</small> <span foreground='#137333'>●</span>"
                    )
                if self.indicator:
                    self.indicator.set_icon_full("google-drive", "Google Drive Running")

        except Exception as e:
            pass

        return True

    def _on_open_drive_folder(self, _widget):
        """Opens active Google Drive mount point in the default file manager."""
        try:
            cfg = load_config()
            active_acc = get_active_account(cfg)
            mount_point = active_acc.get("mount_point", "")
            if not mount_point or not os.path.exists(mount_point):
                mount_point = os.path.expanduser("~/GoogleDrive")

            if os.path.exists(mount_point):
                subprocess.Popen(["xdg-open", mount_point], start_new_session=True)
            else:
                send_desktop_notification("Google Drive", f"Mount directory not found: {mount_point}")
        except Exception as e:
            print(f"Error opening mount point: {e}")

    def _on_sync_now_clicked(self, _widget):
        """Triggers immediate synchronization via IPC."""
        self.syncing = True
        self.lbl_sync_state.set_markup("<b>Status:</b> <span foreground='#1a73e8'>🔄 Syncing triggered...</span>")
        send_desktop_notification("Google Drive", "Synchronization cycle started.")

        def run_sync():
            try:
                ensure_daemon_running()
                sock_path = get_socket_path()
                if os.path.exists(sock_path):
                    import socket
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(4.0)
                    s.connect(sock_path)
                    req = McpIpcRequest(trigger_sync={"account_id": get_active_account_id(load_config())})
                    s.sendall(frame_message(req.serialize()))
                    read_framed_message(s)
                    s.close()
            except Exception as e:
                print(f"Sync trigger error: {e}")
            finally:
                time.sleep(2)
                self.syncing = False

        threading.Thread(target=run_sync, daemon=True).start()

    def _on_open_preferences(self, _widget):
        """Launches Preferences dialog window."""
        try:
            ensure_daemon_running()
            # Launch preferences dialog detached
            cmd = [sys.executable, "-m", "linux_drive.preferences_dialog"]
            subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            print(f"Error launching preferences: {e}")

    def _on_show_about(self, _widget):
        """Displays About Dialog with Version 1.12 and deployment info."""
        about = Gtk.AboutDialog()
        about.set_program_name("Google Drive for Linux")
        about.set_version(f"Version {__version__}")
        about.set_comments(
            f"Google Drive Desktop Client & MCP Protocol Server for Linux.\n\n"
            f"Deployment Date: {__deploy_date__}\n"
            f"Engine: Native Drive API v3 Two-Way Synchronizer\n"
            f"Architecture: Multi-Account Isolation & Selective Sync"
        )
        about.set_copyright("© 2026 Mark Sterling")
        about.set_website("https://aisgit.armellini.net/aelmark/google-drive")
        about.set_website_label("Google Drive Linux Project Repository")

        logo_path = os.path.join(self.icon_dir, "google-drive.png")
        if os.path.exists(logo_path):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(logo_path, 96, 96, True)
                about.set_logo(pixbuf)
            except Exception:
                pass

        about.run()
        about.destroy()

    def _on_quit_clicked(self, _widget):
        """Quits the tray indicator cleanly."""
        self._remove_pid()
        Gtk.main_quit()

    def _write_pid(self):
        """Writes current tray indicator process PID."""
        try:
            os.makedirs(os.path.dirname(TRAY_PID_FILE), exist_ok=True)
            with open(TRAY_PID_FILE, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    def _remove_pid(self):
        """Removes PID file on exit."""
        try:
            if os.path.exists(TRAY_PID_FILE):
                os.unlink(TRAY_PID_FILE)
        except Exception:
            pass


def is_tray_running() -> bool:
    """Checks if a tray indicator instance is already active."""
    if not os.path.exists(TRAY_PID_FILE):
        return False
    try:
        with open(TRAY_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def start_tray_daemon(enable_floating_pill: bool = False):
    """Starts the Tray indicator main loop."""
    # Ensure background daemon is running
    ensure_daemon_running()

    # Handle termination signals cleanly
    def handle_signal(_signum, _frame):
        if os.path.exists(TRAY_PID_FILE):
            try:
                os.unlink(TRAY_PID_FILE)
            except Exception:
                pass
        Gtk.main_quit()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    app = GoogleDriveTray(show_notification=True, enable_floating_pill=enable_floating_pill)
    Gtk.main()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Google Drive Linux Tray Indicator")
    parser.add_argument("--pill", action="store_true", help="Enable floating desktop status pill")
    parser.add_argument("--no-notification", action="store_true", help="Disable launch notification")
    args = parser.parse_args()

    # If already running, exit cleanly
    if is_tray_running():
        print(f"Google Drive Tray Indicator is already active (PID: {Path(TRAY_PID_FILE).read_text().strip()}).")
        return

    start_tray_daemon(enable_floating_pill=args.pill)


if __name__ == "__main__":
    main()

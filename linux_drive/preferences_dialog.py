#!/usr/bin/env python3
"""
Google Drive Linux - Native Modern GTK3 Preferences & Configuration Dialog
Provides a full desktop interface matching Google Drive Desktop settings:
- Multi-Account management (Google Workspace + personal Gmail)
- Independent sync options (Stream vs Mirror) per account
- Automatic mount point directory creation and quick file manager access
- Continuous local folder sync ("My Computer") with real-time status & working folder removal
- Account settings, cache sizing, bandwidth limits, autostart
- Model Context Protocol (MCP) server & IPC diagnostics
"""

import os
import sys
import time
import subprocess
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

from linux_drive.config import (
    load_config,
    save_config,
    ensure_mount_point_exists,
    get_accounts,
    get_active_account_id,
    get_active_account,
    get_account_by_id,
    set_active_account_id,
    add_or_update_account,
    remove_account,
    get_tracked_folders,
    add_tracked_folder,
    remove_tracked_folder,
    CONFIG_FILE
)
from linux_drive.drivectl import (
    is_daemon_running,
    ensure_daemon_running,
    send_ipc,
    get_socket_path
)
from linux_drive.ipc_wire import McpIpcRequest
from linux_drive.oauth import oauth_manager

ICON_PATH = ROOT_DIR / "assets" / "icons" / "google-drive.png"

# Modern Google-inspired and GNOME Adwaita CSS stylesheet
MODERN_CSS = b"""
/* Main Window & Typography */
window.google-drive-prefs {
    background-color: #f8f9fa;
    color: #202124;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

/* Card Containers */
.card {
    background-color: #ffffff;
    border: 1px solid #dadce0;
    border-radius: 12px;
    padding: 16px;
    margin: 4px 0px;
}

.card:backdrop {
    background-color: #fafafa;
    border-color: #e0e0e0;
}

/* Sync Mode Option Cards */
.mode-card {
    background-color: #ffffff;
    border: 2px solid #dadce0;
    border-radius: 10px;
    padding: 14px 18px;
    transition: all 180ms ease-in-out;
}

.mode-card:hover {
    border-color: #1a73e8;
    background-color: #f8fbff;
}

.mode-card.selected {
    border-color: #1a73e8;
    background-color: #e8f0fe;
}

/* Status Badges */
.badge {
    border-radius: 12px;
    padding: 3px 10px;
    font-weight: 600;
    font-size: 11px;
}

.badge-synced {
    background-color: #e6f4ea;
    color: #137333;
    border: 1px solid #ceead6;
}

.badge-syncing {
    background-color: #e8f0fe;
    color: #1a73e8;
    border: 1px solid #d2e3fc;
}

.badge-error {
    background-color: #fce8e6;
    color: #c5221f;
    border: 1px solid #fad2cf;
}

.badge-idle {
    background-color: #f1f3f4;
    color: #5f6368;
    border: 1px solid #dadce0;
}

.badge-daemon-running {
    background-color: #e6f4ea;
    color: #137333;
    border-radius: 14px;
    padding: 4px 12px;
    font-weight: bold;
    font-size: 12px;
}

.badge-daemon-stopped {
    background-color: #fce8e6;
    color: #c5221f;
    border-radius: 14px;
    padding: 4px 12px;
    font-weight: bold;
    font-size: 12px;
}

/* Buttons */
button.suggested-action, .btn-primary {
    background-color: #1a73e8;
    color: #ffffff;
    border-radius: 6px;
    font-weight: 600;
    padding: 6px 16px;
    border: none;
    box-shadow: 0 1px 2px rgba(60,64,67,0.3);
}

button.suggested-action:hover, .btn-primary:hover {
    background-color: #1557b0;
    box-shadow: 0 1px 3px rgba(60,64,67,0.4);
}

button.suggested-action:active, .btn-primary:active {
    background-color: #0d47a1;
}

button.destructive-action, .btn-danger {
    color: #d93025;
    border-color: #fce8e6;
    border-radius: 6px;
    padding: 6px 14px;
}

button.destructive-action:hover, .btn-danger:hover {
    background-color: #fce8e6;
    color: #a51d24;
}

/* HeaderBar */
headerbar {
    background-color: #ffffff;
    border-bottom: 1px solid #dadce0;
    padding: 6px 12px;
}

/* Notebook Tabs */
notebook > header {
    background-color: #ffffff;
    border-bottom: 2px solid #e0e0e0;
}

notebook > header > tabs > tab {
    padding: 10px 18px;
    font-weight: 600;
    font-size: 13px;
    color: #5f6368;
    border: none;
    background: transparent;
}

notebook > header > tabs > tab:checked {
    color: #1a73e8;
    border-bottom: 3px solid #1a73e8;
}

/* Account Switcher Banner */
.account-banner {
    background-color: #ffffff;
    border-bottom: 1px solid #dadce0;
    padding: 10px 20px;
}

/* TreeView */
treeview {
    border: 1px solid #dadce0;
    border-radius: 8px;
    background-color: #ffffff;
}

treeview:selected {
    background-color: #e8f0fe;
    color: #1a73e8;
}
"""


def format_relative_time(ts: Optional[float]) -> str:
    """Returns human-friendly relative time string."""
    if not ts:
        return "Never"
    diff = time.time() - ts
    if diff < 5:
        return "Just now"
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return time.strftime("%b %d, %H:%M", time.localtime(ts))


class PreferencesWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Google Drive")
        self.set_default_size(780, 580)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.get_style_context().add_class("google-drive-prefs")

        # Load modern CSS
        self._apply_css()

        # Window icon
        self._setup_icon()

        # Load config and initialize state
        self.config = load_config()
        self.current_account_id = get_active_account_id(self.config)
        self._sync_in_progress = False
        self._updating_widgets = False  # Avoid recursive signal loops

        # Ensure daemon is running so GUI connects to active IPC
        ensure_daemon_running(timeout=2.0)

        # Build UI Structure
        self._build_header_bar()
        self._build_main_layout()

        # Start periodic status timer (every 2.5 seconds)
        GLib.timeout_add(2500, self._on_periodic_poll)

    def _apply_css(self):
        """Applies modern CSS theme to the application screen."""
        try:
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(MODERN_CSS)
            screen = Gdk.Screen.get_default()
            if screen:
                Gtk.StyleContext.add_provider_for_screen(
                    screen,
                    css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
        except Exception as e:
            print(f"Warning: Failed to load custom CSS: {e}")

    def _setup_icon(self):
        try:
            Gtk.Window.set_default_icon_name("google-drive")
        except Exception:
            pass

        for candidate in [
            ICON_PATH,
            Path("/usr/share/icons/hicolor/512x512/apps/google-drive.png"),
            Path("/usr/share/pixmaps/google-drive.png")
        ]:
            if candidate.exists():
                try:
                    self.set_icon_from_file(str(candidate))
                    break
                except Exception:
                    pass

    def _build_header_bar(self):
        """Constructs modern GNOME HeaderBar with title, status pill, and actions."""
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = "Google Drive"
        header.props.subtitle = "Preferences & Synchronization"
        self.set_titlebar(header)

        # Left side: Daemon status indicator pill
        self.lbl_daemon_pill = Gtk.Label()
        self._update_daemon_pill()
        header.pack_start(self.lbl_daemon_pill)

        # Right side: Global Sync button and Save button
        btn_sync_all = Gtk.Button(label="↻ Sync Now")
        btn_sync_all.set_tooltip_text("Trigger immediate cloud sync for all accounts")
        btn_sync_all.connect("clicked", self._on_global_sync_clicked)
        header.pack_end(btn_sync_all)

        btn_save = Gtk.Button(label="Save")
        btn_save.get_style_context().add_class("suggested-action")
        btn_save.set_tooltip_text("Save all preferences")
        btn_save.connect("clicked", self._on_save_clicked)
        header.pack_end(btn_save)

    def _build_main_layout(self):
        """Builds main container with Account Switcher Banner and Tabbed Notebook."""
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(main_vbox)

        # Top: Multi-Account Switcher Bar
        self.account_banner = self._create_account_switcher_banner()
        main_vbox.pack_start(self.account_banner, False, False, 0)

        # Main Notebook
        self.notebook = Gtk.Notebook()
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)
        main_vbox.pack_start(self.notebook, True, True, 0)

        # Tab 1: Google Drive Syncing Options (Stream vs Mirror)
        tab_drive = self._create_drive_tab()
        self.notebook.append_page(tab_drive, Gtk.Label(label="Google Drive"))

        # Tab 2: Folders from your computer (Local folder continuous sync)
        tab_folders = self._create_folders_tab()
        self.notebook.append_page(tab_folders, Gtk.Label(label="My Computer"))

        # Tab 3: Settings & Bandwidth
        tab_settings = self._create_settings_tab()
        self.notebook.append_page(tab_settings, Gtk.Label(label="Settings"))

        # Tab 4: MCP Protocol & Diagnostics
        tab_mcp = self._create_mcp_tab()
        self.notebook.append_page(tab_mcp, Gtk.Label(label="Diagnostics & MCP"))

        # Populate initial account data
        self._load_account_data(self.current_account_id)

    # -------------------------------------------------------------------------
    # Account Switcher Banner
    # -------------------------------------------------------------------------
    def _create_account_switcher_banner(self) -> Gtk.Widget:
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        banner.get_style_context().add_class("account-banner")

        lbl_acct_icon = Gtk.Label()
        lbl_acct_icon.set_markup("<span font='16'>👤</span>")
        banner.pack_start(lbl_acct_icon, False, False, 0)

        lbl_tag = Gtk.Label()
        lbl_tag.set_markup("<b>Google Account:</b>")
        banner.pack_start(lbl_tag, False, False, 0)

        # Account Dropdown Combo
        self.combo_accounts = Gtk.ComboBoxText()
        self.combo_accounts.set_hexpand(True)
        self.combo_accounts.connect("changed", self._on_account_changed)
        banner.pack_start(self.combo_accounts, True, True, 0)

        # Add Account Button
        self.btn_add_account = Gtk.Button(label="+ Add Account...")
        self.btn_add_account.set_tooltip_text("Connect another Google Account (e.g. Workspace or Gmail)")
        self.btn_add_account.connect("clicked", self._on_add_account_clicked)
        banner.pack_start(self.btn_add_account, False, False, 0)

        # Disconnect Account Button
        self.btn_remove_account = Gtk.Button(label="Disconnect")
        self.btn_remove_account.get_style_context().add_class("destructive-action")
        self.btn_remove_account.set_tooltip_text("Disconnect and remove selected account")
        self.btn_remove_account.connect("clicked", self._on_disconnect_account_clicked)
        banner.pack_start(self.btn_remove_account, False, False, 0)

        self._refresh_accounts_combo()
        return banner

    def _refresh_accounts_combo(self):
        """Populates account dropdown with all configured accounts."""
        self._updating_widgets = True
        self.combo_accounts.remove_all()

        accounts = get_accounts(self.config)
        active_id = get_active_account_id(self.config)

        if not accounts:
            self.combo_accounts.append("primary", "Default Account (Not Signed In)")
            self.combo_accounts.set_active(0)
            self._updating_widgets = False
            return

        active_idx = 0
        idx = 0
        for aid, acc in accounts.items():
            email = acc.get("email") or aid
            name = acc.get("name", "")
            auth_str = "✓ " if acc.get("authenticated") else "🔒 "
            display_str = f"{auth_str}{email}"
            if name and name != email:
                display_str += f" ({name})"
            if aid == active_id:
                display_str += " [Active]"

            self.combo_accounts.append(aid, display_str)
            if aid == self.current_account_id or (not self.current_account_id and aid == active_id):
                active_idx = idx
            idx += 1

        self.combo_accounts.set_active(active_idx)
        self._updating_widgets = False

    def _on_account_changed(self, widget):
        if self._updating_widgets:
            return
        aid = self.combo_accounts.get_active_id()
        if aid and aid != self.current_account_id:
            self.current_account_id = aid
            set_active_account_id(aid)
            self.config = load_config()
            self._load_account_data(aid)

    def _load_account_data(self, account_id: str):
        """Loads account-specific data into the UI widgets."""
        self._updating_widgets = True
        acc = get_account_by_id(account_id, self.config) or {}

        # 1. Update Sync Mode (Stream vs Mirror)
        sync_mode = acc.get("sync_mode", "stream")
        if sync_mode == "mirror":
            self.radio_mirror.set_active(True)
            self.card_mirror.get_style_context().add_class("selected")
            self.card_stream.get_style_context().remove_class("selected")
        else:
            self.radio_stream.set_active(True)
            self.card_stream.get_style_context().add_class("selected")
            self.card_mirror.get_style_context().remove_class("selected")

        # 2. Update Mount Point
        mount_point = acc.get("mount_point", str(Path.home() / "GoogleDrive"))
        self.entry_mount.set_text(mount_point)
        # Verify and create directory automatically
        ensure_mount_point_exists(mount_point)
        self._update_mount_status_label(mount_point)

        # 3. Update Folders Store
        self._load_folders_into_store()

        # 4. Update Settings Tab Account Card
        self._populate_account_card()

        self._updating_widgets = False

    # -------------------------------------------------------------------------
    # Tab 1: Google Drive Sync Mode & Mount Point
    # -------------------------------------------------------------------------
    def _create_drive_tab(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(20)
        scrolled.add(box)

        # Section Header
        lbl_title = Gtk.Label()
        lbl_title.set_markup("<b><big>Google Drive Syncing Options</big></b>")
        lbl_title.set_xalign(0)
        box.pack_start(lbl_title, False, False, 0)

        lbl_desc = Gtk.Label(
            label="Choose how files in your Google Drive cloud are stored and synchronized on this computer."
        )
        lbl_desc.set_xalign(0)
        box.pack_start(lbl_desc, False, False, 0)

        # Sync Mode Option Card 1: Stream files
        self.card_stream = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.card_stream.get_style_context().add_class("mode-card")

        self.radio_stream = Gtk.RadioButton.new_with_label(None, "Stream files (Recommended)")
        self.radio_stream.get_style_context().add_class("h3")
        self.radio_stream.connect("toggled", self._on_sync_mode_toggled, "stream")
        self.card_stream.pack_start(self.radio_stream, False, False, 0)

        desc_stream = Gtk.Label(
            label="  • Store all your Google Drive files in the cloud only.\n"
                  "  • Files are fetched on-demand when opened via virtual mount.\n"
                  "  • Uses almost no hard drive space on your Linux system."
        )
        desc_stream.set_xalign(0)
        desc_stream.get_style_context().add_class("dim-label")
        self.card_stream.pack_start(desc_stream, False, False, 0)
        box.pack_start(self.card_stream, False, False, 0)

        # Sync Mode Option Card 2: Mirror files
        self.card_mirror = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.card_mirror.get_style_context().add_class("mode-card")

        self.radio_mirror = Gtk.RadioButton.new_with_label_from_widget(
            self.radio_stream, "Mirror files"
        )
        self.radio_mirror.get_style_context().add_class("h3")
        self.radio_mirror.connect("toggled", self._on_sync_mode_toggled, "mirror")
        self.card_mirror.pack_start(self.radio_mirror, False, False, 0)

        desc_mirror = Gtk.Label(
            label="  • Store all your Google Drive files both in the cloud and locally.\n"
                  "  • All files are automatically available offline.\n"
                  "  • Changes sync in real-time between your computer and Google Drive.\n"
                  "  • Uses more local hard drive space."
        )
        desc_mirror.set_xalign(0)
        desc_mirror.get_style_context().add_class("dim-label")
        self.card_mirror.pack_start(desc_mirror, False, False, 0)
        box.pack_start(self.card_mirror, False, False, 0)

        # Auto-save status feedback label
        self.lbl_sync_mode_feedback = Gtk.Label()
        self.lbl_sync_mode_feedback.set_xalign(0)
        box.pack_start(self.lbl_sync_mode_feedback, False, False, 0)

        # Mount Location Card
        mount_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        mount_card.get_style_context().add_class("card")

        lbl_mount_title = Gtk.Label()
        lbl_mount_title.set_markup("<b>Google Drive Mount Location</b>")
        lbl_mount_title.set_xalign(0)
        mount_card.pack_start(lbl_mount_title, False, False, 0)

        lbl_mount_desc = Gtk.Label(
            label="Location in your Linux filesystem where Google Drive is mounted and accessible:"
        )
        lbl_mount_desc.set_xalign(0)
        mount_card.pack_start(lbl_mount_desc, False, False, 0)

        mount_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.entry_mount = Gtk.Entry()
        self.entry_mount.set_hexpand(True)
        self.entry_mount.connect("changed", self._on_mount_entry_changed)
        mount_row.pack_start(self.entry_mount, True, True, 0)

        btn_browse_mount = Gtk.Button(label="Browse...")
        btn_browse_mount.connect("clicked", self._on_browse_mount_point)
        mount_row.pack_start(btn_browse_mount, False, False, 0)

        btn_open_mount = Gtk.Button(label="📁 Open in Files")
        btn_open_mount.set_tooltip_text("Open mount location in file manager")
        btn_open_mount.connect("clicked", self._on_open_mount_folder)
        mount_row.pack_start(btn_open_mount, False, False, 0)
        mount_card.pack_start(mount_row, False, False, 0)

        self.lbl_mount_status = Gtk.Label()
        self.lbl_mount_status.set_xalign(0)
        mount_card.pack_start(self.lbl_mount_status, False, False, 0)

        box.pack_start(mount_card, False, False, 0)
        return scrolled

    def _on_sync_mode_toggled(self, widget, mode_name: str):
        """Immediately auto-saves the selected sync mode and notifies the daemon."""
        if not widget.get_active() or self._updating_widgets:
            return

        # Update visual card highlights
        if mode_name == "stream":
            self.card_stream.get_style_context().add_class("selected")
            self.card_mirror.get_style_context().remove_class("selected")
        else:
            self.card_mirror.get_style_context().add_class("selected")
            self.card_stream.get_style_context().remove_class("selected")

        # Save to config for active account
        self.config = load_config()
        aid = self.current_account_id or get_active_account_id(self.config)
        if aid in self.config.get("accounts", {}):
            self.config["accounts"][aid]["sync_mode"] = mode_name
        self.config.setdefault("storage", {})["sync_mode"] = mode_name
        save_config(self.config)

        # Ensure mount point exists
        mp = self.entry_mount.get_text().strip()
        ensure_mount_point_exists(mp)

        # Notify daemon via IPC Tag 9
        if is_daemon_running():
            try:
                send_ipc(McpIpcRequest(request_id=1, set_sync_mode={"sync_mode": mode_name, "account_id": aid}))
            except Exception as e:
                print(f"Warning: Failed to send set_sync_mode IPC: {e}")

        # Show feedback
        self.lbl_sync_mode_feedback.set_markup(
            f"<span foreground='#137333'><b>✓ Saved:</b> Sync mode set to <i>{mode_name.capitalize()} files</i></span>"
        )
        GLib.timeout_add(3000, lambda: self.lbl_sync_mode_feedback.set_text("") or False)

    def _on_mount_entry_changed(self, widget):
        if self._updating_widgets:
            return
        mp = self.entry_mount.get_text().strip()
        if mp:
            ensure_mount_point_exists(mp)
            self._update_mount_status_label(mp)

            # Persist to config
            self.config = load_config()
            aid = self.current_account_id or get_active_account_id(self.config)
            if aid in self.config.get("accounts", {}):
                self.config["accounts"][aid]["mount_point"] = mp
            self.config.setdefault("storage", {})["mount_point"] = mp
            save_config(self.config)

    def _update_mount_status_label(self, mount_point: str):
        p = Path(mount_point).expanduser()
        my_drive = p / "My Drive"
        if my_drive.exists():
            self.lbl_mount_status.set_markup(
                f"<span foreground='#137333'>✓ Mount directory active at: <tt>{my_drive}</tt></span>"
            )
        elif p.exists():
            self.lbl_mount_status.set_markup(
                f"<span foreground='#137333'>✓ Directory exists at: <tt>{p}</tt></span>"
            )
        else:
            self.lbl_mount_status.set_markup(
                f"<span foreground='#c5221f'>⚠️ Directory will be created upon start: <tt>{p}</tt></span>"
            )

    def _on_browse_mount_point(self, widget):
        dialog = Gtk.FileChooserDialog(
            title="Choose Google Drive Mount Folder",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        if dialog.run() == Gtk.ResponseType.OK:
            chosen = dialog.get_filename()
            self.entry_mount.set_text(chosen)
            ensure_mount_point_exists(chosen)
            self._update_mount_status_label(chosen)
        dialog.destroy()

    def _on_open_mount_folder(self, widget):
        mp = self.entry_mount.get_text().strip()
        if mp:
            p = Path(mp).expanduser()
            my_drive = p / "My Drive"
            target = str(my_drive if my_drive.exists() else p)
            ensure_mount_point_exists(target)
            try:
                subprocess.Popen(["xdg-open", target])
            except Exception:
                try:
                    subprocess.Popen(["gio", "open", target])
                except Exception as e:
                    print(f"Failed to open folder: {e}")

    # -------------------------------------------------------------------------
    # Tab 2: Folders from your computer (Continuous Folder Sync)
    # -------------------------------------------------------------------------
    def _create_folders_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(20)

        lbl_title = Gtk.Label()
        lbl_title.set_markup("<b><big>Folders from your computer</big></b>")
        lbl_title.set_xalign(0)
        box.pack_start(lbl_title, False, False, 0)

        lbl_desc = Gtk.Label(
            label="Continuously sync and back up specific directories from this Linux computer to Google Drive.\n"
                  "Files in these folders sync automatically in the background."
        )
        lbl_desc.set_xalign(0)
        box.pack_start(lbl_desc, False, False, 0)

        # TreeView: Path (str), Mode (str), Status (str), Last Synced (str)
        self.folder_store = Gtk.ListStore(str, str, str, str)
        self.folder_tree = Gtk.TreeView(model=self.folder_store)
        self.folder_tree.get_selection().connect("changed", self._on_folder_selection_changed)

        cols = [
            ("Folder Path", 0, True),
            ("Sync Mode", 1, False),
            ("Sync Status", 2, False),
            ("Last Synced", 3, False),
        ]
        for title, idx, expand in cols:
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=idx)
            column.set_resizable(True)
            if expand:
                column.set_expand(True)
            self.folder_tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(200)
        scroll.add(self.folder_tree)
        box.pack_start(scroll, True, True, 0)

        # Action Buttons Row
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        btn_add = Gtk.Button(label="+ Add folder...")
        btn_add.get_style_context().add_class("suggested-action")
        btn_add.connect("clicked", self._on_add_folder)
        btn_box.pack_start(btn_add, False, False, 0)

        self.btn_remove_folder = Gtk.Button(label="- Remove folder")
        self.btn_remove_folder.get_style_context().add_class("destructive-action")
        self.btn_remove_folder.set_sensitive(False)  # Enabled on row selection
        self.btn_remove_folder.connect("clicked", self._on_remove_folder)
        btn_box.pack_start(self.btn_remove_folder, False, False, 0)

        self.btn_open_folder = Gtk.Button(label="📁 Open Folder")
        self.btn_open_folder.set_sensitive(False)
        self.btn_open_folder.connect("clicked", self._on_open_selected_folder)
        btn_box.pack_start(self.btn_open_folder, False, False, 0)

        self.btn_sync_folder = Gtk.Button(label="↻ Sync Now")
        self.btn_sync_folder.set_tooltip_text("Trigger immediate sync of tracked folders")
        self.btn_sync_folder.connect("clicked", self._on_sync_tracked_now)
        btn_box.pack_start(self.btn_sync_folder, False, False, 0)

        self.lbl_folder_action_status = Gtk.Label()
        self.lbl_folder_action_status.set_xalign(0)
        btn_box.pack_start(self.lbl_folder_action_status, False, False, 8)

        box.pack_start(btn_box, False, False, 0)
        return box

    def _on_folder_selection_changed(self, selection):
        model, treeiter = selection.get_selected()
        has_sel = (treeiter is not None)
        self.btn_remove_folder.set_sensitive(has_sel)
        self.btn_open_folder.set_sensitive(has_sel)

    def _load_folders_into_store(self):
        """Loads tracked folders for the current account into the TreeView."""
        self.folder_store.clear()
        aid = self.current_account_id

        # Query live from daemon if running
        if is_daemon_running():
            try:
                resp = send_ipc(McpIpcRequest(request_id=1, list_tracked_folders={"account_id": aid} if aid else {}))
                paths = resp.list_tracked_folders.get("paths", []) if resp.list_tracked_folders else []
                for p in paths:
                    st_resp = send_ipc(McpIpcRequest(request_id=2, get_sync_status={"path": p}))
                    st = "synced"
                    if st_resp and st_resp.get_sync_status:
                        st = st_resp.get_sync_status.get("status", "synced")
                    self.folder_store.append([p, "Mirror", st.capitalize(), "Just now"])
                if paths:
                    return
            except Exception as e:
                print(f"Warning: Daemon query failed in folder list: {e}")

        # Fallback to local config
        folders = get_tracked_folders(aid)
        for f in folders:
            p = f.get("path") if isinstance(f, dict) else str(f)
            mode = f.get("sync_mode", "mirror").capitalize() if isinstance(f, dict) else "Mirror"
            st = f.get("status", "synced").capitalize() if isinstance(f, dict) else "Synced"
            last_ts = f.get("last_synced") if isinstance(f, dict) else None
            self.folder_store.append([p, mode, st, format_relative_time(last_ts)])

    def _on_add_folder(self, widget):
        dialog = Gtk.FileChooserDialog(
            title="Select Folder to Sync with Google Drive",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            aid = self.current_account_id or "default"

            # 1. Add to daemon via IPC
            if is_daemon_running():
                try:
                    send_ipc(McpIpcRequest(request_id=1, add_folder_to_sync={"path": path, "account_id": aid}))
                except Exception as e:
                    print(f"Warning: IPC add_folder_to_sync failed: {e}")

            # 2. Add to config
            add_tracked_folder(path, sync_mode="mirror", account_id=aid)

            # 3. Reload list
            self._load_folders_into_store()

            # 4. Trigger immediate sync
            if is_daemon_running():
                try:
                    send_ipc(McpIpcRequest(request_id=10, trigger_sync={"account_id": aid}))
                except Exception:
                    pass

            self.lbl_folder_action_status.set_markup(
                f"<span foreground='#137333'>✓ Added <b>{Path(path).name}</b> to sync</span>"
            )
            GLib.timeout_add(3000, lambda: self.lbl_folder_action_status.set_text("") or False)
        dialog.destroy()

    def _on_remove_folder(self, widget):
        """Removes the selected folder from sync in both daemon SQLite and config.json."""
        selection = self.folder_tree.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return

        path = model[treeiter][0]
        aid = self.current_account_id

        # Confirmation dialog
        confirm = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Stop syncing '{Path(path).name}'?"
        )
        confirm.format_secondary_text(
            f"Path: {path}\n\nFiles will remain on Google Drive and your local drive, "
            "but updates will no longer be synchronized."
        )
        response = confirm.run()
        confirm.destroy()

        if response != Gtk.ResponseType.OK:
            return

        # 1. Remove from daemon via IPC Tag 8
        if is_daemon_running():
            try:
                resp = send_ipc(McpIpcRequest(request_id=8, remove_folder_from_sync={"path": path, "account_id": aid}))
            except Exception as e:
                print(f"Warning: IPC remove_folder_from_sync failed: {e}")

        # 2. Remove from config
        remove_tracked_folder(path, account_id=aid)

        # 3. Reload folder store
        self._load_folders_into_store()
        self.btn_remove_folder.set_sensitive(False)
        self.btn_open_folder.set_sensitive(False)

        self.lbl_folder_action_status.set_markup(
            f"<span foreground='#c5221f'>✓ Removed <b>{Path(path).name}</b> from sync</span>"
        )
        GLib.timeout_add(3000, lambda: self.lbl_folder_action_status.set_text("") or False)

    def _on_open_selected_folder(self, widget):
        selection = self.folder_tree.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter:
            path = model[treeiter][0]
            try:
                subprocess.Popen(["xdg-open", path])
            except Exception:
                try:
                    subprocess.Popen(["gio", "open", path])
                except Exception as e:
                    print(f"Failed to open path: {e}")

    def _on_sync_tracked_now(self, widget):
        aid = self.current_account_id
        if is_daemon_running():
            try:
                send_ipc(McpIpcRequest(request_id=10, trigger_sync={"account_id": aid}))
                self.lbl_folder_action_status.set_markup(
                    "<span foreground='#1a73e8'>↻ Sync started...</span>"
                )
            except Exception as e:
                self.lbl_folder_action_status.set_markup(
                    f"<span foreground='#c5221f'>Error: {e}</span>"
                )
        else:
            ensure_daemon_running(timeout=2.0)
            self.lbl_folder_action_status.set_markup(
                "<span foreground='#1a73e8'>Daemon started.</span>"
            )
        GLib.timeout_add(3000, lambda: self.lbl_folder_action_status.set_text("") or False)

    # -------------------------------------------------------------------------
    # Tab 3: Settings & Bandwidth
    # -------------------------------------------------------------------------
    def _create_settings_tab(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(20)
        scrolled.add(box)

        # Section 1: Account Profile & Storage Quota Card
        self.account_card = self._create_account_card()
        box.pack_start(self.account_card, False, False, 0)

        # Section 2: Cache & Storage Limits
        cache_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cache_card.get_style_context().add_class("card")

        lbl_cache_sec = Gtk.Label()
        lbl_cache_sec.set_markup("<b>Local Cache &amp; Storage</b>")
        lbl_cache_sec.set_xalign(0)
        cache_card.pack_start(lbl_cache_sec, False, False, 0)

        grid_cache = Gtk.Grid()
        grid_cache.set_column_spacing(12)
        grid_cache.set_row_spacing(8)
        cache_card.pack_start(grid_cache, False, False, 0)

        grid_cache.attach(Gtk.Label(label="Local Cache Folder:", xalign=0), 0, 0, 1, 1)
        self.entry_cache = Gtk.Entry()
        self.entry_cache.set_hexpand(True)
        self.entry_cache.set_text(self.config.get("storage", {}).get("cache_dir", ""))
        grid_cache.attach(self.entry_cache, 1, 0, 1, 1)

        btn_browse_cache = Gtk.Button(label="Browse...")
        btn_browse_cache.connect("clicked", self._on_browse_cache_dir)
        grid_cache.attach(btn_browse_cache, 2, 0, 1, 1)

        grid_cache.attach(Gtk.Label(label="Max Cache Size (GB):", xalign=0), 0, 1, 1, 1)
        self.spin_cache_limit = Gtk.SpinButton.new_with_range(1, 500, 1)
        self.spin_cache_limit.set_value(self.config.get("storage", {}).get("cache_size_limit_gb", 10))
        grid_cache.attach(self.spin_cache_limit, 1, 1, 1, 1)

        box.pack_start(cache_card, False, False, 0)

        # Section 3: Bandwidth Limits
        bw_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        bw_card.get_style_context().add_class("card")

        lbl_bw_sec = Gtk.Label()
        lbl_bw_sec.set_markup("<b>Bandwidth Limits (0 = Unlimited)</b>")
        lbl_bw_sec.set_xalign(0)
        bw_card.pack_start(lbl_bw_sec, False, False, 0)

        grid_bw = Gtk.Grid()
        grid_bw.set_column_spacing(12)
        grid_bw.set_row_spacing(8)
        bw_card.pack_start(grid_bw, False, False, 0)

        grid_bw.attach(Gtk.Label(label="Download Rate Limit (KB/s):", xalign=0), 0, 0, 1, 1)
        self.spin_down = Gtk.SpinButton.new_with_range(0, 100000, 50)
        self.spin_down.set_value(self.config.get("bandwidth", {}).get("download_rate_kbps", 0))
        grid_bw.attach(self.spin_down, 1, 0, 1, 1)

        grid_bw.attach(Gtk.Label(label="Upload Rate Limit (KB/s):", xalign=0), 0, 1, 1, 1)
        self.spin_up = Gtk.SpinButton.new_with_range(0, 100000, 50)
        self.spin_up.set_value(self.config.get("bandwidth", {}).get("upload_rate_kbps", 0))
        grid_bw.attach(self.spin_up, 1, 1, 1, 1)

        box.pack_start(bw_card, False, False, 0)

        # Section 4: System Options
        sys_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sys_card.get_style_context().add_class("card")

        lbl_sys_sec = Gtk.Label()
        lbl_sys_sec.set_markup("<b>System Options</b>")
        lbl_sys_sec.set_xalign(0)
        sys_card.pack_start(lbl_sys_sec, False, False, 0)

        self.chk_autostart = Gtk.CheckButton(label="Launch Google Drive automatically on system startup")
        self.chk_autostart.set_active(self.config.get("system", {}).get("autostart", True))
        sys_card.pack_start(self.chk_autostart, False, False, 0)

        self.chk_notify = Gtk.CheckButton(label="Show desktop notifications for file sync activity")
        self.chk_notify.set_active(self.config.get("system", {}).get("notifications", True))
        sys_card.pack_start(self.chk_notify, False, False, 0)

        box.pack_start(sys_card, False, False, 0)

        # Section 5: Custom Google Cloud Credentials Expander
        box.pack_start(self._create_custom_creds_expander(), False, False, 0)

        return scrolled

    def _create_account_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("card")
        self.account_card_box = card
        self._populate_account_card()
        return card

    def _populate_account_card(self):
        """Populates the Google Account details card for the current account."""
        for child in self.account_card_box.get_children():
            self.account_card_box.remove(child)

        self.config = load_config()
        acc = get_account_by_id(self.current_account_id, self.config) or {}
        is_auth = acc.get("authenticated", False) and oauth_manager.is_authenticated(self.current_account_id)

        lbl_hdr = Gtk.Label()
        lbl_hdr.set_markup("<b>Google Account &amp; Storage Quota</b>")
        lbl_hdr.set_xalign(0)
        self.account_card_box.pack_start(lbl_hdr, False, False, 0)

        if is_auth:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            lbl_icon = Gtk.Label()
            lbl_icon.set_markup("<span font='24'>👤</span>")
            row.pack_start(lbl_icon, False, False, 0)

            vbox_user = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            lbl_name = Gtk.Label()
            name_str = acc.get("name") or "Google Account"
            email_str = acc.get("email") or ""
            lbl_name.set_markup(f"<b><big>{name_str}</big></b>  <tt>&lt;{email_str}&gt;</tt>")
            lbl_name.set_xalign(0)
            vbox_user.pack_start(lbl_name, False, False, 0)

            lbl_status = Gtk.Label()
            lbl_status.set_markup("<span foreground='#137333'><b>✓ Connected &amp; Synchronized</b></span>")
            lbl_status.set_xalign(0)
            vbox_user.pack_start(lbl_status, False, False, 0)
            row.pack_start(vbox_user, True, True, 0)

            btn_logout = Gtk.Button(label="Sign Out")
            btn_logout.get_style_context().add_class("destructive-action")
            btn_logout.connect("clicked", self._on_account_logout)
            row.pack_start(btn_logout, False, False, 0)
            self.account_card_box.pack_start(row, False, False, 0)

            # Quota Progress Bar
            quota = acc.get("storage_quota", {})
            limit = quota.get("limit", 0)
            usage = quota.get("usage", 0)
            if limit > 0:
                usage_gb = usage / (1024**3)
                limit_gb = limit / (1024**3)
                fraction = min(1.0, max(0.0, usage_gb / limit_gb))
                pct = fraction * 100

                lbl_quota = Gtk.Label()
                lbl_quota.set_markup(f"<b>Cloud Storage:</b> {usage_gb:.2f} GB of {limit_gb:.1f} GB used ({pct:.1f}%)")
                lbl_quota.set_xalign(0)
                self.account_card_box.pack_start(lbl_quota, False, False, 4)

                progress = Gtk.ProgressBar()
                progress.set_fraction(fraction)
                progress.set_show_text(True)
                progress.set_text(f"{pct:.1f}%")
                self.account_card_box.pack_start(progress, False, False, 0)
            else:
                lbl_quota = Gtk.Label(label="Cloud Storage: Active")
                lbl_quota.set_xalign(0)
                self.account_card_box.pack_start(lbl_quota, False, False, 0)

        else:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            lbl_icon = Gtk.Label()
            lbl_icon.set_markup("<span font='24'>🔒</span>")
            row.pack_start(lbl_icon, False, False, 0)

            vbox_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            lbl_msg = Gtk.Label()
            lbl_msg.set_markup("<b>No Google Account Connected</b>")
            lbl_msg.set_xalign(0)
            vbox_info.pack_start(lbl_msg, False, False, 0)

            lbl_sub = Gtk.Label(label="Sign in via browser to connect your Google Drive and enable cloud sync.")
            lbl_sub.set_xalign(0)
            vbox_info.pack_start(lbl_sub, False, False, 0)
            row.pack_start(vbox_info, True, True, 0)

            btn_signin = Gtk.Button(label="Sign in with Google...")
            btn_signin.get_style_context().add_class("suggested-action")
            btn_signin.connect("clicked", self._on_add_account_clicked)
            row.pack_start(btn_signin, False, False, 0)
            self.account_card_box.pack_start(row, False, False, 0)

        self.account_card_box.show_all()

    def _on_browse_cache_dir(self, widget):
        dialog = Gtk.FileChooserDialog(
            title="Choose Local Cache Directory",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        if dialog.run() == Gtk.ResponseType.OK:
            self.entry_cache.set_text(dialog.get_filename())
        dialog.destroy()

    def _create_custom_creds_expander(self) -> Gtk.Widget:
        expander = Gtk.Expander(label="Advanced: Custom Google Cloud OAuth 2.0 Credentials")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)

        lbl_desc = Gtk.Label(
            label="You can use your own Google Cloud Console OAuth 2.0 Client ID and Secret (Desktop app),\n"
                  "or leave blank to use the official Google Drive desktop credentials."
        )
        lbl_desc.set_xalign(0)
        box.pack_start(lbl_desc, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(10)
        grid.set_row_spacing(8)

        grid.attach(Gtk.Label(label="Client ID:", xalign=0), 0, 0, 1, 1)
        self.entry_client_id = Gtk.Entry()
        self.entry_client_id.set_hexpand(True)
        custom_id = self.config.get("oauth", {}).get("custom_client_id", "")
        self.entry_client_id.set_text(custom_id)
        self.entry_client_id.set_placeholder_text("xxxx.apps.googleusercontent.com")
        grid.attach(self.entry_client_id, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Client Secret:", xalign=0), 0, 1, 1, 1)
        self.entry_client_secret = Gtk.Entry()
        self.entry_client_secret.set_hexpand(True)
        self.entry_client_secret.set_visibility(False)
        custom_secret = self.config.get("oauth", {}).get("custom_client_secret", "")
        self.entry_client_secret.set_text(custom_secret)
        grid.attach(self.entry_client_secret, 1, 1, 1, 1)

        box.pack_start(grid, False, False, 0)

        btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_import = Gtk.Button(label="Import client_secrets.json...")
        btn_import.connect("clicked", self._on_import_secrets)
        btn_bar.pack_start(btn_import, False, False, 0)

        btn_save_creds = Gtk.Button(label="Save Custom Credentials")
        btn_save_creds.connect("clicked", self._on_save_custom_creds)
        btn_bar.pack_start(btn_save_creds, False, False, 0)

        btn_reset_creds = Gtk.Button(label="Reset to Default")
        btn_reset_creds.connect("clicked", self._on_reset_creds)
        btn_bar.pack_start(btn_reset_creds, False, False, 0)

        box.pack_start(btn_bar, False, False, 0)
        expander.add(box)
        return expander

    def _on_import_secrets(self, widget):
        dialog = Gtk.FileChooserDialog(
            title="Select Google Cloud client_secrets.json",
            parent=self,
            action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        filter_json = Gtk.FileFilter()
        filter_json.set_name("JSON Files")
        filter_json.add_mime_type("application/json")
        filter_json.add_pattern("*.json")
        dialog.add_filter(filter_json)

        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            try:
                cid, secret = oauth_manager.import_credentials_file(path)
                self.entry_client_id.set_text(cid)
                self.entry_client_secret.set_text(secret)
                msg = Gtk.MessageDialog(
                    transient_for=self, flags=0,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="Credentials Imported"
                )
                msg.format_secondary_text(f"Imported credentials for Client ID:\n{cid}")
                msg.run()
                msg.destroy()
            except Exception as e:
                err_dialog = Gtk.MessageDialog(
                    transient_for=self, flags=0,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Import Failed"
                )
                err_dialog.format_secondary_text(str(e))
                err_dialog.run()
                err_dialog.destroy()
        dialog.destroy()

    def _on_save_custom_creds(self, widget):
        cid = self.entry_client_id.get_text().strip()
        secret = self.entry_client_secret.get_text().strip()
        if not cid or not secret:
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="Missing Credentials"
            )
            dialog.format_secondary_text("Please enter both a Client ID and Client Secret.")
            dialog.run()
            dialog.destroy()
            return

        oauth_manager.set_custom_client_credentials(cid, secret)
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Credentials Saved"
        )
        dialog.format_secondary_text("Custom Google Cloud credentials have been saved.")
        dialog.run()
        dialog.destroy()

    def _on_reset_creds(self, widget):
        oauth_manager.reset_to_default_credentials()
        self.entry_client_id.set_text("")
        self.entry_client_secret.set_text("")
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Reset to Defaults"
        )
        dialog.format_secondary_text("Using default official Google Drive desktop credentials.")
        dialog.run()
        dialog.destroy()

    # -------------------------------------------------------------------------
    # Tab 4: MCP Protocol & Diagnostics
    # -------------------------------------------------------------------------
    def _create_mcp_tab(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(20)
        scrolled.add(box)

        lbl_title = Gtk.Label()
        lbl_title.set_markup("<b><big>Model Context Protocol &amp; IPC Status</big></b>")
        lbl_title.set_xalign(0)
        box.pack_start(lbl_title, False, False, 0)

        diag_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        diag_card.get_style_context().add_class("card")

        self.lbl_mcp_info = Gtk.Label()
        self.lbl_mcp_info.set_xalign(0)
        self._refresh_mcp_status()
        diag_card.pack_start(self.lbl_mcp_info, False, False, 0)
        box.pack_start(diag_card, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_test = Gtk.Button(label="Test IPC Connection")
        btn_test.connect("clicked", self._on_test_ipc)
        btn_box.pack_start(btn_test, False, False, 0)

        btn_daemon = Gtk.Button(label="Restart Background Daemon")
        btn_daemon.connect("clicked", self._on_restart_daemon)
        btn_box.pack_start(btn_daemon, False, False, 0)

        box.pack_start(btn_box, False, False, 0)
        return scrolled

    def _refresh_mcp_status(self):
        running = is_daemon_running()
        sock = get_socket_path()
        status_text = (
            f"<b>Daemon State:</b> {'<span color=\"#137333\">🟢 RUNNING</span>' if running else '<span color=\"#c5221f\">🔴 STOPPED</span>'}\n"
            f"<b>IPC Socket:</b> <tt>{sock}</tt>\n"
            f"<b>Configuration File:</b> <tt>{CONFIG_FILE}</tt>\n\n"
            "<b>Registered MCP Tools for AI Agents:</b>\n"
            "  1. <tt>dfd_get_sync_status</tt> — Inspect file/folder sync state\n"
            "  2. <tt>dfd_add_folder_to_sync</tt> — Track new folder for sync\n"
            "  3. <tt>dfd_get_drive_url</tt> — Get shareable Google Drive web URL\n"
            "  4. <tt>dfd_list_tracked_folders</tt> — List all synchronized paths\n"
            "  5. <tt>dfd_open_acl_dialog</tt> — Launch interactive sharing permissions UI\n"
            "  6. <tt>dfd_get_account_info</tt> — Query account profile and quota"
        )
        if hasattr(self, 'lbl_mcp_info'):
            self.lbl_mcp_info.set_markup(status_text)

    def _on_test_ipc(self, widget):
        try:
            resp = send_ipc(McpIpcRequest(request_id=1, list_tracked_folders={}))
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="IPC Connection Succeeded!"
            )
            dialog.format_secondary_text(f"Connected to {get_socket_path()}.\nDaemon is responsive and ready.")
            dialog.run()
            dialog.destroy()
        except Exception as e:
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="IPC Connection Failed"
            )
            dialog.format_secondary_text(str(e))
            dialog.run()
            dialog.destroy()

    def _on_restart_daemon(self, widget):
        ensure_daemon_running(timeout=3.0)
        self._refresh_mcp_status()
        self._update_daemon_pill()
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Daemon Check Complete"
        )
        st = "Running" if is_daemon_running() else "Could not start"
        dialog.format_secondary_text(f"Daemon Status: {st} on {get_socket_path()}")
        dialog.run()
        dialog.destroy()

    # -------------------------------------------------------------------------
    # Authentication & Multi-Account Operations
    # -------------------------------------------------------------------------
    def _on_add_account_clicked(self, widget):
        """Initiates OAuth flow in browser to connect a new Google Account."""
        self.btn_add_account.set_sensitive(False)

        # Dialog with spinner showing waiting status
        progress_dialog = Gtk.Dialog(
            title="Connecting Google Account",
            parent=self,
            flags=Gtk.DialogFlags.MODAL
        )
        progress_dialog.set_default_size(360, 160)
        vbox = progress_dialog.get_content_area()
        vbox.set_spacing(12)
        vbox.set_border_width(18)

        spinner = Gtk.Spinner()
        spinner.start()
        vbox.pack_start(spinner, True, True, 0)

        lbl_status = Gtk.Label(label="Opening browser for Google authentication...\nPlease sign in to authorize.")
        lbl_status.set_justify(Gtk.Justification.CENTER)
        vbox.pack_start(lbl_status, False, False, 0)
        progress_dialog.show_all()

        def worker():
            try:
                res = oauth_manager.start_oauth_flow(
                    open_browser=True,
                    url_callback=lambda u: GLib.idle_add(lbl_status.set_text, "Waiting for approval in browser...")
                )
                GLib.idle_add(self._on_oauth_finished, res, None, progress_dialog)
            except Exception as e:
                GLib.idle_add(self._on_oauth_finished, None, str(e), progress_dialog)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _on_oauth_finished(self, res, error, progress_dialog):
        progress_dialog.destroy()
        self.btn_add_account.set_sensitive(True)

        if error:
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Authentication Failed"
            )
            dialog.format_secondary_text(error)
            dialog.run()
            dialog.destroy()
        else:
            profile = res.get("profile", {})
            email = profile.get("email", "")
            name = profile.get("name", "")

            # Reload config, update accounts combo and select new account
            self.config = load_config()
            self.current_account_id = email or get_active_account_id(self.config)
            self._refresh_accounts_combo()
            self._load_account_data(self.current_account_id)

            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Account Connected!"
            )
            dialog.format_secondary_text(f"Successfully connected {name} ({email}).\nSync options configured.")
            dialog.run()
            dialog.destroy()

    def _on_disconnect_account_clicked(self, widget):
        aid = self.current_account_id
        if not aid:
            return

        acc = get_account_by_id(aid, self.config) or {}
        email = acc.get("email") or aid

        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Disconnect '{email}'?"
        )
        dialog.format_secondary_text(
            "This will remove the account from Google Drive for Linux. "
            "Local files on your computer will not be deleted."
        )
        res = dialog.run()
        dialog.destroy()

        if res == Gtk.ResponseType.OK:
            oauth_manager.revoke_tokens(aid)
            remove_account(aid)
            self.config = load_config()
            self.current_account_id = get_active_account_id(self.config)
            self._refresh_accounts_combo()
            self._load_account_data(self.current_account_id)

    def _on_account_logout(self, widget):
        self._on_disconnect_account_clicked(widget)

    # -------------------------------------------------------------------------
    # Global Actions, Save & Periodic Polling
    # -------------------------------------------------------------------------
    def _on_global_sync_clicked(self, widget):
        if is_daemon_running():
            try:
                send_ipc(McpIpcRequest(request_id=10, trigger_sync={}))
            except Exception:
                pass
        else:
            ensure_daemon_running(timeout=2.0)
        self._load_folders_into_store()

    def _on_save_clicked(self, widget):
        """Saves all preferences cleanly without any attribute errors."""
        self.config = load_config()
        aid = self.current_account_id or get_active_account_id(self.config)

        mode = "stream" if self.radio_stream.get_active() else "mirror"
        mount_point = self.entry_mount.get_text().strip()
        cache_dir = self.entry_cache.get_text().strip()
        cache_limit = int(self.spin_cache_limit.get_value())

        # Update current account
        if aid in self.config.get("accounts", {}):
            acc = self.config["accounts"][aid]
            acc["sync_mode"] = mode
            acc["mount_point"] = mount_point
            acc["cache_dir"] = cache_dir
            acc["cache_size_limit_gb"] = cache_limit

        # Update global storage / bandwidth / system settings
        self.config["storage"]["sync_mode"] = mode
        self.config["storage"]["mount_point"] = mount_point
        self.config["storage"]["cache_dir"] = cache_dir
        self.config["storage"]["cache_size_limit_gb"] = cache_limit

        self.config["bandwidth"]["download_rate_kbps"] = int(self.spin_down.get_value())
        self.config["bandwidth"]["upload_rate_kbps"] = int(self.spin_up.get_value())
        self.config["system"]["autostart"] = self.chk_autostart.get_active()
        self.config["system"]["notifications"] = self.chk_notify.get_active()

        ensure_mount_point_exists(mount_point)
        save_config(self.config)

        # Notify daemon via IPC
        if is_daemon_running():
            try:
                send_ipc(McpIpcRequest(request_id=9, set_sync_mode={"sync_mode": mode, "account_id": aid}))
            except Exception:
                pass

        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Preferences Saved"
        )
        dialog.format_secondary_text(f"Settings saved successfully to {CONFIG_FILE}.")
        dialog.run()
        dialog.destroy()

    def _update_daemon_pill(self):
        """Updates the status pill in the HeaderBar."""
        running = is_daemon_running()
        if running:
            self.lbl_daemon_pill.get_style_context().remove_class("badge-daemon-stopped")
            self.lbl_daemon_pill.get_style_context().add_class("badge-daemon-running")
            self.lbl_daemon_pill.set_markup("<b><span foreground='#137333'>● Running</span></b>")
        else:
            self.lbl_daemon_pill.get_style_context().remove_class("badge-daemon-running")
            self.lbl_daemon_pill.get_style_context().add_class("badge-daemon-stopped")
            self.lbl_daemon_pill.set_markup("<b><span foreground='#c5221f'>● Daemon Stopped</span></b>")

    def _on_periodic_poll(self) -> bool:
        """Periodic background callback to update live status indicators."""
        self._update_daemon_pill()
        self._refresh_mcp_status()
        return True  # Return True to keep the timer firing


def main():
    ensure_daemon_running(timeout=2.0)
    win = PreferencesWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()

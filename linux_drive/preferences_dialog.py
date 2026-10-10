#!/usr/bin/env python3
"""
Google Drive Linux - Native GTK3 Preferences & Configuration Dialog
Provides a full desktop interface matching Google Drive Desktop settings:
- Sync options (Stream vs Mirror)
- Local folder backup & tracking
- Account settings, cache sizing, bandwidth limits
- Model Context Protocol (MCP) server & IPC diagnostics
"""

import os
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

from linux_drive.config import (
    load_config,
    save_config,
    CONFIG_FILE
)
from linux_drive.drivectl import (
    is_daemon_running,
    send_ipc,
    get_socket_path
)
from linux_drive.ipc_wire import McpIpcRequest
from linux_drive.oauth import oauth_manager
import threading

ICON_PATH = ROOT_DIR / "assets" / "icons" / "google-drive.png"

class PreferencesWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Google Drive Preferences")
        self.set_default_size(720, 540)
        self.set_position(Gtk.WindowPosition.CENTER)

        try:
            Gtk.Window.set_default_icon_name("google-drive")
        except Exception:
            pass

        for candidate in [
            ROOT_DIR / "assets" / "icons" / "google-drive.png",
            Path("/usr/share/icons/hicolor/512x512/apps/google-drive.png"),
            Path("/usr/share/pixmaps/google-drive.png")
        ]:
            if candidate.exists():
                try:
                    self.set_icon_from_file(str(candidate))
                    break
                except Exception:
                    pass

        self.config = load_config()

        # Header bar
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = "Google Drive Preferences"
        header.props.subtitle = "Settings & Synchronization"
        self.set_titlebar(header)

        # Main notebook (tabs)
        notebook = Gtk.Notebook()
        notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.add(notebook)

        # Tab 1: Google Drive Sync Mode
        tab_drive = self._create_drive_tab()
        notebook.append_page(tab_drive, Gtk.Label(label="Google Drive"))

        # Tab 2: Folders from your computer
        tab_folders = self._create_folders_tab()
        notebook.append_page(tab_folders, Gtk.Label(label="My Computer"))

        # Tab 3: Settings & Bandwidth
        tab_settings = self._create_settings_tab()
        notebook.append_page(tab_settings, Gtk.Label(label="Settings"))

        # Tab 4: MCP Server & IPC
        tab_mcp = self._create_mcp_tab()
        notebook.append_page(tab_mcp, Gtk.Label(label="MCP & IPC"))

        # Bottom Bar for Actions
        save_btn = Gtk.Button(label="Save Preferences")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self._on_save)
        header.pack_end(save_btn)

    def _create_drive_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(24)

        # Section Title
        lbl_title = Gtk.Label()
        lbl_title.set_markup("<b><big>Google Drive Syncing Options</big></b>")
        lbl_title.set_xalign(0)
        box.pack_start(lbl_title, False, False, 0)

        lbl_desc = Gtk.Label(
            label="Choose how your cloud files are stored and synchronized on this Linux system."
        )
        lbl_desc.set_xalign(0)
        box.pack_start(lbl_desc, False, False, 0)

        # Radio options: Stream vs Mirror
        sync_mode = self.config.get("storage", {}).get("sync_mode", "stream")

        self.radio_stream = Gtk.RadioButton.new_with_label(None, "Stream files (Recommended)")
        self.radio_stream.set_active(sync_mode == "stream")
        box.pack_start(self.radio_stream, False, False, 0)

        desc_stream = Gtk.Label(
            label="  • Store all your Google Drive files in the cloud only.\n"
                  "  • Files are fetched on-demand via virtual mount (~/GoogleDrive).\n"
                  "  • Uses minimal hard drive space."
        )
        desc_stream.set_xalign(0)
        box.pack_start(desc_stream, False, False, 0)

        self.radio_mirror = Gtk.RadioButton.new_with_label_from_widget(
            self.radio_stream, "Mirror files"
        )
        self.radio_mirror.set_active(sync_mode == "mirror")
        box.pack_start(self.radio_mirror, False, False, 0)

        desc_mirror = Gtk.Label(
            label="  • Store all your Google Drive files both in the cloud and locally.\n"
                  "  • All files are automatically available offline.\n"
                  "  • Uses more local hard drive space."
        )
        desc_mirror.set_xalign(0)
        box.pack_start(desc_mirror, False, False, 0)

        # Separator
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 8)

        # Mount Point selection
        mount_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        lbl_mount = Gtk.Label(label="Google Drive Mount Location:")
        lbl_mount.set_xalign(0)
        mount_box.pack_start(lbl_mount, False, False, 0)

        self.entry_mount = Gtk.Entry()
        self.entry_mount.set_text(self.config.get("storage", {}).get("mount_point", ""))
        self.entry_mount.set_hexpand(True)
        mount_box.pack_start(self.entry_mount, True, True, 0)

        btn_browse_mount = Gtk.Button(label="Browse...")
        btn_browse_mount.connect("clicked", self._browse_mount_point)
        mount_box.pack_start(btn_browse_mount, False, False, 0)

        box.pack_start(mount_box, False, False, 0)

        return box

    def _browse_mount_point(self, widget):
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
            self.entry_mount.set_text(dialog.get_filename())
        dialog.destroy()

    def _create_folders_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(24)

        lbl_title = Gtk.Label()
        lbl_title.set_markup("<b><big>Folders from your computer</big></b>")
        lbl_title.set_xalign(0)
        box.pack_start(lbl_title, False, False, 0)

        lbl_desc = Gtk.Label(
            label="Continuously sync and back up specific directories from your computer to Google Drive."
        )
        lbl_desc.set_xalign(0)
        box.pack_start(lbl_desc, False, False, 0)

        # TreeView for tracked folders
        # Columns: Path (str), Sync Mode (str), Status (str)
        self.folder_store = Gtk.ListStore(str, str, str)
        self._load_folders_into_store()

        tree = Gtk.TreeView(model=self.folder_store)
        for i, col_title in enumerate(["Folder Path", "Sync Mode", "Sync Status"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(col_title, renderer, text=i)
            column.set_resizable(True)
            if i == 0:
                column.set_expand(True)
            tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(180)
        scroll.add(tree)
        box.pack_start(scroll, True, True, 0)
        self.folder_tree = tree

        # Action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_add = Gtk.Button(label="+ Add folder...")
        btn_add.connect("clicked", self._on_add_folder)
        btn_box.pack_start(btn_add, False, False, 0)

        btn_remove = Gtk.Button(label="- Remove folder")
        btn_remove.connect("clicked", self._on_remove_folder)
        btn_box.pack_start(btn_remove, False, False, 0)

        box.pack_start(btn_box, False, False, 0)
        return box

    def _load_folders_into_store(self):
        self.folder_store.clear()
        # Query daemon if running, else load from config
        if is_daemon_running():
            try:
                resp = send_ipc(McpIpcRequest(request_id=1, list_tracked_folders={}))
                paths = resp.list_tracked_folders.get("paths", []) if resp.list_tracked_folders else []
                for p in paths:
                    st_resp = send_ipc(McpIpcRequest(request_id=2, get_sync_status={"path": p}))
                    status = st_resp.get_sync_status.get("status", "synced") if st_resp.get_sync_status else "synced"
                    self.folder_store.append([p, "Mirror", status.capitalize()])
                return
            except Exception:
                pass
        for item in self.config.get("tracked_folders", []):
            self.folder_store.append([
                item.get("path", ""),
                item.get("sync_mode", "mirror").capitalize(),
                item.get("status", "synced").capitalize()
            ])

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
            if is_daemon_running():
                send_ipc(McpIpcRequest(request_id=1, add_folder_to_sync={"path": path, "account_id": "default"}))
            # Also add to config
            tracked = self.config.get("tracked_folders", [])
            if not any(f.get("path") == path for f in tracked):
                tracked.append({"path": path, "sync_mode": "mirror", "status": "synced"})
                self.config["tracked_folders"] = tracked
                save_config(self.config)
            self._load_folders_into_store()
        dialog.destroy()

    def _on_remove_folder(self, widget):
        selection = self.folder_tree.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter:
            path = model[treeiter][0]
            tracked = self.config.get("tracked_folders", [])
            self.config["tracked_folders"] = [f for f in tracked if f.get("path") != path]
            save_config(self.config)
            self._load_folders_into_store()

    def _create_settings_tab(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_border_width(24)
        scrolled.add(box)

        # Section 1: Google Account Connection Card
        box.pack_start(self._create_account_card(), False, False, 0)

        # Section 2: Custom Google Cloud Credentials (Collapsible)
        box.pack_start(self._create_custom_creds_expander(), False, False, 0)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        # Section 3: Cache & Storage Limits
        grid = Gtk.Grid()
        grid.set_column_spacing(16)
        grid.set_row_spacing(12)
        box.pack_start(grid, False, False, 0)

        lbl_cache = Gtk.Label()
        lbl_cache.set_markup("<b>Cache &amp; Storage Limits</b>")
        lbl_cache.set_xalign(0)
        grid.attach(lbl_cache, 0, 0, 2, 1)

        grid.attach(Gtk.Label(label="Local Cache Folder:", xalign=0), 0, 1, 1, 1)
        self.entry_cache = Gtk.Entry()
        self.entry_cache.set_text(self.config.get("storage", {}).get("cache_dir", ""))
        self.entry_cache.set_hexpand(True)
        grid.attach(self.entry_cache, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Max Cache Size (GB):", xalign=0), 0, 2, 1, 1)
        self.spin_cache_limit = Gtk.SpinButton.new_with_range(1, 500, 1)
        self.spin_cache_limit.set_value(self.config.get("storage", {}).get("cache_size_limit_gb", 10))
        grid.attach(self.spin_cache_limit, 1, 2, 1, 1)

        # Section 4: Bandwidth Limits
        lbl_bw = Gtk.Label()
        lbl_bw.set_markup("<b>Bandwidth Limits (0 = Unlimited)</b>")
        lbl_bw.set_xalign(0)
        grid.attach(lbl_bw, 0, 3, 2, 1)

        grid.attach(Gtk.Label(label="Download Rate Limit (KB/s):", xalign=0), 0, 4, 1, 1)
        self.spin_down = Gtk.SpinButton.new_with_range(0, 100000, 50)
        self.spin_down.set_value(self.config.get("bandwidth", {}).get("download_rate_kbps", 0))
        grid.attach(self.spin_down, 1, 4, 1, 1)

        grid.attach(Gtk.Label(label="Upload Rate Limit (KB/s):", xalign=0), 0, 5, 1, 1)
        self.spin_up = Gtk.SpinButton.new_with_range(0, 100000, 50)
        self.spin_up.set_value(self.config.get("bandwidth", {}).get("upload_rate_kbps", 0))
        grid.attach(self.spin_up, 1, 5, 1, 1)

        # Section 5: System Options
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        self.chk_autostart = Gtk.CheckButton(label="Launch Google Drive on system startup")
        self.chk_autostart.set_active(self.config.get("system", {}).get("autostart", True))
        box.pack_start(self.chk_autostart, False, False, 0)

        self.chk_notify = Gtk.CheckButton(label="Show desktop notifications for file sync activity")
        self.chk_notify.set_active(self.config.get("system", {}).get("notifications", True))
        box.pack_start(self.chk_notify, False, False, 0)

        return scrolled

    def _create_account_card(self) -> Gtk.Widget:
        frame = Gtk.Frame()
        lbl_frame = Gtk.Label()
        lbl_frame.set_markup("<b>Google Account</b>")
        frame.set_label_widget(lbl_frame)

        self.account_card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.account_card_box.set_border_width(14)
        frame.add(self.account_card_box)
        self._populate_account_card()
        return frame

    def _populate_account_card(self):
        for child in self.account_card_box.get_children():
            self.account_card_box.remove(child)

        cfg = load_config()
        self.config = cfg
        acc = cfg.get("account", {})
        is_auth = acc.get("authenticated", False) and oauth_manager.is_authenticated()

        if is_auth:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            lbl_icon = Gtk.Label()
            lbl_icon.set_markup("<big>👤</big>")
            row.pack_start(lbl_icon, False, False, 0)

            vbox_user = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            lbl_name = Gtk.Label()
            name_str = acc.get("name") or "Google Account"
            email_str = acc.get("email") or ""
            lbl_name.set_markup(f"<b>{name_str}</b>  &lt;{email_str}&gt;")
            lbl_name.set_xalign(0)
            vbox_user.pack_start(lbl_name, False, False, 0)

            lbl_status = Gtk.Label()
            lbl_status.set_markup("<span foreground='#2e7d32'><b>✓ Authenticated &amp; Ready for Sync</b></span>")
            lbl_status.set_xalign(0)
            vbox_user.pack_start(lbl_status, False, False, 0)
            row.pack_start(vbox_user, True, True, 0)

            btn_logout = Gtk.Button(label="Sign Out")
            btn_logout.connect("clicked", self._on_account_logout)
            row.pack_start(btn_logout, False, False, 0)

            self.account_card_box.pack_start(row, False, False, 0)

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
                self.account_card_box.pack_start(lbl_quota, False, False, 2)

                progress = Gtk.ProgressBar()
                progress.set_fraction(fraction)
                progress.set_show_text(True)
                progress.set_text(f"{pct:.1f}%")
                self.account_card_box.pack_start(progress, False, False, 0)

        else:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            lbl_icon = Gtk.Label()
            lbl_icon.set_markup("<big>🔒</big>")
            row.pack_start(lbl_icon, False, False, 0)

            vbox_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            lbl_msg = Gtk.Label(label="No Google Account connected.")
            lbl_msg.set_xalign(0)
            vbox_info.pack_start(lbl_msg, False, False, 0)

            lbl_sub = Gtk.Label(label="Sign in via browser to connect your Google Drive and enable MCP tools.")
            lbl_sub.set_xalign(0)
            vbox_info.pack_start(lbl_sub, False, False, 0)
            row.pack_start(vbox_info, True, True, 0)

            self.btn_signin = Gtk.Button(label="Sign in with Google...")
            self.btn_signin.get_style_context().add_class("suggested-action")
            self.btn_signin.connect("clicked", self._on_sign_in_clicked)
            row.pack_start(self.btn_signin, False, False, 0)

            self.account_card_box.pack_start(row, False, False, 0)

            self.spin_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.spinner = Gtk.Spinner()
            self.lbl_auth_status = Gtk.Label(label="Waiting for authentication in browser...")
            self.spin_box.pack_start(self.spinner, False, False, 0)
            self.spin_box.pack_start(self.lbl_auth_status, False, False, 0)
            self.account_card_box.pack_start(self.spin_box, False, False, 4)
            self.spin_box.hide()

        self.account_card_box.show_all()
        if not is_auth and hasattr(self, "spin_box"):
            self.spin_box.hide()

    def _create_custom_creds_expander(self) -> Gtk.Widget:
        expander = Gtk.Expander(label="Advanced: Custom Google Cloud OAuth 2.0 Credentials")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)

        lbl_desc = Gtk.Label(
            label="You can use your own Google Cloud Console OAuth 2.0 Client ID and Secret (Desktop app),\n"
                  "or leave blank to use the pre-configured official Google Drive desktop credentials."
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

    def _on_sign_in_clicked(self, widget):
        self.btn_signin.set_sensitive(False)
        self.spin_box.show()
        self.spinner.start()
        self.lbl_auth_status.set_text("Opening browser for Google authentication...")

        def worker():
            try:
                res = oauth_manager.start_oauth_flow(
                    open_browser=True,
                    url_callback=lambda u: GLib.idle_add(self._on_auth_url_opened, u)
                )
                GLib.idle_add(self._on_auth_finished, res, None)
            except Exception as e:
                GLib.idle_add(self._on_auth_finished, None, str(e))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _on_auth_url_opened(self, url):
        self.lbl_auth_status.set_text("Waiting for authentication in browser...")

    def _on_auth_finished(self, res, error):
        self.spinner.stop()
        if error:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Authentication Failed"
            )
            dialog.format_secondary_text(error)
            dialog.run()
            dialog.destroy()
        else:
            profile = res.get("profile", {})
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Account Connected!"
            )
            dialog.format_secondary_text(f"Successfully connected as {profile.get('name', 'Google User')} ({profile.get('email', '')}).")
            dialog.run()
            dialog.destroy()

        self._populate_account_card()

    def _on_account_logout(self, widget):
        oauth_manager.revoke_tokens()
        self._populate_account_card()

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

    def _create_mcp_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_border_width(24)

        lbl_title = Gtk.Label()
        lbl_title.set_markup("<b><big>Model Context Protocol &amp; IPC Status</big></b>")
        lbl_title.set_xalign(0)
        box.pack_start(lbl_title, False, False, 0)

        running = is_daemon_running()
        sock = get_socket_path()

        status_text = (
            f"<b>Daemon State:</b> {'<span color=\"#2e7d32\">🟢 RUNNING</span>' if running else '<span color=\"#c62828\">🔴 STOPPED</span>'}\n"
            f"<b>IPC Socket:</b> <tt>{sock}</tt>\n"
            f"<b>Configuration File:</b> <tt>{CONFIG_FILE}</tt>\n\n"
            "<b>Registered MCP Tools:</b>\n"
            "  1. <tt>dfd_get_sync_status</tt> — Inspect sync status of file/folder\n"
            "  2. <tt>dfd_add_folder_to_sync</tt> — Track new folder for sync\n"
            "  3. <tt>dfd_get_drive_url</tt> — Get shareable Google Drive web URL\n"
            "  4. <tt>dfd_list_tracked_folders</tt> — List all synchronized paths\n"
            "  5. <tt>dfd_open_acl_dialog</tt> — Launch interactive sharing permissions UI"
        )

        lbl_info = Gtk.Label()
        lbl_info.set_markup(status_text)
        lbl_info.set_xalign(0)
        box.pack_start(lbl_info, False, False, 0)

        btn_test = Gtk.Button(label="Test IPC Connection")
        btn_test.connect("clicked", self._on_test_ipc)
        box.pack_start(btn_test, False, False, 0)

        return box

    def _on_test_ipc(self, widget):
        try:
            resp = send_ipc(McpIpcRequest(request_id=1, list_tracked_folders={}))
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="IPC Connection Succeeded!"
            )
            dialog.format_secondary_text(f"Connected to {get_socket_path()}. Daemon is responsive.")
            dialog.run()
            dialog.destroy()
        except Exception as e:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="IPC Connection Failed"
            )
            dialog.format_secondary_text(str(e))
            dialog.run()
            dialog.destroy()

    def _on_save(self, widget):
        mode = "stream" if self.radio_stream.get_active() else "mirror"
        self.config["storage"]["sync_mode"] = mode
        self.config["storage"]["mount_point"] = self.entry_mount.get_text().strip()
        self.config["storage"]["cache_dir"] = self.entry_cache.get_text().strip()
        self.config["storage"]["cache_size_limit_gb"] = int(self.spin_cache_limit.get_value())
        self.config["account"]["email"] = self.entry_email.get_text().strip()
        self.config["bandwidth"]["download_rate_kbps"] = int(self.spin_down.get_value())
        self.config["bandwidth"]["upload_rate_kbps"] = int(self.spin_up.get_value())
        self.config["system"]["autostart"] = self.chk_autostart.get_active()
        self.config["system"]["notifications"] = self.chk_notify.get_active()

        save_config(self.config)

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Preferences Saved"
        )
        dialog.format_secondary_text(f"Settings saved to {CONFIG_FILE}.")
        dialog.run()
        dialog.destroy()

def main():
    win = PreferencesWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == '__main__':
    main()

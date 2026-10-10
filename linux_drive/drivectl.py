#!/usr/bin/env python3
"""
Google Drive Linux - Command Line Control & Configuration Utility (`drivectl`)
Manage settings, tracked folders, sync status, and launch the configuration UI.
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from linux_drive.config import (
    load_config,
    save_config,
    get_config_value,
    set_config_value,
    CONFIG_FILE
)
from linux_drive.ipc_wire import (
    McpIpcRequest,
    McpIpcResponse,
    frame_message,
    read_framed_message
)
from linux_drive.oauth import oauth_manager
import time

def get_socket_path() -> str:
    return f"/tmp/drivefs_ipc.{os.getuid()}"

def is_daemon_running() -> bool:
    sock_path = get_socket_path()
    if not os.path.exists(sock_path):
        return False
    import socket
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        s.close()
        return True
    except Exception:
        return False

def send_ipc(req: McpIpcRequest) -> McpIpcResponse:
    sock_path = get_socket_path()
    if not os.path.exists(sock_path):
        raise RuntimeError(f"Daemon socket {sock_path} not found. Is the Google Drive daemon running?")
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    try:
        s.sendall(frame_message(req.serialize()))
        raw_resp = read_framed_message(s)
        if not raw_resp:
            raise RuntimeError("Daemon closed connection without response.")
        return McpIpcResponse.deserialize(raw_resp)
    finally:
        s.close()

def cmd_status(args):
    running = is_daemon_running()
    cfg = load_config()
    acc = cfg.get("account", {})
    auth_str = "🟢 CONNECTED" if acc.get("authenticated") else "🔴 NOT CONNECTED"

    print("=" * 50)
    print(" Google Drive for Linux - Status")
    print("=" * 50)
    print(f" Daemon State     : {'🟢 RUNNING' if running else '🔴 STOPPED'}")
    print(f" IPC Socket       : {get_socket_path()}")
    print(f" Config File      : {CONFIG_FILE}")
    print(f" Account Status   : {auth_str}")
    if acc.get("authenticated"):
        name_str = f"{acc.get('name')} " if acc.get("name") else ""
        print(f" Account Email    : {name_str}<{acc.get('email')}>")
        quota = acc.get("storage_quota", {})
        if quota.get("limit"):
            usage_gb = quota.get("usage", 0) / (1024**3)
            limit_gb = quota.get("limit", 0) / (1024**3)
            pct = (usage_gb / limit_gb) * 100 if limit_gb > 0 else 0
            print(f" Storage Quota    : {usage_gb:.2f} GB of {limit_gb:.1f} GB ({pct:.1f}% used)")
    else:
        print("                    (Run 'drivectl auth login' or open Preferences to connect)")
    print(f" Sync Mode        : {cfg.get('storage', {}).get('sync_mode', 'stream').upper()}")
    print(f" Mount Point      : {cfg.get('storage', {}).get('mount_point')}")
    print(f" Cache Directory  : {cfg.get('storage', {}).get('cache_dir')}")
    print(f" Cache Limit      : {cfg.get('storage', {}).get('cache_size_limit_gb')} GB")
    
    if running:
        try:
            resp = send_ipc(McpIpcRequest(request_id=1, list_tracked_folders={}))
            tracked = resp.list_tracked_folders.get("paths", []) if resp.list_tracked_folders else []
            print(f" Tracked Folders  : {len(tracked)}")
            for p in tracked:
                print(f"   • {p}")
        except Exception as e:
            print(f" Tracked Folders  : Error querying daemon: {e}")
    else:
        print(" (Start daemon with 'drivectl start' to query live sync metrics)")
    print("=" * 50)

def cmd_config(args):
    cfg = load_config()
    if not args.action or args.action == "show":
        print(json.dumps(cfg, indent=2))
    elif args.action == "get":
        if not args.key:
            print("Error: Specify a key (e.g. storage.sync_mode)")
            return
        val = get_config_value(args.key)
        print(f"{args.key} = {json.dumps(val) if val is not None else 'None'}")
    elif args.action == "set":
        if not args.key or args.value is None:
            print("Error: Specify key and value (e.g. drivectl config set storage.sync_mode mirror)")
            return
        # Attempt to parse json value (for booleans, ints, etc.)
        val = args.value
        try:
            val = json.loads(args.value)
        except Exception:
            pass
        set_config_value(args.key, val)
        print(f"Updated {args.key} to {val}")

def cmd_add(args):
    path = os.path.abspath(args.path)
    if not os.path.exists(path):
        print(f"Error: Path does not exist: {path}")
        sys.exit(1)
    
    # Send to daemon if running
    if is_daemon_running():
        resp = send_ipc(McpIpcRequest(
            request_id=1,
            add_folder_to_sync={"path": path, "account_id": "default"}
        ))
        if resp.add_folder_to_sync and resp.add_folder_to_sync.get("success"):
            print(f"✓ Added folder to sync: {path}")
        else:
            err = resp.add_folder_to_sync.get("error_message") if resp.add_folder_to_sync else "Unknown error"
            print(f"Daemon reported error: {err}")
    else:
        # Update config directly
        cfg = load_config()
        tracked = cfg.get("tracked_folders", [])
        if not any(f.get("path") == path for f in tracked):
            tracked.append({"path": path, "sync_mode": args.mode or "mirror", "status": "synced"})
            cfg["tracked_folders"] = tracked
            save_config(cfg)
            print(f"✓ Added folder to config: {path} (will sync when daemon starts)")
        else:
            print(f"Folder already tracked: {path}")

def cmd_list(args):
    if is_daemon_running():
        resp = send_ipc(McpIpcRequest(request_id=1, list_tracked_folders={}))
        paths = resp.list_tracked_folders.get("paths", []) if resp.list_tracked_folders else []
        print(f"Tracked folders ({len(paths)}):")
        for p in paths:
            status_resp = send_ipc(McpIpcRequest(request_id=2, get_sync_status={"path": p}))
            st = status_resp.get_sync_status.get("status", "unknown") if status_resp.get_sync_status else "unknown"
            print(f"  [{st.upper():<8}] {p}")
    else:
        cfg = load_config()
        folders = cfg.get("tracked_folders", [])
        print(f"Tracked folders in config ({len(folders)}):")
        for f in folders:
            print(f"  [{f.get('sync_mode', 'mirror').upper():<8}] {f.get('path')}")

def cmd_settings(args):
    # Launch GTK preferences dialog
    dialog_script = ROOT_DIR / "linux_drive" / "preferences_dialog.py"
    subprocess.Popen([sys.executable, str(dialog_script)])
    print("Launched Google Drive Preferences window.")

def cmd_start(args):
    if is_daemon_running():
        print("Google Drive daemon is already running.")
        return
    daemon_script = ROOT_DIR / "linux_drive" / "ipc_daemon.py"
    proc = subprocess.Popen([sys.executable, str(daemon_script)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Starting Google Drive daemon (PID {proc.pid})...")
    import time
    for _ in range(10):
        time.sleep(0.3)
        if is_daemon_running():
            print("✓ Google Drive daemon is up and listening.")
            return
    print("Daemon launched. Check logs if socket did not appear.")

def cmd_stop(args):
    sock_path = get_socket_path()
    # Find process listening or running ipc_daemon
    res = subprocess.run(["pkill", "-f", "linux_drive/ipc_daemon.py"], capture_output=True)
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    print("Google Drive daemon stopped.")

def cmd_auth(args):
    action = getattr(args, "auth_action", None)
    if not action or action == "status":
        cfg = load_config()
        acc = cfg.get("account", {})
        tokens = oauth_manager.load_tokens()
        print("=" * 50)
        print(" Google Drive Account & OAuth Status")
        print("=" * 50)
        cid, _ = oauth_manager.get_client_credentials()
        custom_used = cfg.get("oauth", {}).get("use_custom_credentials", False)
        print(f" Credentials Mode : {'Custom Google Cloud Project' if custom_used else 'Default Official Desktop Client'}")
        print(f" Client ID        : {cid[:25]}...{cid[-15:] if len(cid) > 40 else cid}")
        print(f" Status           : {'🟢 Authenticated' if acc.get('authenticated') else '🔴 Disconnected'}")
        if acc.get("authenticated"):
            print(f" Email            : {acc.get('email')}")
            print(f" Name             : {acc.get('name')}")
            print(f" Connected Since  : {acc.get('connected_at')}")
            quota = acc.get("storage_quota", {})
            if quota.get("limit"):
                usage_gb = quota.get("usage", 0) / (1024**3)
                limit_gb = quota.get("limit", 0) / (1024**3)
                pct = (usage_gb / limit_gb) * 100 if limit_gb > 0 else 0
                print(f" Cloud Storage    : {usage_gb:.2f} GB / {limit_gb:.1f} GB ({pct:.1f}%)")
            exp = tokens.get("expires_at", 0)
            if exp:
                rem = max(0, int(exp - time.time()))
                print(f" Access Token TTL : {rem}s remaining (auto-refreshes)")
        print("=" * 50)

    elif action == "login":
        if getattr(args, "client_id", None) and getattr(args, "client_secret", None):
            oauth_manager.set_custom_client_credentials(args.client_id, args.client_secret)
            print("Configured custom Google Cloud client credentials.")

        print("Initiating Google Drive OAuth 2.0 authorization...")
        def on_url(url):
            print("\nPlease open the following link in your browser to sign in:")
            print(f"\n  \033[1;34m{url}\033[0m\n")
            print("Waiting for authentication in browser...")

        try:
            res = oauth_manager.start_oauth_flow(
                port=getattr(args, "port", None),
                open_browser=not getattr(args, "no_browser", False),
                url_callback=on_url
            )
            profile = res.get("profile", {})
            quota = res.get("quota", {})
            print("\n\033[1;32m✓ Google Drive Account successfully connected!\033[0m")
            print(f"  User  : {profile.get('name')} <{profile.get('email')}>")
            if quota.get("limit"):
                usage_gb = quota.get("usage", 0) / (1024**3)
                limit_gb = quota.get("limit", 0) / (1024**3)
                print(f"  Quota : {usage_gb:.2f} GB of {limit_gb:.1f} GB used")
        except Exception as e:
            print(f"\n\033[1;31mError authenticating with Google:\033[0m {e}")
            sys.exit(1)

    elif action == "logout":
        oauth_manager.revoke_tokens()
        print("✓ Disconnected Google Drive account and revoked credentials.")

    elif action == "set-credentials":
        if not args.client_id or not args.client_secret:
            print("Error: Both --client-id and --client-secret are required.")
            sys.exit(1)
        oauth_manager.set_custom_client_credentials(args.client_id, args.client_secret)
        print("✓ Custom Google Cloud OAuth credentials updated.")

    elif action == "import-credentials":
        try:
            cid, _ = oauth_manager.import_credentials_file(args.file)
            print(f"✓ Imported credentials for Client ID: {cid}")
        except Exception as e:
            print(f"Error importing credentials file: {e}")
            sys.exit(1)

    elif action == "reset-credentials":
        oauth_manager.reset_to_default_credentials()
        print("✓ Reset to default Google Drive desktop credentials.")

def main():
    parser = argparse.ArgumentParser(
        prog="drivectl",
        description="Google Drive Linux - CLI Control & Configuration Tool"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # status
    p_status = subparsers.add_parser("status", help="Show daemon status, sync mode, and configuration")
    p_status.set_defaults(func=cmd_status)

    # auth
    p_auth = subparsers.add_parser("auth", help="Authenticate with Google and manage OAuth credentials")
    p_auth_sub = p_auth.add_subparsers(dest="auth_action", help="Auth action")

    p_auth_login = p_auth_sub.add_parser("login", help="Connect a Google Account via browser")
    p_auth_login.add_argument("--client-id", help="Optional custom Google Cloud Client ID")
    p_auth_login.add_argument("--client-secret", help="Optional custom Google Cloud Client Secret")
    p_auth_login.add_argument("--port", type=int, default=8085, help="Local loopback port (default 8085)")
    p_auth_login.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")

    p_auth_logout = p_auth_sub.add_parser("logout", help="Disconnect Google Account and revoke credentials")
    p_auth_status = p_auth_sub.add_parser("status", help="Show Google Account connection and quota status")

    p_auth_set = p_auth_sub.add_parser("set-credentials", help="Configure custom Google Cloud credentials")
    p_auth_set.add_argument("--client-id", required=True, help="Google Cloud Client ID")
    p_auth_set.add_argument("--client-secret", required=True, help="Google Cloud Client Secret")

    p_auth_import = p_auth_sub.add_parser("import-credentials", help="Import Google Cloud client_secret_*.json")
    p_auth_import.add_argument("file", help="Path to downloaded client_secrets JSON file")

    p_auth_reset = p_auth_sub.add_parser("reset-credentials", help="Reset to default official desktop client credentials")
    p_auth.set_defaults(func=cmd_auth)

    # settings / preferences GUI
    p_settings = subparsers.add_parser("settings", help="Open the graphical Preferences dialog")
    p_settings.set_defaults(func=cmd_settings)

    # config
    p_config = subparsers.add_parser("config", help="View or modify configuration")
    p_config.add_argument("action", nargs="?", choices=["show", "get", "set"], default="show", help="Config action")
    p_config.add_argument("key", nargs="?", help="Configuration key (e.g. storage.sync_mode)")
    p_config.add_argument("value", nargs="?", help="Value to set")
    p_config.set_defaults(func=cmd_config)

    # add
    p_add = subparsers.add_parser("add", help="Add a local folder to sync")
    p_add.add_argument("path", help="Local folder path")
    p_add.add_argument("--mode", choices=["mirror", "stream"], default="mirror", help="Sync mode")
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = subparsers.add_parser("list", help="List tracked sync folders")
    p_list.set_defaults(func=cmd_list)

    # start / stop / restart
    p_start = subparsers.add_parser("start", help="Start the background daemon")
    p_start.set_defaults(func=cmd_start)

    p_stop = subparsers.add_parser("stop", help="Stop the background daemon")
    p_stop.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)

if __name__ == "__main__":
    main()

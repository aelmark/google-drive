"""
Google Drive for Linux - Core IPC Daemon
Listens on /tmp/drivefs_ipc.<UID> using Unix Domain Socket.
Speaks the exact wire protocol expected by Google Drive Desktop tools.
Integrates with pure Python SyncEngine for background synchronization.
"""

import os
import sys
import time
import socket
import struct
import sqlite3
import threading
import logging
import webbrowser
import urllib.parse
from pathlib import Path
from typing import Optional

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_drive.ipc_wire import (
    McpIpcRequest,
    McpIpcResponse,
    frame_message,
    read_framed_message,
)
from linux_drive.sync_engine import SyncEngine
from linux_drive.config import (
    ensure_mount_point_exists,
    load_config,
    save_config,
    get_accounts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class DriveIpcDaemon:
    def __init__(self, db_path: Optional[str] = None, mount_point: Optional[str] = None):
        self.uid = os.getuid()
        self.socket_path = f"/tmp/drivefs_ipc.{self.uid}"
        self.data_dir = Path.home() / ".local" / "share" / "google-drive"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or str(self.data_dir / "daemon_state.db")
        self.mount_point = mount_point or str(Path.home() / "GoogleDrive")
        self.my_drive_path = str(Path(self.mount_point) / "My Drive")
        self.running = False
        self.server_sock = None
        self._init_db()
        self.sync_engine = SyncEngine(db_path=self.db_path)

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracked_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    account_id TEXT,
                    sync_mode TEXT NOT NULL DEFAULT 'mirror',
                    status TEXT NOT NULL DEFAULT 'synced',
                    last_synced REAL
                );
            """)

            cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_folders)").fetchall()]
            if "last_synced" not in cols:
                try:
                    conn.execute("ALTER TABLE tracked_folders ADD COLUMN last_synced REAL;")
                except Exception:
                    pass
            if "account_id" not in cols:
                try:
                    conn.execute("ALTER TABLE tracked_folders ADD COLUMN account_id TEXT;")
                except Exception:
                    pass
            if "sync_mode" not in cols:
                try:
                    conn.execute("ALTER TABLE tracked_folders ADD COLUMN sync_mode TEXT NOT NULL DEFAULT 'mirror';")
                except Exception:
                    pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracked_files (
                    path TEXT PRIMARY KEY,
                    folder_path TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    md5 TEXT,
                    mtime REAL,
                    size INTEGER,
                    status TEXT NOT NULL DEFAULT 'synced',
                    last_synced REAL
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_urls (
                    path TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    drive_url TEXT NOT NULL
                );
            """)
            conn.commit()

    def handle_request(self, req: McpIpcRequest) -> McpIpcResponse:
        resp = McpIpcResponse(request_id=req.request_id)

        if req.get_sync_status:
            path = os.path.abspath(req.get_sync_status.get('path', ''))
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA busy_timeout=5000;")
                row = conn.execute("SELECT status, sync_mode FROM tracked_folders WHERE path = ?", (path,)).fetchone()

            if row:
                status_str = row[0]
                progress = self.sync_engine.get_sync_progress() if hasattr(self, 'sync_engine') and self.sync_engine else {}
                if progress.get("state") == "syncing" and progress.get("current_folder") == path:
                    status_str = "syncing"

                resp.get_sync_status = {
                    'status': status_str,
                    'description': f"Folder is actively tracked in {row[1]} mode."
                }
            elif path.startswith(self.mount_point):
                resp.get_sync_status = {
                    'status': 'synced',
                    'description': "File/folder is inside Google Drive streaming mount."
                }
            else:
                # Check if inside any account's configured mount point
                inside_mount = False
                try:
                    cfg = load_config()
                    for aid, acc in cfg.get("accounts", {}).items():
                        mp = acc.get("mount_point")
                        if mp and path.startswith(os.path.abspath(mp)):
                            inside_mount = True
                            break
                except Exception:
                    pass

                if inside_mount:
                    resp.get_sync_status = {
                        'status': 'synced',
                        'description': "File/folder is inside Google Drive streaming mount."
                    }
                else:
                    resp.get_sync_status = {
                        'status': 'untracked',
                        'description': "Path is not synchronized with Google Drive."
                    }

        elif req.add_folder_to_sync:
            path = os.path.abspath(req.add_folder_to_sync.get('path', ''))
            account = req.add_folder_to_sync.get('account_id') or 'default'
            if not os.path.exists(path):
                resp.add_folder_to_sync = {
                    'success': False,
                    'error_message': f"Local path does not exist: {path}"
                }
            else:
                try:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute("PRAGMA busy_timeout=5000;")
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO tracked_folders (path, account_id, sync_mode, status, last_synced)
                            VALUES (?, ?, 'mirror', 'synced', ?)
                            """,
                            (path, account, time.time())
                        )
                        conn.commit()

                    # Also update config.json
                    try:
                        cfg = load_config()
                        target_acc_id = account if account in cfg.get("accounts", {}) else cfg.get("active_account_id", "primary")
                        if target_acc_id in cfg.get("accounts", {}):
                            tfs = cfg["accounts"][target_acc_id].setdefault("tracked_folders", [])
                            if not any((f.get("path") if isinstance(f, dict) else f) == path for f in tfs):
                                tfs.append({"path": path, "sync_mode": "mirror", "status": "synced"})
                        tfs_top = cfg.setdefault("tracked_folders", [])
                        if not any((f.get("path") if isinstance(f, dict) else f) == path for f in tfs_top):
                            tfs_top.append({"path": path, "sync_mode": "mirror", "status": "synced"})
                        save_config(cfg)
                    except Exception as cfg_err:
                        logging.warning(f"Failed to update config.json for added folder: {cfg_err}")

                    # Trigger immediate sync for newly added folder
                    if hasattr(self, 'sync_engine') and self.sync_engine:
                        self.sync_engine.sync_now(account_id=account)

                    logging.info(f"Added folder to sync: {path} for account {account}")
                    resp.add_folder_to_sync = {'success': True, 'error_message': ''}
                except Exception as e:
                    logging.error(f"Error adding folder to sync: {e}")
                    resp.add_folder_to_sync = {'success': False, 'error_message': str(e)}

        elif req.get_drive_url:
            path = os.path.abspath(req.get_drive_url.get('path', ''))
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA busy_timeout=5000;")
                row = conn.execute("SELECT drive_url FROM file_urls WHERE path = ?", (path,)).fetchone()
            if row:
                resp.get_drive_url = {'drive_url': row[0], 'error_message': ''}
            else:
                # Return standard Drive web folder / file search URL as fallback
                filename = os.path.basename(path)
                resp.get_drive_url = {
                    'drive_url': f"https://drive.google.com/drive/u/0/search?q={urllib.parse.quote(filename)}",
                    'error_message': ''
                }

        elif req.list_tracked_folders is not None:
            account = None
            if isinstance(req.list_tracked_folders, dict):
                account = req.list_tracked_folders.get('account_id')
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA busy_timeout=5000;")
                if account:
                    rows = conn.execute("SELECT path FROM tracked_folders WHERE account_id = ?", (account,)).fetchall()
                else:
                    rows = conn.execute("SELECT path FROM tracked_folders").fetchall()
            paths = [r[0] for r in rows]
            resp.list_tracked_folders = {'paths': paths, 'error_message': ''}

        elif req.fix_acl:
            email = req.fix_acl.get('email', '')
            item_id = req.fix_acl.get('item_id', '')
            logging.info(f"Launching sharing dialog for item {item_id} with {email}")
            share_url = f"https://drive.google.com/file/d/{item_id}/view?usp=sharing"
            try:
                # Open system default browser to the sharing page
                webbrowser.open(share_url)
                resp.fix_acl = {'success': True, 'error_message': ''}
            except Exception as e:
                resp.fix_acl = {'success': False, 'error_message': str(e)}

        elif req.get_streaming_mount_points is not None:
            account = req.get_streaming_mount_points.get('account_id') or 'primary'
            cfg = load_config()
            mps = []
            if account in cfg.get("accounts", {}):
                acc = cfg["accounts"][account]
                mp = acc.get("mount_point", self.mount_point)
                mps.append({
                    'account_id': account,
                    'mount_point': mp,
                    'my_drive_path': str(Path(mp) / "My Drive")
                })
            else:
                for aid, acc in cfg.get("accounts", {}).items():
                    mp = acc.get("mount_point", self.mount_point)
                    mps.append({
                        'account_id': aid,
                        'mount_point': mp,
                        'my_drive_path': str(Path(mp) / "My Drive")
                    })
                if not mps:
                    mps.append({
                        'account_id': 'primary',
                        'mount_point': self.mount_point,
                        'my_drive_path': self.my_drive_path
                    })
            resp.get_streaming_mount_points = {
                'mount_points': mps,
                'error_message': ''
            }

        elif req.remove_folder_from_sync:
            path = os.path.abspath(req.remove_folder_from_sync.get('path', ''))
            account = req.remove_folder_from_sync.get('account_id')
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("PRAGMA busy_timeout=5000;")
                    if account:
                        res = conn.execute("DELETE FROM tracked_folders WHERE path = ? AND account_id = ?", (path, account))
                    else:
                        res = conn.execute("DELETE FROM tracked_folders WHERE path = ?", (path,))
                    # Clean up tracked files under this folder
                    conn.execute("DELETE FROM tracked_files WHERE folder_path = ? OR path LIKE ?", (path, path + "/%"))
                    conn.commit()
                    deleted = res.rowcount > 0

                # Also update config.json
                try:
                    cfg = load_config()
                    for aid, acc in cfg.get("accounts", {}).items():
                        if not account or aid == account or acc.get("email") == account:
                            tfs = acc.get("tracked_folders", [])
                            acc["tracked_folders"] = [f for f in tfs if (f.get("path") if isinstance(f, dict) else f) != path]
                    tfs_top = cfg.get("tracked_folders", [])
                    cfg["tracked_folders"] = [f for f in tfs_top if (f.get("path") if isinstance(f, dict) else f) != path]
                    save_config(cfg)
                except Exception as cfg_err:
                    logging.warning(f"Failed to remove folder from config.json: {cfg_err}")

                if deleted:
                    logging.info(f"Removed folder from sync: {path}")
                    resp.remove_folder_from_sync = {'success': True, 'error_message': ''}
                else:
                    resp.remove_folder_from_sync = {'success': False, 'error_message': f"Folder not found in tracked folders: {path}"}
            except Exception as e:
                logging.error(f"Error removing folder from sync: {e}")
                resp.remove_folder_from_sync = {'success': False, 'error_message': str(e)}

        elif req.set_sync_mode:
            sync_mode = req.set_sync_mode.get('sync_mode', 'mirror').lower()
            account = req.set_sync_mode.get('account_id')
            if sync_mode not in ('stream', 'mirror'):
                resp.set_sync_mode = {'success': False, 'error_message': f"Invalid sync mode: {sync_mode}. Must be 'stream' or 'mirror'."}
            else:
                try:
                    cfg = load_config()
                    if account and account in cfg.get("accounts", {}):
                        cfg["accounts"][account]["sync_mode"] = sync_mode
                        ensure_mount_point_exists(cfg["accounts"][account].get("mount_point"))
                    elif account:
                        for aid, acc in cfg.get("accounts", {}).items():
                            if acc.get("email") == account:
                                acc["sync_mode"] = sync_mode
                                ensure_mount_point_exists(acc.get("mount_point"))
                                break
                    else:
                        cfg.setdefault("storage", {})["sync_mode"] = sync_mode
                        active_id = cfg.get("active_account_id")
                        if active_id and active_id in cfg.get("accounts", {}):
                            cfg["accounts"][active_id]["sync_mode"] = sync_mode
                        ensure_mount_point_exists(cfg.get("storage", {}).get("mount_point", self.mount_point))

                    save_config(cfg)

                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute("PRAGMA busy_timeout=5000;")
                        if account:
                            conn.execute("UPDATE tracked_folders SET sync_mode = ? WHERE account_id = ?", (sync_mode, account))
                        else:
                            conn.execute("UPDATE tracked_folders SET sync_mode = ?", (sync_mode,))
                        conn.commit()

                    logging.info(f"Updated sync mode to {sync_mode} for account {account or 'active'}")
                    resp.set_sync_mode = {'success': True, 'error_message': ''}
                except Exception as e:
                    logging.error(f"Error setting sync mode: {e}")
                    resp.set_sync_mode = {'success': False, 'error_message': str(e)}

        elif req.trigger_sync is not None:
            account = req.trigger_sync.get('account_id') if isinstance(req.trigger_sync, dict) else None
            path = req.trigger_sync.get('path') if isinstance(req.trigger_sync, dict) else None
            try:
                items_synced = 0
                try:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute("PRAGMA busy_timeout=5000;")
                        if path:
                            abs_p = os.path.abspath(path)
                            conn.execute("UPDATE tracked_folders SET status = 'synced' WHERE path = ?", (abs_p,))
                            items_synced = conn.execute("SELECT COUNT(*) FROM tracked_folders WHERE path = ?", (abs_p,)).fetchone()[0]
                        elif account:
                            conn.execute("UPDATE tracked_folders SET status = 'synced' WHERE account_id = ?", (account,))
                            items_synced = conn.execute("SELECT COUNT(*) FROM tracked_folders WHERE account_id = ?", (account,)).fetchone()[0]
                        else:
                            conn.execute("UPDATE tracked_folders SET status = 'synced'")
                            items_synced = conn.execute("SELECT COUNT(*) FROM tracked_folders").fetchone()[0]
                        conn.commit()
                except Exception as db_err:
                    logging.warning(f"Database update during trigger_sync: {db_err}")

                if hasattr(self, 'sync_engine') and self.sync_engine:
                    self.sync_engine.sync_now(account_id=account)
                    logging.info(f"Triggered immediate sync cycle for account: {account or 'all'}")

                resp.trigger_sync = {
                    'success': True,
                    'status': 'synced',
                    'error_message': '',
                    'items_synced': items_synced
                }
            except Exception as e:
                logging.error(f"Error triggering sync: {e}")
                resp.trigger_sync = {
                    'success': False,
                    'status': 'error',
                    'error_message': str(e),
                    'items_synced': 0
                }

        return resp

    def _handle_client(self, client_sock):
        try:
            # SO_PEERCRED check on Linux
            SO_PEERCRED = 17  # socket.SO_PEERCRED on Linux x86_64
            ucred = client_sock.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, struct.calcsize('3i'))
            pid, peer_uid, gid = struct.unpack('3i', ucred)
            if peer_uid != self.uid:
                logging.warning(f"Rejected connection: Peer UID {peer_uid} != Daemon UID {self.uid}")
                client_sock.close()
                return

            while self.running:
                raw_req = read_framed_message(client_sock)
                if not raw_req:
                    break
                req = McpIpcRequest.deserialize(raw_req)
                resp = self.handle_request(req)
                raw_resp = resp.serialize()
                client_sock.sendall(frame_message(raw_resp))
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            logging.error(f"Error handling client: {e}")
        finally:
            client_sock.close()

    def start(self):
        # Ensure configured mount points exist on startup
        try:
            ensure_mount_point_exists(self.mount_point)
            cfg = load_config()
            for aid, acc in cfg.get("accounts", {}).items():
                mp = acc.get("mount_point")
                if mp:
                    ensure_mount_point_exists(mp)
        except Exception as mp_err:
            logging.warning(f"Error verifying mount points: {mp_err}")

        # Start background sync engine
        if hasattr(self, 'sync_engine') and self.sync_engine and not self.sync_engine.is_alive():
            self.sync_engine.start()

        if os.path.exists(self.socket_path):
            # Check if active daemon is already listening on this socket
            try:
                test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                test_sock.settimeout(0.5)
                test_sock.connect(self.socket_path)
                test_sock.close()
                logging.info(f"Google Drive IPC Daemon is already running on {self.socket_path}. Exiting duplicate instance.")
                return
            except Exception:
                # Stale socket file, unlink it
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass

        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.server_sock.listen(10)
        self.running = True
        logging.info(f"Google Drive Linux IPC Daemon listening at {self.socket_path}")

        try:
            while self.running:
                client_sock, _ = self.server_sock.accept()
                t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
                t.start()
        except KeyboardInterrupt:
            logging.info("Shutting down IPC daemon...")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if hasattr(self, 'sync_engine') and self.sync_engine:
            self.sync_engine.stop()
        if self.server_sock:
            self.server_sock.close()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        logging.info("IPC Daemon stopped.")


if __name__ == '__main__':
    daemon = DriveIpcDaemon()
    daemon.start()

"""
Google Drive for Linux - Core IPC Daemon
Listens on /tmp/drivefs_ipc.<UID> using Unix Domain Socket.
Speaks the exact wire protocol expected by Google Drive Desktop tools.
"""

import os
import sys
import socket
import struct
import sqlite3
import threading
import logging
import webbrowser
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_drive.ipc_wire import (
    McpIpcRequest,
    McpIpcResponse,
    frame_message,
    read_framed_message
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class DriveIpcDaemon:
    def __init__(self, db_path: str = None, mount_point: str = None):
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

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracked_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    account_id TEXT,
                    sync_mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'synced'
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
                row = conn.execute("SELECT status, sync_mode FROM tracked_folders WHERE path = ?", (path,)).fetchone()
            
            if row:
                resp.get_sync_status = {
                    'status': row[0],
                    'description': f"Folder is actively tracked in {row[1]} mode."
                }
            elif path.startswith(self.mount_point):
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
            account = req.add_folder_to_sync.get('account_id', 'default')
            if not os.path.exists(path):
                resp.add_folder_to_sync = {
                    'success': False,
                    'error_message': f"Local path does not exist: {path}"
                }
            else:
                try:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO tracked_folders (path, account_id, sync_mode, status) VALUES (?, ?, ?, ?)",
                            (path, account, 'mirror', 'synced')
                        )
                        conn.commit()
                    logging.info(f"Added folder to sync: {path} for account {account}")
                    resp.add_folder_to_sync = {'success': True, 'error_message': ''}
                except Exception as e:
                    resp.add_folder_to_sync = {'success': False, 'error_message': str(e)}

        elif req.get_drive_url:
            path = os.path.abspath(req.get_drive_url.get('path', ''))
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT drive_url FROM file_urls WHERE path = ?", (path,)).fetchone()
            if row:
                resp.get_drive_url = {'drive_url': row[0], 'error_message': ''}
            else:
                # Return standard Drive web folder / file search URL as fallback
                filename = os.path.basename(path)
                resp.get_drive_url = {
                    'drive_url': f"https://drive.google.com/drive/u/0/search?q={filename}",
                    'error_message': ''
                }

        elif req.list_tracked_folders is not None:
            with sqlite3.connect(self.db_path) as conn:
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
            resp.get_streaming_mount_points = {
                'mount_points': [{
                    'account_id': account,
                    'mount_point': self.mount_point,
                    'my_drive_path': self.my_drive_path
                }],
                'error_message': ''
            }

        return resp

    def _handle_client(self, client_sock):
        try:
            # SO_PEERCRED check on Linux
            SO_PEERCRED = 17 # socket.SO_PEERCRED on Linux x86_64
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

#!/usr/bin/env python3
"""
Google Drive Linux - Pure Python Sync Engine
Performs recursive local directory scanning, MD5 and mtime comparison against
SQLite state cache, and multipart uploads to Google Drive via Drive API v3.
Runs in a background thread or standalone. Zero external pip dependencies.
"""

import os
import sys
import time
import json
import sqlite3
import hashlib
import mimetypes
import logging
import threading
import uuid
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from linux_drive.oauth import oauth_manager
from linux_drive.config import load_config, save_config

logger = logging.getLogger("DriveSyncEngine")


def calculate_md5(filepath: str) -> str:
    """Calculate the MD5 checksum of a local file in 64KB chunks."""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


class SyncEngine(threading.Thread):
    """
    Background synchronization engine for Google Drive on Linux.
    Runs as a daemon thread or standalone CLI process.
    """

    DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files"
    DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

    # Ignored directory names during recursive walk
    IGNORED_DIRS = {
        ".git",
        ".svn",
        ".hg",
        ".cache",
        "__pycache__",
        "node_modules",
        ".tox",
        ".venv",
        "venv",
        "env",
        ".idea",
        ".vscode",
    }

    # Ignored file extensions
    IGNORED_EXTS = {".tmp", ".swp", ".swo", "~", ".bak", ".part", ".crdownload"}

    def __init__(self, db_path: Optional[str] = None, sync_interval: int = 60):
        super().__init__(daemon=True, name="SyncEngineThread")
        data_dir = Path.home() / ".local" / "share" / "google-drive"
        data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = db_path or str(data_dir / "daemon_state.db")
        self.sync_interval = sync_interval
        self._stop_event = threading.Event()
        self._trigger_event = threading.Event()
        self._requested_account_id: Optional[str] = None
        self._lock = threading.Lock()

        # Sync Progress State
        self.state = "idle"  # 'idle', 'syncing', 'error', 'disconnected'
        self.current_folder: Optional[str] = None
        self.current_file: Optional[str] = None
        self.files_synced: int = 0
        self.files_total: int = 0
        self.bytes_synced: int = 0
        self.bytes_total: int = 0
        self.last_sync_time: Optional[float] = None
        self.last_error: Optional[str] = None

        self._init_db()

    def _init_db(self) -> None:
        """Initialise or migrate SQLite state tables."""
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

            # Ensure columns exist if table was created by older version
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

    def _api_request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        timeout: int = 60,
    ) -> Tuple[int, Optional[Dict[str, Any]], bytes]:
        """
        Execute an HTTP request using urllib.request.
        Returns (status_code, parsed_json_or_None, raw_body_bytes).
        """
        req_headers = headers.copy() if headers else {}
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.status
                body = response.read()
                try:
                    json_data = json.loads(body.decode("utf-8"))
                except Exception:
                    json_data = None
                return status_code, json_data, body
        except urllib.error.HTTPError as e:
            err_body = e.read()
            try:
                err_json = json.loads(err_body.decode("utf-8"))
            except Exception:
                err_json = None
            return e.code, err_json, err_body

    def _get_valid_token(self, account_id: Optional[str] = None, force_refresh: bool = False) -> Optional[str]:
        """Obtain a valid access token via oauth_manager."""
        try:
            return oauth_manager.get_valid_access_token(account_id=account_id, force_refresh=force_refresh)
        except Exception as e:
            logger.debug(f"Failed to acquire valid access token for account '{account_id}': {e}")
            return None

    def _find_or_create_remote_folder(
        self,
        folder_name: str,
        parent_id: Optional[str],
        access_token: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Search for or create a remote folder in Google Drive.
        Returns (folder_id, web_view_link).
        """
        escaped_name = folder_name.replace("'", "\\'")
        query_parts = [
            f"name = '{escaped_name}'",
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false",
        ]
        if parent_id and parent_id != "root":
            query_parts.append(f"'{parent_id}' in parents")
        elif parent_id == "root":
            query_parts.append("'root' in parents")

        q_str = " and ".join(query_parts)
        params = {
            "q": q_str,
            "fields": "files(id, name, webViewLink)",
            "spaces": "drive",
            "pageSize": "1",
        }
        url = f"{self.DRIVE_FILES_API}?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": f"Bearer {access_token}"}

        status, data, _ = self._api_request(url, headers=headers)
        if status == 200 and data and data.get("files"):
            file_meta = data["files"][0]
            return file_meta.get("id"), file_meta.get("webViewLink")

        # Not found, create remote folder
        create_meta: Dict[str, Any] = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            create_meta["parents"] = [parent_id]

        create_body = json.dumps(create_meta).encode("utf-8")
        create_url = f"{self.DRIVE_FILES_API}?fields=id,name,webViewLink"
        headers["Content-Type"] = "application/json; charset=UTF-8"

        status, create_data, err = self._api_request(create_url, method="POST", headers=headers, data=create_body)
        if status in (200, 201) and create_data:
            logger.info(f"Created remote folder '{folder_name}' (ID: {create_data.get('id')})")
            return create_data.get("id"), create_data.get("webViewLink")

        logger.error(f"Failed to create remote folder '{folder_name}': HTTP {status} {err}")
        return None, None

    def _upload_file_multipart(
        self,
        local_path: str,
        filename: str,
        parent_id: Optional[str],
        existing_file_id: Optional[str],
        access_token: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Upload local file using Drive API v3 multipart upload.
        Uses POST for new files or PATCH for existing files.
        """
        try:
            stat = os.stat(local_path)
            file_size = stat.st_size
        except OSError as e:
            logger.warning(f"Cannot stat local file '{local_path}': {e}")
            return None

        # Guard against extremely large files if necessary (> 250MB in one chunk)
        if file_size > 250 * 1024 * 1024:
            logger.warning(f"Skipping large file '{local_path}' ({file_size / (1024*1024):.1f}MB exceeds 250MB limit)")
            return None

        try:
            with open(local_path, "rb") as f:
                content_bytes = f.read()
        except OSError as e:
            logger.warning(f"Cannot read local file '{local_path}': {e}")
            return None

        mime_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        boundary = f"=====DriveSync_{uuid.uuid4().hex}====="

        metadata: Dict[str, Any] = {"name": filename}
        if not existing_file_id and parent_id:
            metadata["parents"] = [parent_id]

        meta_json = json.dumps(metadata).encode("utf-8")

        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b"Content-Type: application/json; charset=UTF-8\r\n\r\n")
        body.extend(meta_json)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        body.extend(content_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
            "Content-Length": str(len(body)),
        }

        if existing_file_id:
            url = f"{self.DRIVE_UPLOAD_API}/{existing_file_id}?uploadType=multipart&fields=id,name,md5Checksum,webViewLink"
            method = "PATCH"
        else:
            url = f"{self.DRIVE_UPLOAD_API}?uploadType=multipart&fields=id,name,md5Checksum,webViewLink"
            method = "POST"

        status, data, err_body = self._api_request(url, method=method, headers=headers, data=bytes(body))

        # If existing_file_id returned 404 (file deleted remotely), fallback to creating as new
        if existing_file_id and status == 404:
            logger.info(f"Remote file '{existing_file_id}' not found; recreating as new file.")
            return self._upload_file_multipart(
                local_path=local_path,
                filename=filename,
                parent_id=parent_id,
                existing_file_id=None,
                access_token=access_token,
            )

        if status in (200, 201) and data:
            return data

        logger.error(f"Failed to upload '{local_path}' (method: {method}): HTTP {status} {err_body}")
        return None

    def _sync_folder(
        self,
        folder_path: str,
        account_id: Optional[str],
        access_token: str,
    ) -> bool:
        """
        Recursively synchronise a single local folder into Google Drive.
        """
        folder_path = os.path.abspath(folder_path)
        if not os.path.isdir(folder_path):
            logger.warning(f"Tracked path is not a directory or does not exist: {folder_path}")
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE tracked_folders SET status = 'error', last_synced = ? WHERE path = ?",
                    (time.time(), folder_path),
                )
                conn.commit()
            return False

        self.current_folder = folder_path
        folder_basename = os.path.basename(folder_path)
        remote_root_name = f"Linux Sync - {folder_basename}"

        remote_root_id, remote_root_url = self._find_or_create_remote_folder(
            folder_name=remote_root_name,
            parent_id="root",
            access_token=access_token,
        )
        if not remote_root_id:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE tracked_folders SET status = 'error', last_synced = ? WHERE path = ?",
                    (time.time(), folder_path),
                )
                conn.commit()
            return False

        # Store remote root folder url
        if remote_root_url:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                    (folder_path, remote_root_id, remote_root_url),
                )
                conn.commit()

        # Cache of relative subdirectory paths to remote folder IDs
        # key: relative path string, e.g. "" or "sub1" or "sub1/sub2"
        folder_id_cache: Dict[str, str] = {"": remote_root_id}

        seen_local_files = set()
        folder_has_errors = False

        for root, dirs, files in os.walk(folder_path):
            # Prune hidden and ignored directories in-place
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in self.IGNORED_DIRS
            ]

            rel_dir = os.path.relpath(root, folder_path)
            if rel_dir == ".":
                rel_dir = ""

            # Ensure parent remote folder exists
            if rel_dir not in folder_id_cache:
                parent_rel, sub_name = os.path.split(rel_dir)
                parent_remote_id = folder_id_cache.get(parent_rel, remote_root_id)
                sub_remote_id, sub_url = self._find_or_create_remote_folder(
                    folder_name=sub_name,
                    parent_id=parent_remote_id,
                    access_token=access_token,
                )
                if not sub_remote_id:
                    logger.error(f"Cannot create remote directory for '{rel_dir}'")
                    folder_has_errors = True
                    continue
                folder_id_cache[rel_dir] = sub_remote_id
                if sub_url:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                            (root, sub_remote_id, sub_url),
                        )
                        conn.commit()

            current_remote_parent_id = folder_id_cache[rel_dir]

            # Process files in directory
            for filename in files:
                if filename.startswith("."):
                    continue
                if any(filename.endswith(ext) for ext in self.IGNORED_EXTS):
                    continue

                local_file_path = os.path.join(root, filename)
                seen_local_files.add(local_file_path)

                try:
                    stat = os.stat(local_file_path)
                    mtime = stat.st_mtime
                    size = stat.st_size
                except OSError:
                    continue

                self.current_file = local_file_path

                # Check state cache in SQLite
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cached = conn.execute(
                        "SELECT file_id, md5, mtime, size, status FROM tracked_files WHERE path = ?",
                        (local_file_path,),
                    ).fetchone()

                existing_file_id = None
                needs_upload = False

                if cached:
                    existing_file_id = cached["file_id"]
                    # If mtime and size match exactly and status was 'synced', file is unchanged
                    if (
                        cached["status"] == "synced"
                        and abs(cached["mtime"] - mtime) < 0.001
                        and cached["size"] == size
                    ):
                        continue

                    # If mtime/size differ, compute MD5 to verify content change
                    local_md5 = calculate_md5(local_file_path)
                    if cached["md5"] == local_md5 and cached["status"] == "synced":
                        # Content is identical, just mtime was touched; update metadata
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                "UPDATE tracked_files SET mtime = ?, size = ? WHERE path = ?",
                                (mtime, size, local_file_path),
                            )
                            conn.commit()
                        continue

                    needs_upload = True
                else:
                    local_md5 = calculate_md5(local_file_path)
                    needs_upload = True

                if needs_upload:
                    logger.info(f"Uploading '{local_file_path}' ({size} bytes)...")
                    upload_res = self._upload_file_multipart(
                        local_path=local_file_path,
                        filename=filename,
                        parent_id=current_remote_parent_id,
                        existing_file_id=existing_file_id,
                        access_token=access_token,
                    )

                    if upload_res and upload_res.get("id"):
                        remote_file_id = upload_res["id"]
                        drive_url = upload_res.get("webViewLink") or f"https://drive.google.com/file/d/{remote_file_id}/view"
                        returned_md5 = upload_res.get("md5Checksum") or local_md5

                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO tracked_files
                                (path, folder_path, file_id, md5, mtime, size, status, last_synced)
                                VALUES (?, ?, ?, ?, ?, ?, 'synced', ?)
                                """,
                                (local_file_path, folder_path, remote_file_id, returned_md5, mtime, size, time.time()),
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                                (local_file_path, remote_file_id, drive_url),
                            )
                            conn.commit()

                        with self._lock:
                            self.files_synced += 1
                            self.bytes_synced += size
                    else:
                        folder_has_errors = True
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO tracked_files
                                (path, folder_path, file_id, md5, mtime, size, status, last_synced)
                                VALUES (?, ?, ?, ?, ?, ?, 'error', ?)
                                """,
                                (local_file_path, folder_path, existing_file_id or "", local_md5, mtime, size, time.time()),
                            )
                            conn.commit()

        # Clean up tracked_files that have been deleted locally
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT path FROM tracked_files WHERE folder_path = ?",
                (folder_path,),
            ).fetchall()
            for r in rows:
                p = r["path"]
                if p not in seen_local_files and not os.path.exists(p):
                    conn.execute("DELETE FROM tracked_files WHERE path = ?", (p,))
                    conn.execute("DELETE FROM file_urls WHERE path = ?", (p,))
            conn.commit()

        # Update folder final status
        final_status = "error" if folder_has_errors else "synced"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tracked_folders SET status = ?, last_synced = ? WHERE path = ?",
                (final_status, time.time(), folder_path),
            )
            conn.commit()

        return not folder_has_errors

    def run_sync_cycle(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute one complete synchronization cycle across tracked folders.
        Gracefully handles offline/disconnected state and authentication failures.
        """
        with self._lock:
            self.state = "syncing"
            self.last_error = None
            self.files_synced = 0
            self.bytes_synced = 0

        logger.info(f"Starting sync cycle (account filter: {account_id or 'all'})...")

        # Check authentication & obtain access token
        access_token = self._get_valid_token(account_id=account_id)
        if not access_token:
            logger.warning("Google Drive authentication unavailable or token expired. Setting state to disconnected.")
            with self._lock:
                self.state = "disconnected"
                self.last_error = "Authentication unavailable or refresh token invalid."
            return self.get_sync_progress()

        # Fetch tracked folders from SQLite
        folders_to_sync: List[Tuple[str, Optional[str]]] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout=5000;")
            if account_id:
                rows = conn.execute(
                    "SELECT path, account_id FROM tracked_folders WHERE account_id = ?",
                    (account_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT path, account_id FROM tracked_folders").fetchall()
            folders_to_sync = [(r[0], r[1]) for r in rows]

        # If SQLite table is empty, check config.json and populate
        if not folders_to_sync:
            cfg = load_config()
            imported_folders = []
            for aid, acc in cfg.get("accounts", {}).items():
                for tf in acc.get("tracked_folders", []):
                    tf_path = tf.get("path") if isinstance(tf, dict) else tf
                    if tf_path:
                        imported_folders.append((tf_path, aid))
            if not imported_folders:
                for tf in cfg.get("tracked_folders", []):
                    tf_path = tf.get("path") if isinstance(tf, dict) else tf
                    if tf_path:
                        imported_folders.append((tf_path, cfg.get("active_account_id", "primary")))

            if imported_folders:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("PRAGMA busy_timeout=5000;")
                    for p, aid in imported_folders:
                        conn.execute(
                            "INSERT OR IGNORE INTO tracked_folders (path, account_id, sync_mode, status) VALUES (?, ?, 'mirror', 'synced')",
                            (p, aid),
                        )
                    conn.commit()
                folders_to_sync = imported_folders

        if not folders_to_sync:
            logger.info("No tracked folders configured for synchronization.")
            with self._lock:
                self.state = "idle"
                self.last_sync_time = time.time()
            return self.get_sync_progress()

        cycle_had_errors = False

        for f_path, f_account in folders_to_sync:
            if self._stop_event.is_set():
                break

            target_account = f_account or account_id
            target_token = access_token
            # If folder belongs to a different specific account, get its token
            if target_account and target_account != account_id:
                t = self._get_valid_token(account_id=target_account)
                if t:
                    target_token = t

            try:
                success = self._sync_folder(
                    folder_path=f_path,
                    account_id=target_account,
                    access_token=target_token,
                )
                if not success:
                    cycle_had_errors = True
            except (urllib.error.URLError, ConnectionError, OSError) as net_err:
                logger.warning(f"Network error syncing folder '{f_path}': {net_err}")
                cycle_had_errors = True
                with self._lock:
                    self.last_error = f"Network connection lost: {net_err}"
            except Exception as ex:
                logger.exception(f"Unexpected error syncing folder '{f_path}': {ex}")
                cycle_had_errors = True
                with self._lock:
                    self.last_error = str(ex)

        with self._lock:
            self.state = "error" if cycle_had_errors else "idle"
            self.last_sync_time = time.time()
            self.current_folder = None
            self.current_file = None

        logger.info(f"Sync cycle completed: state={self.state}, files_synced={self.files_synced}")
        return self.get_sync_progress()

    def run(self) -> None:
        """Background thread execution loop."""
        logger.info(f"SyncEngine thread started (interval: {self.sync_interval}s)")

        # Initial delay on startup before first cycle
        if self._stop_event.wait(timeout=3.0):
            return

        while not self._stop_event.is_set():
            with self._lock:
                req_account = self._requested_account_id
                self._requested_account_id = None
                self._trigger_event.clear()

            try:
                self.run_sync_cycle(account_id=req_account)
            except Exception as e:
                logger.exception(f"Unhandled error in sync cycle: {e}")
                with self._lock:
                    self.state = "error"
                    self.last_error = str(e)

            # Sleep until next sync interval or wake up immediately on trigger
            self._trigger_event.wait(timeout=self.sync_interval)

        logger.info("SyncEngine thread exiting.")

    def sync_now(self, account_id: Optional[str] = None) -> None:
        """Trigger an immediate sync pass."""
        with self._lock:
            if account_id:
                self._requested_account_id = account_id
            self._trigger_event.set()

    def get_sync_progress(self) -> Dict[str, Any]:
        """Return a thread-safe snapshot of sync metrics."""
        with self._lock:
            return {
                "state": self.state,
                "current_folder": self.current_folder,
                "current_file": self.current_file,
                "files_synced": self.files_synced,
                "files_total": self.files_total,
                "bytes_synced": self.bytes_synced,
                "bytes_total": self.bytes_total,
                "last_sync_time": self.last_sync_time,
                "error_message": self.last_error or "",
            }

    def stop(self) -> None:
        """Signal background thread to stop cleanly."""
        self._stop_event.set()
        self._trigger_event.set()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Google Drive Linux - Pure Python Sync Engine")
    parser.add_argument("--account", help="Optional account ID filter")
    parser.add_argument("--db", help="Path to daemon_state.db")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit")
    parser.add_argument("--interval", type=int, default=60, help="Sync interval in seconds (default 60)")
    args = parser.parse_args()

    engine = SyncEngine(db_path=args.db, sync_interval=args.interval)

    if args.once:
        print("Running single sync cycle...")
        result = engine.run_sync_cycle(account_id=args.account)
        print(f"Cycle finished: {json.dumps(result, indent=2)}")
        sys.exit(0 if result["state"] in ("idle", "disconnected") else 1)
    else:
        print(f"Starting SyncEngine daemon (interval {args.interval}s)... Press Ctrl+C to stop.")
        engine.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping SyncEngine...")
            engine.stop()
            engine.join(timeout=3)
            print("Stopped.")


if __name__ == "__main__":
    main()

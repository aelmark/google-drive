#!/usr/bin/env python3
"""
Google Drive Linux - Pure Python Two-Way Sync Engine
Performs bidirectional synchronization between Google Drive (My Drive & Shared Drives)
and local Linux mount folders (~/GoogleDrive...), as well as tracked local folders.
Handles binary file downloading/uploading, Google Docs/Sheets shortcuts (.desktop & .gdoc),
file metadata caching in SQLite, and Shared Drives discovery via Drive API v3.
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
from typing import Optional, Dict, Any, List, Tuple, Set

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from linux_drive.oauth import oauth_manager
from linux_drive.config import load_config, save_config, ensure_mount_point_exists, get_active_account_id

logger = logging.getLogger("DriveSyncEngine")


def calculate_md5(filepath: str) -> str:
    """Calculate the MD5 checksum of a local file in 64KB chunks."""
    hasher = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        logger.debug(f"Error calculating MD5 for {filepath}: {e}")
        return ""


def sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent illegal filesystem characters."""
    if not name:
        return "Untitled"
    clean = name.replace("/", "-").replace("\x00", "").strip()
    return clean if clean else "Untitled"


def get_google_app_info(mime_type: str) -> Tuple[str, str, str]:
    """Returns (extension, icon_name, generic_type_name) for Google Workspace cloud files."""
    mapping = {
        "application/vnd.google-apps.document": (".gdoc", "x-office-document", "Google Docs Document"),
        "application/vnd.google-apps.spreadsheet": (".gsheet", "x-office-spreadsheet", "Google Sheets Spreadsheet"),
        "application/vnd.google-apps.presentation": (".gslides", "x-office-presentation", "Google Slides Presentation"),
        "application/vnd.google-apps.form": (".gform", "application-vnd.google-apps.form", "Google Forms Form"),
        "application/vnd.google-apps.drawing": (".gdraw", "image-x-generic", "Google Drawings Drawing"),
        "application/vnd.google-apps.site": (".gsite", "text-html", "Google Sites Site"),
    }
    return mapping.get(mime_type, (".glink", "text-html", "Google Drive Item"))


def write_shortcut_files(
    dest_dir: Path,
    stem_name: str,
    url: str,
    mime_type: str,
    file_id: str,
    email: str = "",
) -> Tuple[str, str]:
    """
    Creates both a Linux .desktop link (with native office icons) and a .gdoc/.gsheet JSON link.
    Returns (desktop_path, json_link_path).
    """
    ext, icon_name, desc = get_google_app_info(mime_type)
    desktop_file = dest_dir / f"{stem_name}.desktop"
    json_file = dest_dir / f"{stem_name}{ext}"

    desktop_content = (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Link\n"
        f"Name={stem_name}\n"
        f"Comment={desc}\n"
        f"URL={url}\n"
        f"Icon={icon_name}\n"
    )
    try:
        with open(desktop_file, "w", encoding="utf-8") as f:
            f.write(desktop_content)
        try:
            os.chmod(desktop_file, 0o755)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Could not write desktop shortcut {desktop_file}: {e}")

    try:
        meta = {"url": url, "doc_id": file_id, "email": email}
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not write json companion link {json_file}: {e}")

    return str(desktop_file), str(json_file)


class SyncEngine(threading.Thread):
    """
    Background two-way synchronization engine for Google Drive on Linux.
    Synchronizes:
      1. 'My Drive' (bidirectional)
      2. 'Shared drives' (bidirectional)
      3. Tracked folders / 'Folders from your computer' (backup/mirror to Google Drive)
    Runs as a daemon thread or standalone CLI process.
    """

    DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files"
    DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
    DRIVE_DRIVES_API = "https://www.googleapis.com/drive/v3/drives"

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

            conn.execute("""
                CREATE TABLE IF NOT EXISTS remote_items (
                    file_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    drive_id TEXT,
                    name TEXT NOT NULL,
                    mime_type TEXT,
                    parent_id TEXT,
                    md5 TEXT,
                    size INTEGER,
                    modified_time TEXT,
                    web_view_link TEXT,
                    local_path TEXT,
                    trashed INTEGER DEFAULT 0
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_tokens (
                    account_id TEXT PRIMARY KEY,
                    page_token TEXT NOT NULL,
                    last_checked REAL
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
        """Execute an HTTP request using urllib.request."""
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
        except Exception as e:
            logger.debug(f"API request failed ({method} {url}): {e}")
            return 0, None, str(e).encode("utf-8")

    def _get_valid_token(self, account_id: Optional[str] = None, force_refresh: bool = False) -> Optional[str]:
        """Obtain a valid access token via oauth_manager."""
        try:
            return oauth_manager.get_valid_access_token(account_id=account_id, force_refresh=force_refresh)
        except Exception as e:
            logger.debug(f"Failed to acquire valid access token for account '{account_id}': {e}")
            return None

    def _list_shared_drives(self, access_token: str) -> List[Dict[str, Any]]:
        """Fetch all Google Workspace Shared Drives accessible by the account."""
        drives: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        headers = {"Authorization": f"Bearer {access_token}"}

        while True:
            params = {"pageSize": "100"}
            if page_token:
                params["pageToken"] = page_token
            url = f"{self.DRIVE_DRIVES_API}?{urllib.parse.urlencode(params)}"
            status, data, _ = self._api_request(url, headers=headers)
            if status != 200 or not data:
                break
            drives.extend(data.get("drives", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        logger.info(f"Discovered {len(drives)} Shared Drives.")
        return drives

    def _list_drive_files(
        self,
        access_token: str,
        drive_id: Optional[str] = None,
        page_limit: int = 15,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch non-trashed files and folders for My Drive or a specific Shared Drive.
        """
        items: List[Dict[str, Any]] = []
        headers = {"Authorization": f"Bearer {access_token}"}
        page_token: Optional[str] = None
        pages_fetched = 0

        while pages_fetched < page_limit:
            params: Dict[str, str] = {
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "q": "trashed = false",
                "pageSize": str(page_size),
                "fields": "nextPageToken,files(id,name,mimeType,parents,size,md5Checksum,modifiedTime,webViewLink,trashed)",
            }
            if drive_id:
                params["corpora"] = "drive"
                params["driveId"] = drive_id
            else:
                params["corpora"] = "user"
                # Exclude items that have driveId (belonging to shared drives) from user drive query
                params["q"] = "trashed = false and 'me' in owners"

            if page_token:
                params["pageToken"] = page_token

            url = f"{self.DRIVE_FILES_API}?{urllib.parse.urlencode(params)}"
            status, data, err_body = self._api_request(url, headers=headers)
            if status != 200 or not data:
                logger.warning(f"Error listing files (drive_id={drive_id}): HTTP {status} {err_body}")
                break

            batch = data.get("files", [])
            items.extend(batch)
            pages_fetched += 1
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return items

    def _download_remote_file(
        self,
        file_id: str,
        dest_path: Path,
        access_token: str,
        expected_size: Optional[int] = None,
        expected_md5: Optional[str] = None,
    ) -> bool:
        """
        Download a binary file from Google Drive using alt=media.
        Supports atomic replace and MD5 verification.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Skip download if local file matches expected size and MD5
        if dest_path.exists():
            try:
                local_size = dest_path.stat().st_size
                if expected_size is not None and local_size == expected_size:
                    if expected_md5:
                        if calculate_md5(str(dest_path)) == expected_md5:
                            return True
                    else:
                        return True
            except Exception:
                pass

        # Check free disk space: abort if less than 1GB free
        try:
            statvfs = os.statvfs(str(dest_path.parent))
            free_bytes = statvfs.f_frsize * statvfs.f_bavail
            if free_bytes < 1024 * 1024 * 1024:  # < 1 GB
                logger.warning(f"Disk space critically low. Skipping download of {dest_path}")
                return False
        except Exception:
            pass

        # Skip very large files (> 250MB) to conserve bandwidth unless needed
        if expected_size and expected_size > 250 * 1024 * 1024:
            logger.info(f"Skipping large binary file (>250MB): {dest_path.name} ({expected_size / (1024*1024):.1f}MB)")
            return True

        url = f"{self.DRIVE_FILES_API}/{file_id}?alt=media&supportsAllDrives=true"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        tmp_path = dest_path.parent / f".tmp_{dest_path.name}_{uuid.uuid4().hex[:8]}"

        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                with open(tmp_path, "wb") as f_out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f_out.write(chunk)
            # Atomically replace destination
            tmp_path.replace(dest_path)
            return True
        except Exception as e:
            logger.warning(f"Failed to download remote file '{dest_path.name}': {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return False

    def _sync_remote_to_local_drive(
        self,
        account_id: str,
        email: str,
        access_token: str,
        target_local_dir: Path,
        drive_id: Optional[str] = None,
        drive_name: Optional[str] = None,
    ) -> bool:
        """
        Synchronize files and folders from Google Drive to local directory.
        Works for both My Drive (drive_id=None) and Shared Drives.
        """
        target_local_dir.mkdir(parents=True, exist_ok=True)
        root_id = drive_id if drive_id else "root"
        display_name = drive_name if drive_name else "My Drive"
        logger.info(f"Syncing remote -> local for '{display_name}' ({target_local_dir})...")

        items = self._list_drive_files(access_token=access_token, drive_id=drive_id)
        if not items:
            logger.info(f"No items found in '{display_name}'.")
            return True

        # Map folder_id -> Path
        folder_map: Dict[str, Path] = {root_id: target_local_dir}

        folders = [it for it in items if it.get("mimeType") == "application/vnd.google-apps.folder"]
        files = [it for it in items if it.get("mimeType") != "application/vnd.google-apps.folder"]

        # Pass 1: Build directory tree
        unresolved = folders.copy()
        last_count = -1
        while unresolved and len(unresolved) != last_count:
            last_count = len(unresolved)
            remaining = []
            for fld in unresolved:
                fld_id = fld["id"]
                parents = fld.get("parents") or [root_id]
                parent_id = parents[0]
                if parent_id in folder_map:
                    safe_name = sanitize_filename(fld["name"])
                    folder_path = folder_map[parent_id] / safe_name
                    folder_path.mkdir(parents=True, exist_ok=True)
                    folder_map[fld_id] = folder_path
                else:
                    remaining.append(fld)
            unresolved = remaining

        # Any orphaned folders fall back to target_local_dir
        for fld in unresolved:
            safe_name = sanitize_filename(fld["name"])
            folder_path = target_local_dir / safe_name
            folder_path.mkdir(parents=True, exist_ok=True)
            folder_map[fld["id"]] = folder_path

        # Pass 2: Sync files
        seen_file_paths: Set[str] = set()

        for file_meta in files:
            if self._stop_event.is_set():
                break

            file_id = file_meta["id"]
            raw_name = file_meta.get("name", "Untitled")
            mime_type = file_meta.get("mimeType", "application/octet-stream")
            parents = file_meta.get("parents") or [root_id]
            parent_id = parents[0]
            dest_dir = folder_map.get(parent_id, target_local_dir)
            web_link = file_meta.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            md5_remote = file_meta.get("md5Checksum")
            size_remote = int(file_meta.get("size", 0)) if file_meta.get("size") else None

            stem_name = sanitize_filename(raw_name)

            if mime_type.startswith("application/vnd.google-apps."):
                # Cloud-native Google Doc/Sheet/Slide
                desktop_p, json_p = write_shortcut_files(
                    dest_dir=dest_dir,
                    stem_name=stem_name,
                    url=web_link,
                    mime_type=mime_type,
                    file_id=file_id,
                    email=email,
                )
                seen_file_paths.add(desktop_p)
                seen_file_paths.add(json_p)

                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("PRAGMA busy_timeout=5000;")
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO tracked_files
                        (path, folder_path, file_id, md5, mtime, size, status, last_synced)
                        VALUES (?, ?, ?, ?, ?, ?, 'synced', ?)
                        """,
                        (desktop_p, str(target_local_dir), file_id, "shortcut", time.time(), 0, time.time()),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                        (desktop_p, file_id, web_link),
                    )
                    conn.commit()

                with self._lock:
                    self.files_synced += 1

            else:
                # Binary file
                dest_file = dest_dir / stem_name
                seen_file_paths.add(str(dest_file))
                self.current_file = str(dest_file)

                download_ok = self._download_remote_file(
                    file_id=file_id,
                    dest_path=dest_file,
                    access_token=access_token,
                    expected_size=size_remote,
                    expected_md5=md5_remote,
                )

                if download_ok and dest_file.exists():
                    stat = dest_file.stat()
                    local_md5 = md5_remote or calculate_md5(str(dest_file))

                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute("PRAGMA busy_timeout=5000;")
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO tracked_files
                            (path, folder_path, file_id, md5, mtime, size, status, last_synced)
                            VALUES (?, ?, ?, ?, ?, ?, 'synced', ?)
                            """,
                            (str(dest_file), str(target_local_dir), file_id, local_md5, stat.st_mtime, stat.st_size, time.time()),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                            (str(dest_file), file_id, web_link),
                        )
                        conn.commit()

                    with self._lock:
                        self.files_synced += 1
                        if size_remote:
                            self.bytes_synced += size_remote

        return True

    def _sync_local_to_remote_drive(
        self,
        account_id: str,
        access_token: str,
        target_local_dir: Path,
        drive_id: Optional[str] = None,
    ) -> bool:
        """
        Upload locally created or modified files from local mount point to Google Drive.
        """
        if not target_local_dir.exists():
            return True

        root_id = drive_id if drive_id else "root"
        folder_cache: Dict[str, str] = {"": root_id}

        for root, dirs, files in os.walk(str(target_local_dir)):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in self.IGNORED_DIRS]

            rel_dir = os.path.relpath(root, str(target_local_dir))
            if rel_dir == ".":
                rel_dir = ""

            # Ensure parent remote folder exists
            if rel_dir not in folder_cache:
                parent_rel, sub_name = os.path.split(rel_dir)
                parent_remote_id = folder_cache.get(parent_rel, root_id)
                sub_id, _ = self._find_or_create_remote_folder(
                    folder_name=sub_name,
                    parent_id=parent_remote_id,
                    access_token=access_token,
                    drive_id=drive_id,
                )
                if sub_id:
                    folder_cache[rel_dir] = sub_id
                else:
                    continue

            current_parent_remote_id = folder_cache.get(rel_dir, root_id)

            for filename in files:
                if filename.startswith("."):
                    continue
                if any(filename.endswith(ext) for ext in self.IGNORED_EXTS):
                    continue
                # Skip desktop shortcuts and json companions
                if filename.endswith(".desktop") or filename.endswith((".gdoc", ".gsheet", ".gslides", ".gform")):
                    continue

                local_path = os.path.join(root, filename)
                try:
                    stat = os.stat(local_path)
                    mtime = stat.st_mtime
                    size = stat.st_size
                except OSError:
                    continue

                # Query database cache
                with sqlite3.connect(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cached = conn.execute(
                        "SELECT file_id, md5, mtime, size, status FROM tracked_files WHERE path = ?",
                        (local_path,),
                    ).fetchone()

                existing_file_id = None
                needs_upload = False

                if cached:
                    existing_file_id = cached["file_id"]
                    if cached["status"] == "synced" and abs(cached["mtime"] - mtime) < 0.001 and cached["size"] == size:
                        continue
                    local_md5 = calculate_md5(local_path)
                    if cached["md5"] == local_md5 and cached["status"] == "synced":
                        with sqlite3.connect(self.db_path) as conn:
                            conn.execute("UPDATE tracked_files SET mtime = ?, size = ? WHERE path = ?", (mtime, size, local_path))
                            conn.commit()
                        continue
                    needs_upload = True
                else:
                    local_md5 = calculate_md5(local_path)
                    needs_upload = True

                if needs_upload:
                    logger.info(f"Uploading local file '{filename}' to Google Drive...")
                    upload_res = self._upload_file_multipart(
                        local_path=local_path,
                        filename=filename,
                        parent_id=current_parent_remote_id,
                        existing_file_id=existing_file_id,
                        access_token=access_token,
                        drive_id=drive_id,
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
                                (local_path, str(target_local_dir), remote_file_id, returned_md5, mtime, size, time.time()),
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                                (local_path, remote_file_id, drive_url),
                            )
                            conn.commit()

                        with self._lock:
                            self.files_synced += 1
                            self.bytes_synced += size

        return True

    def _find_or_create_remote_folder(
        self,
        folder_name: str,
        parent_id: Optional[str],
        access_token: str,
        drive_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Search for or create a remote folder in Google Drive or Shared Drive."""
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
        params: Dict[str, str] = {
            "q": q_str,
            "fields": "files(id, name, webViewLink)",
            "pageSize": "1",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id

        url = f"{self.DRIVE_FILES_API}?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": f"Bearer {access_token}"}

        status, data, _ = self._api_request(url, headers=headers)
        if status == 200 and data and data.get("files"):
            file_meta = data["files"][0]
            return file_meta.get("id"), file_meta.get("webViewLink")

        create_meta: Dict[str, Any] = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            create_meta["parents"] = [parent_id]

        create_body = json.dumps(create_meta).encode("utf-8")
        create_url = f"{self.DRIVE_FILES_API}?supportsAllDrives=true&fields=id,name,webViewLink"
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
        drive_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Upload local file using Drive API v3 multipart upload."""
        try:
            stat = os.stat(local_path)
            file_size = stat.st_size
        except OSError as e:
            logger.warning(f"Cannot stat local file '{local_path}': {e}")
            return None

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
            url = f"{self.DRIVE_UPLOAD_API}/{existing_file_id}?uploadType=multipart&supportsAllDrives=true&fields=id,name,md5Checksum,webViewLink"
            method = "PATCH"
        else:
            url = f"{self.DRIVE_UPLOAD_API}?uploadType=multipart&supportsAllDrives=true&fields=id,name,md5Checksum,webViewLink"
            method = "POST"

        status, data, err_body = self._api_request(url, method=method, headers=headers, data=bytes(body))

        if existing_file_id and status == 404:
            logger.info(f"Remote file '{existing_file_id}' not found; recreating as new file.")
            return self._upload_file_multipart(
                local_path=local_path,
                filename=filename,
                parent_id=parent_id,
                existing_file_id=None,
                access_token=access_token,
                drive_id=drive_id,
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
        """Recursively synchronise a single local folder into Google Drive (Tracked Folders)."""
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

        if remote_root_url:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO file_urls (path, item_id, drive_url) VALUES (?, ?, ?)",
                    (folder_path, remote_root_id, remote_root_url),
                )
                conn.commit()

        folder_id_cache: Dict[str, str] = {"": remote_root_id}
        seen_local_files = set()
        folder_has_errors = False

        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in self.IGNORED_DIRS]

            rel_dir = os.path.relpath(root, folder_path)
            if rel_dir == ".":
                rel_dir = ""

            if rel_dir not in folder_id_cache:
                parent_rel, sub_name = os.path.split(rel_dir)
                parent_remote_id = folder_id_cache.get(parent_rel, remote_root_id)
                sub_remote_id, sub_url = self._find_or_create_remote_folder(
                    folder_name=sub_name,
                    parent_id=parent_remote_id,
                    access_token=access_token,
                )
                if not sub_remote_id:
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

            for filename in files:
                if filename.startswith(".") or any(filename.endswith(ext) for ext in self.IGNORED_EXTS):
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
                    if cached["status"] == "synced" and abs(cached["mtime"] - mtime) < 0.001 and cached["size"] == size:
                        continue

                    local_md5 = calculate_md5(local_file_path)
                    if cached["md5"] == local_md5 and cached["status"] == "synced":
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
        Execute one complete synchronization cycle across:
          1. Mount Point: My Drive (bidirectional)
          2. Mount Point: Shared Drives (bidirectional)
          3. Tracked folders / Folders from your computer
        """
        with self._lock:
            self.state = "syncing"
            self.last_error = None
            self.files_synced = 0
            self.bytes_synced = 0

        cfg = load_config()
        active_id = account_id or get_active_account_id(cfg)
        logger.info(f"Starting sync cycle (active account: {active_id})...")

        # Discover accounts to sync
        accounts_to_process: List[Tuple[str, Dict[str, Any]]] = []
        if account_id and account_id in cfg.get("accounts", {}):
            accounts_to_process.append((account_id, cfg["accounts"][account_id]))
        elif cfg.get("accounts"):
            for aid, acc in cfg["accounts"].items():
                if acc.get("authenticated"):
                    accounts_to_process.append((aid, acc))
        else:
            # Fallback for single-account config
            acc = cfg.get("account", {})
            if acc.get("authenticated"):
                accounts_to_process.append(("primary", acc))

        if not accounts_to_process:
            logger.info("No authenticated Google accounts configured for synchronization.")
            with self._lock:
                self.state = "disconnected"
                self.last_error = "No authenticated accounts configured."
            return self.get_sync_progress()

        cycle_had_errors = False

        for aid, acc_info in accounts_to_process:
            if self._stop_event.is_set():
                break

            email = acc_info.get("email", aid)
            logger.info(f"Processing sync for account: {email} ({aid})")
            access_token = self._get_valid_token(account_id=aid)
            if not access_token:
                logger.warning(f"Could not acquire access token for {email}. Skipping.")
                cycle_had_errors = True
                continue

            # -------------------------------------------------------------
            # Part 1: Mount Point Sync (My Drive & Shared Drives)
            # -------------------------------------------------------------
            mount_point_str = acc_info.get("mount_point")
            if not mount_point_str and aid == active_id:
                mount_point_str = cfg.get("storage", {}).get("mount_point")
            if not mount_point_str:
                mount_point_str = str(Path.home() / f"GoogleDrive - {email}")

            ensure_mount_point_exists(mount_point_str)
            mount_path = Path(mount_point_str).expanduser()
            my_drive_dir = mount_path / "My Drive"
            shared_drives_dir = mount_path / "Shared drives"

            # Check selective sync settings
            selective_cfg = acc_info.get("selective_sync", {})
            sync_my_drive = selective_cfg.get("sync_my_drive", True)
            selected_shared_drives = selective_cfg.get("shared_drives", None)

            # 1A. Sync My Drive (Remote -> Local)
            if sync_my_drive:
                try:
                    self.current_folder = str(my_drive_dir)
                    self._sync_remote_to_local_drive(
                        account_id=aid,
                        email=email,
                        access_token=access_token,
                        target_local_dir=my_drive_dir,
                        drive_id=None,
                        drive_name="My Drive",
                    )
                except Exception as e:
                    logger.exception(f"Error syncing My Drive remote->local for {email}: {e}")
                    cycle_had_errors = True

                # 1B. Sync My Drive (Local -> Remote)
                try:
                    self._sync_local_to_remote_drive(
                        account_id=aid,
                        access_token=access_token,
                        target_local_dir=my_drive_dir,
                        drive_id=None,
                    )
                except Exception as e:
                    logger.exception(f"Error syncing My Drive local->remote for {email}: {e}")
                    cycle_had_errors = True
            else:
                logger.info(f"Selective sync: skipping My Drive for {email}")

            # 1C. Sync Shared Drives
            if selected_shared_drives is None or len(selected_shared_drives) > 0:
                try:
                    shared_drives = self._list_shared_drives(access_token)
                    if selected_shared_drives is not None:
                        shared_drives = [
                            sd for sd in shared_drives
                            if sd.get("id") in selected_shared_drives or sd.get("name") in selected_shared_drives
                        ]
                        logger.info(f"Selective sync: syncing {len(shared_drives)} selected Shared Drives for {email}")

                    for sd in shared_drives:
                        if self._stop_event.is_set():
                            break
                        sd_id = sd.get("id")
                        sd_name = sd.get("name", "Shared Drive")
                        safe_sd_name = sanitize_filename(sd_name)
                        local_sd_dir = shared_drives_dir / safe_sd_name
                        local_sd_dir.mkdir(parents=True, exist_ok=True)

                        self.current_folder = str(local_sd_dir)
                        # Remote -> Local
                        self._sync_remote_to_local_drive(
                            account_id=aid,
                            email=email,
                            access_token=access_token,
                            target_local_dir=local_sd_dir,
                            drive_id=sd_id,
                            drive_name=sd_name,
                        )
                        # Local -> Remote
                        self._sync_local_to_remote_drive(
                            account_id=aid,
                            access_token=access_token,
                            target_local_dir=local_sd_dir,
                            drive_id=sd_id,
                        )
                except Exception as e:
                    logger.exception(f"Error syncing Shared Drives for {email}: {e}")
                    cycle_had_errors = True
            else:
                logger.info(f"Selective sync: no Shared Drives selected for {email} (skipping)")

            # -------------------------------------------------------------
            # Part 2: Tracked Folders ('Folders from your computer')
            # -------------------------------------------------------------
            tracked_paths: List[str] = []
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA busy_timeout=5000;")
                rows = conn.execute(
                    "SELECT path FROM tracked_folders WHERE account_id = ? OR account_id = 'default'",
                    (aid,),
                ).fetchall()
                tracked_paths = [r[0] for r in rows]

            for tf_path in tracked_paths:
                if self._stop_event.is_set():
                    break
                try:
                    ok = self._sync_folder(
                        folder_path=tf_path,
                        account_id=aid,
                        access_token=access_token,
                    )
                    if not ok:
                        cycle_had_errors = True
                except Exception as ex:
                    logger.warning(f"Error syncing tracked folder {tf_path}: {ex}")
                    cycle_had_errors = True

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

        if self._stop_event.wait(timeout=2.0):
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

    parser = argparse.ArgumentParser(description="Google Drive Linux - Pure Python Two-Way Sync Engine")
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

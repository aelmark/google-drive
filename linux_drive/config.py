"""
Google Drive Linux - Configuration Manager
Handles reading, writing, and validating persistent configuration in ~/.config/google-drive/config.json
Supports seamless multi-account management (Google Workspace + personal Gmail) with independent
sync options (Stream vs Mirror), mount locations, and tracked folder sets per account.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

CONFIG_DIR = Path.home() / ".config" / "google-drive"
CONFIG_FILE = CONFIG_DIR / "config.json"
ACCOUNTS_DIR = CONFIG_DIR / "accounts"

def ensure_mount_point_exists(mount_point: str) -> bool:
    """Ensures that the mount point and its 'My Drive' subdirectory exist on disk."""
    if not mount_point:
        return False
    try:
        mp = Path(mount_point).expanduser().resolve()
        mp.mkdir(parents=True, exist_ok=True)
        (mp / "My Drive").mkdir(parents=True, exist_ok=True)
        return True
    except Exception as e:
        logging.error(f"Failed to create mount point directory {mount_point}: {e}")
        return False

def make_default_account_dict(email: str = "", name: str = "", is_primary: bool = True) -> Dict[str, Any]:
    """Creates a fresh account dictionary with isolated sync and storage paths."""
    clean_email = email.strip()
    if is_primary or not clean_email:
        mount_point = str(Path.home() / "GoogleDrive")
        cache_dir = str(Path.home() / ".cache" / "google-drive")
    else:
        # Sanitize email for path naming
        safe_tag = clean_email.replace("@", "_").replace(".", "_")
        mount_point = str(Path.home() / f"GoogleDrive - {clean_email}")
        cache_dir = str(Path.home() / ".cache" / "google-drive" / safe_tag)

    return {
        "email": clean_email,
        "name": name.strip(),
        "avatar_url": "",
        "sub": "",
        "authenticated": False,
        "connected_at": None,
        "client_id": "947318989803-6bn6qk8qdgf4n4g3pfee6491hc0brc4i.apps.googleusercontent.com",
        "sync_mode": "stream",  # 'stream' or 'mirror'
        "mount_point": mount_point,
        "cache_dir": cache_dir,
        "cache_size_limit_gb": 10,
        "storage_quota": {
            "limit": 0,
            "usage": 0,
            "usage_in_drive": 0
        },
        "tracked_folders": []
    }

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 2,
    "active_account_id": "",
    "accounts": {},
    "oauth": {
        "use_custom_credentials": False,
        "custom_client_id": "",
        "custom_client_secret": ""
    },
    "account": {
        "email": "",
        "name": "",
        "avatar_url": "",
        "sub": "",
        "authenticated": False,
        "connected_at": None,
        "client_id": "947318989803-6bn6qk8qdgf4n4g3pfee6491hc0brc4i.apps.googleusercontent.com",
        "storage_quota": {
            "limit": 0,
            "usage": 0,
            "usage_in_drive": 0
        }
    },
    "storage": {
        "sync_mode": "stream",
        "mount_point": str(Path.home() / "GoogleDrive"),
        "cache_dir": str(Path.home() / ".cache" / "google-drive"),
        "cache_size_limit_gb": 10
    },
    "bandwidth": {
        "download_rate_kbps": 0,  # 0 = unlimited
        "upload_rate_kbps": 0     # 0 = unlimited
    },
    "system": {
        "autostart": True,
        "notifications": True
    },
    "tracked_folders": []
}

def _migrate_v1_to_v2(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Seamlessly upgrades v1 single-account config to v2 multi-account config."""
    if "accounts" not in cfg:
        cfg["accounts"] = {}

    # If accounts is empty, migrate legacy account/storage fields
    if not cfg["accounts"]:
        legacy_acc = cfg.get("account", {})
        legacy_email = legacy_acc.get("email", "").strip()
        legacy_storage = cfg.get("storage", {})
        legacy_tracked = cfg.get("tracked_folders", [])

        account_key = legacy_email or "primary"
        migrated_acc = make_default_account_dict(
            email=legacy_email,
            name=legacy_acc.get("name", ""),
            is_primary=True
        )
        migrated_acc["avatar_url"] = legacy_acc.get("avatar_url", "")
        migrated_acc["sub"] = legacy_acc.get("sub", "")
        migrated_acc["authenticated"] = legacy_acc.get("authenticated", False)
        migrated_acc["connected_at"] = legacy_acc.get("connected_at")
        migrated_acc["storage_quota"] = legacy_acc.get("storage_quota", migrated_acc["storage_quota"])
        migrated_acc["sync_mode"] = legacy_storage.get("sync_mode", "stream")
        migrated_acc["mount_point"] = legacy_storage.get("mount_point", migrated_acc["mount_point"])
        migrated_acc["cache_dir"] = legacy_storage.get("cache_dir", migrated_acc["cache_dir"])
        migrated_acc["cache_size_limit_gb"] = legacy_storage.get("cache_size_limit_gb", 10)
        migrated_acc["tracked_folders"] = legacy_tracked

        cfg["accounts"][account_key] = migrated_acc
        cfg["active_account_id"] = account_key

    # Ensure active_account_id is valid
    if not cfg.get("active_account_id") or cfg["active_account_id"] not in cfg["accounts"]:
        if cfg["accounts"]:
            cfg["active_account_id"] = next(iter(cfg["accounts"].keys()))
        else:
            primary = make_default_account_dict(is_primary=True)
            cfg["accounts"]["primary"] = primary
            cfg["active_account_id"] = "primary"

    cfg["version"] = 2
    _sync_legacy_top_level(cfg)
    return cfg

def _sync_legacy_top_level(cfg: Dict[str, Any]) -> None:
    """Keeps legacy top-level keys ('account', 'storage', 'tracked_folders') synced with active account."""
    active_id = cfg.get("active_account_id")
    accounts = cfg.get("accounts", {})
    if active_id and active_id in accounts:
        act = accounts[active_id]
        cfg["account"] = {
            "email": act.get("email", ""),
            "name": act.get("name", ""),
            "avatar_url": act.get("avatar_url", ""),
            "sub": act.get("sub", ""),
            "authenticated": act.get("authenticated", False),
            "connected_at": act.get("connected_at"),
            "client_id": act.get("client_id", DEFAULT_CONFIG["account"]["client_id"]),
            "storage_quota": act.get("storage_quota", {})
        }
        cfg["storage"] = {
            "sync_mode": act.get("sync_mode", "stream"),
            "mount_point": act.get("mount_point", str(Path.home() / "GoogleDrive")),
            "cache_dir": act.get("cache_dir", str(Path.home() / ".cache" / "google-drive")),
            "cache_size_limit_gb": act.get("cache_size_limit_gb", 10)
        }
        cfg["tracked_folders"] = act.get("tracked_folders", [])

def load_config() -> Dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        cfg = DEFAULT_CONFIG.copy()
        primary = make_default_account_dict(is_primary=True)
        cfg["accounts"]["primary"] = primary
        cfg["active_account_id"] = "primary"
        _sync_legacy_top_level(cfg)
        save_config(cfg)
        # Ensure default mount point exists
        ensure_mount_point_exists(primary["mount_point"])
        return cfg

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg = _migrate_v1_to_v2(cfg)
        return cfg
    except Exception as e:
        logging.error(f"Error loading config.json: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _sync_legacy_top_level(cfg)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def get_accounts(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Returns dictionary of all configured accounts keyed by account_id / email."""
    if cfg is None:
        cfg = load_config()
    return cfg.get("accounts", {})

def get_active_account_id(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Returns the ID/email of the active account."""
    if cfg is None:
        cfg = load_config()
    return cfg.get("active_account_id", "primary")

def get_active_account(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Returns the active account dictionary."""
    if cfg is None:
        cfg = load_config()
    aid = get_active_account_id(cfg)
    accounts = cfg.get("accounts", {})
    if aid in accounts:
        return accounts[aid]
    if accounts:
        first_key = next(iter(accounts.keys()))
        return accounts[first_key]
    default_acc = make_default_account_dict(is_primary=True)
    return default_acc

def get_account_by_id(account_id: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Returns a specific account by email / ID."""
    if cfg is None:
        cfg = load_config()
    accounts = cfg.get("accounts", {})
    if account_id in accounts:
        return accounts[account_id]
    for k, v in accounts.items():
        if v.get("email") == account_id or v.get("sub") == account_id:
            return v
    return None

def set_active_account_id(account_id: str) -> bool:
    """Switches the active account and saves config."""
    cfg = load_config()
    accounts = cfg.get("accounts", {})
    target_key = None
    if account_id in accounts:
        target_key = account_id
    else:
        for k, v in accounts.items():
            if v.get("email") == account_id or v.get("sub") == account_id:
                target_key = k
                break
    if target_key:
        cfg["active_account_id"] = target_key
        save_config(cfg)
        return True
    return False

def add_or_update_account(account_data: Dict[str, Any], make_active: bool = True) -> str:
    """
    Adds or updates an account in config.json.
    Automatically creates the mount point directory and cache directory on disk.
    """
    cfg = load_config()
    email = account_data.get("email", "").strip()
    account_id = email or account_data.get("sub", "") or "primary"

    # Check if this is a newly added account and needs a unique mount point
    existing = cfg.get("accounts", {}).get(account_id)
    if not existing:
        is_first = len(cfg.get("accounts", {})) == 0 or (
            len(cfg.get("accounts", {})) == 1 and "primary" in cfg.get("accounts", {}) and not cfg["accounts"]["primary"].get("authenticated")
        )
        base = make_default_account_dict(email=email, is_primary=is_first)
        # Deep merge new data over base
        base.update(account_data)
        account_data = base
    else:
        existing.update(account_data)
        account_data = existing

    # If replacing dummy 'primary' with real email
    if "primary" in cfg.get("accounts", {}) and account_id != "primary":
        if not cfg["accounts"]["primary"].get("authenticated") or cfg["accounts"]["primary"].get("email") == email:
            del cfg["accounts"]["primary"]

    cfg["accounts"][account_id] = account_data
    if make_active or not cfg.get("active_account_id"):
        cfg["active_account_id"] = account_id

    # Automatically create the mount point directory on disk
    mp = account_data.get("mount_point")
    if mp:
        ensure_mount_point_exists(mp)

    # Automatically create the cache directory on disk
    cdir = account_data.get("cache_dir")
    if cdir:
        try:
            Path(cdir).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    save_config(cfg)
    return account_id

def remove_account(account_id: str) -> bool:
    """Removes an account from configuration."""
    cfg = load_config()
    accounts = cfg.get("accounts", {})
    target_key = None
    if account_id in accounts:
        target_key = account_id
    else:
        for k, v in accounts.items():
            if v.get("email") == account_id or v.get("sub") == account_id:
                target_key = k
                break
    if target_key and target_key in accounts:
        del accounts[target_key]
        if cfg.get("active_account_id") == target_key:
            cfg["active_account_id"] = next(iter(accounts.keys())) if accounts else ""
        if not accounts:
            primary = make_default_account_dict(is_primary=True)
            accounts["primary"] = primary
            cfg["active_account_id"] = "primary"
        cfg["accounts"] = accounts
        save_config(cfg)
        return True
    return False

def get_config_value(path_str: str, default: Any = None) -> Any:
    cfg = load_config()
    keys = path_str.split(".")
    curr = cfg
    for k in keys:
        if isinstance(curr, dict) and k in curr:
            curr = curr[k]
        else:
            return default
    return curr

def set_config_value(path_str: str, value: Any) -> None:
    cfg = load_config()
    keys = path_str.split(".")
    curr = cfg
    for k in keys[:-1]:
        if k not in curr or not isinstance(curr[k], dict):
            curr[k] = {}
        curr = curr[k]
    curr[keys[-1]] = value
    save_config(cfg)

def get_tracked_folders(account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns the list of tracked folders for an account (or active account if None)."""
    cfg = load_config()
    target_id = account_id or cfg.get("active_account_id", "primary")
    acc = get_account_by_id(target_id, cfg)
    if acc and "tracked_folders" in acc:
        return list(acc["tracked_folders"])
    return list(cfg.get("tracked_folders", []))

def add_tracked_folder(path: str, sync_mode: str = "mirror", account_id: Optional[str] = None) -> bool:
    """Adds a tracked folder to the specified account (or active account) in config."""
    cfg = load_config()
    abs_path = os.path.abspath(path)
    target_id = account_id or cfg.get("active_account_id", "primary")
    accounts = cfg.get("accounts", {})
    target_key = None
    if target_id in accounts:
        target_key = target_id
    else:
        for k, v in accounts.items():
            if v.get("email") == target_id or v.get("sub") == target_id:
                target_key = k
                break
    if not target_key and accounts:
        target_key = cfg.get("active_account_id") or next(iter(accounts.keys()))

    if target_key and target_key in accounts:
        acc = accounts[target_key]
        if "tracked_folders" not in acc:
            acc["tracked_folders"] = []
        if not any(os.path.abspath(f.get("path", "")) == abs_path for f in acc["tracked_folders"]):
            acc["tracked_folders"].append({"path": abs_path, "sync_mode": sync_mode, "status": "synced"})
            save_config(cfg)
            return True
        return False
    else:
        tracked = cfg.get("tracked_folders", [])
        if not any(os.path.abspath(f.get("path", "")) == abs_path for f in tracked):
            tracked.append({"path": abs_path, "sync_mode": sync_mode, "status": "synced"})
            cfg["tracked_folders"] = tracked
            save_config(cfg)
            return True
        return False

def remove_tracked_folder(path: str, account_id: Optional[str] = None) -> bool:
    """Removes a tracked folder from the specified account (or all/active accounts)."""
    cfg = load_config()
    abs_path = os.path.abspath(path)
    removed = False
    accounts = cfg.get("accounts", {})
    if account_id:
        target_key = None
        if account_id in accounts:
            target_key = account_id
        else:
            for k, v in accounts.items():
                if v.get("email") == account_id or v.get("sub") == account_id:
                    target_key = k
                    break
        if target_key and target_key in accounts:
            acc = accounts[target_key]
            orig_len = len(acc.get("tracked_folders", []))
            acc["tracked_folders"] = [f for f in acc.get("tracked_folders", []) if os.path.abspath(f.get("path", "")) != abs_path]
            if len(acc["tracked_folders"]) < orig_len:
                removed = True
    else:
        for acc in accounts.values():
            orig_len = len(acc.get("tracked_folders", []))
            acc["tracked_folders"] = [f for f in acc.get("tracked_folders", []) if os.path.abspath(f.get("path", "")) != abs_path]
            if len(acc["tracked_folders"]) < orig_len:
                removed = True

    orig_top = len(cfg.get("tracked_folders", []))
    cfg["tracked_folders"] = [f for f in cfg.get("tracked_folders", []) if os.path.abspath(f.get("path", "")) != abs_path]
    if len(cfg["tracked_folders"]) < orig_top:
        removed = True

    if removed:
        save_config(cfg)
    return removed


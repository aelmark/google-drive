"""
Google Drive Linux - Configuration Manager
Handles reading, writing, and validating persistent configuration in ~/.config/google-drive/config.json
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, List

CONFIG_DIR = Path.home() / ".config" / "google-drive"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
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
        "sync_mode": "stream",  # 'stream' or 'mirror'
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

def load_config() -> Dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Deep merge defaults in case new fields were added
        merged = DEFAULT_CONFIG.copy()
        for k, v in cfg.items():
            if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

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

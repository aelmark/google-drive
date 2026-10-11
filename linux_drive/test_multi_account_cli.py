"""
Test suite for Multi-Account OAuth handling and drivectl CLI extensions.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_drive.config import (
    load_config,
    save_config,
    get_accounts,
    get_active_account_id,
    set_active_account_id,
    add_or_update_account,
    remove_account,
    get_tracked_folders,
    add_tracked_folder,
    remove_tracked_folder,
    CONFIG_FILE,
    CONFIG_DIR
)
from linux_drive.oauth import (
    oauth_manager,
    sanitize_account_id,
    TOKENS_FILE
)
from linux_drive.ipc_daemon import DriveIpcDaemon
from linux_drive.ipc_wire import McpIpcRequest, McpIpcResponse
from linux_drive.drivectl import send_ipc, is_daemon_running

def test_multi_account_and_cli():
    print("=== Testing Multi-Account Token Management and Isolation ===")
    
    # 1. Sanitize account IDs
    assert sanitize_account_id("user@gmail.com") == "user@gmail.com"
    assert sanitize_account_id("work.user+tag@domain.co.uk") == "work.user_tag@domain.co.uk"
    assert sanitize_account_id("primary") == "primary"
    assert sanitize_account_id("") == "primary"
    print("✓ sanitize_account_id verified")

    # 2. Add two accounts and check tokens isolation
    acc1_tokens = {
        "access_token": "token_personal_123",
        "refresh_token": "refresh_personal_456",
        "expires_in": 3600,
        "expires_at": time.time() + 3600,
        "email": "personal@gmail.com"
    }
    acc1_profile = {
        "email": "personal@gmail.com",
        "name": "Personal User",
        "picture": "https://example.com/p.jpg",
        "sub": "11111"
    }
    acc1_quota = {
        "limit": 100 * (1024**3),
        "usage": 25 * (1024**3),
        "usage_in_drive": 20 * (1024**3)
    }

    acc2_tokens = {
        "access_token": "token_work_789",
        "refresh_token": "refresh_work_012",
        "expires_in": 3600,
        "expires_at": time.time() + 3600,
        "email": "work@company.com"
    }
    acc2_profile = {
        "email": "work@company.com",
        "name": "Work User",
        "picture": "https://example.com/w.jpg",
        "sub": "22222"
    }
    acc2_quota = {
        "limit": 2000 * (1024**3),
        "usage": 500 * (1024**3),
        "usage_in_drive": 400 * (1024**3)
    }

    oauth_manager.save_tokens(acc1_tokens, acc1_profile, acc1_quota, account_id="personal@gmail.com", make_active=True)
    oauth_manager.save_tokens(acc2_tokens, acc2_profile, acc2_quota, account_id="work@company.com", make_active=False)

    # Verify token files exist and have 0600 permissions
    acc1_token_file = CONFIG_DIR / "accounts" / "personal@gmail.com" / "tokens.json"
    acc2_token_file = CONFIG_DIR / "accounts" / "work@company.com" / "tokens.json"
    assert acc1_token_file.exists(), f"Missing {acc1_token_file}"
    assert acc2_token_file.exists(), f"Missing {acc2_token_file}"
    assert oct(os.stat(acc1_token_file).st_mode & 0o777) == "0o600"
    assert oct(os.stat(acc2_token_file).st_mode & 0o777) == "0o600"

    # Verify active account tokens are reflected in TOKENS_FILE
    assert TOKENS_FILE.exists()
    assert oct(os.stat(TOKENS_FILE).st_mode & 0o777) == "0o600"
    with open(TOKENS_FILE, "r") as f:
        active_saved = json.load(f)
    assert active_saved["access_token"] == "token_personal_123"
    print("✓ Token file isolation and 0600 permissions verified")

    # 3. Test load_tokens for specific accounts and active fallback
    loaded_acc1 = oauth_manager.load_tokens(account_id="personal@gmail.com")
    assert loaded_acc1["access_token"] == "token_personal_123"

    loaded_acc2 = oauth_manager.load_tokens(account_id="work@company.com")
    assert loaded_acc2["access_token"] == "token_work_789"

    loaded_active = oauth_manager.load_tokens()
    assert loaded_active["access_token"] == "token_personal_123"
    print("✓ Account-scoped load_tokens and active fallback verified")

    # 4. Test switching active account
    set_active_account_id("work@company.com")
    assert get_active_account_id() == "work@company.com"

    # Simulate drivectl switch token copy
    target_token_file = oauth_manager._get_token_file("work@company.com", check_exists=True)
    with open(target_token_file, "r") as sf:
        tdata = sf.read()
    with open(TOKENS_FILE, "w") as df:
        df.write(tdata)
    os.chmod(TOKENS_FILE, 0o600)

    loaded_after_switch = oauth_manager.load_tokens()
    assert loaded_after_switch["access_token"] == "token_work_789"
    print("✓ Account switching and active tokens.json update verified")

    # 5. Test IPC Daemon handling for trigger_sync and remove_folder_from_sync
    test_db = "/tmp/test_cli_multi.db"
    test_mount = "/tmp/test_cli_mount"
    if os.path.exists(test_db):
        os.unlink(test_db)
    os.makedirs(test_mount, exist_ok=True)

    daemon = DriveIpcDaemon(db_path=test_db, mount_point=test_mount)
    d_thread = threading.Thread(target=daemon.start, daemon=True)
    d_thread.start()
    time.sleep(0.5)

    try:
        folder_a = "/tmp/test_folder_a"
        folder_b = "/tmp/test_folder_b"
        os.makedirs(folder_a, exist_ok=True)
        os.makedirs(folder_b, exist_ok=True)

        # Add folders for different accounts
        resp_add1 = send_ipc(McpIpcRequest(request_id=101, add_folder_to_sync={"path": folder_a, "account_id": "personal@gmail.com"}))
        assert resp_add1.add_folder_to_sync["success"] is True

        resp_add2 = send_ipc(McpIpcRequest(request_id=102, add_folder_to_sync={"path": folder_b, "account_id": "work@company.com"}))
        assert resp_add2.add_folder_to_sync["success"] is True

        # Test trigger_sync
        resp_sync = send_ipc(McpIpcRequest(request_id=103, trigger_sync={"path": folder_a, "account_id": "personal@gmail.com"}))
        assert resp_sync.trigger_sync["success"] is True
        assert resp_sync.trigger_sync["status"] == "synced"
        assert resp_sync.trigger_sync["items_synced"] >= 1
        print("✓ trigger_sync IPC handler verified")

        # Test remove_folder_from_sync
        resp_rm = send_ipc(McpIpcRequest(request_id=104, remove_folder_from_sync={"path": folder_a, "account_id": "personal@gmail.com"}))
        assert resp_rm.remove_folder_from_sync["success"] is True

        resp_list = send_ipc(McpIpcRequest(request_id=105, list_tracked_folders=True))
        assert folder_a not in resp_list.list_tracked_folders["paths"]
        assert folder_b in resp_list.list_tracked_folders["paths"]
        print("✓ remove_folder_from_sync IPC handler verified")

    finally:
        daemon.stop()
        if os.path.exists(test_db):
            os.unlink(test_db)

    print("\n=== ALL MULTI-ACCOUNT AND CLI TESTS PASSED! ===")

if __name__ == "__main__":
    test_multi_account_and_cli()

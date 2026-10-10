"""
End-to-end integration test for Google Drive Linux Daemon and DFD MCP Server.
"""

import sys
import os
import json
import time
import subprocess
import threading
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_drive.ipc_daemon import DriveIpcDaemon

def run_tests():
    test_db = "/tmp/test_google_drive.db"
    test_mount = "/tmp/test_google_drive_mount"
    if os.path.exists(test_db):
        os.unlink(test_db)
    os.makedirs(test_mount, exist_ok=True)

    print("=== Starting IPC Daemon ===")
    daemon = DriveIpcDaemon(db_path=test_db, mount_point=test_mount)
    daemon_thread = threading.Thread(target=daemon.start, daemon=True)
    daemon_thread.start()
    time.sleep(0.5)

    try:
        print("=== Testing DFD MCP Server over stdio ===")
        mcp_proc = subprocess.Popen(
            [sys.executable, "linux_drive/dfd_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        def send_rpc(msg):
            mcp_proc.stdin.write(json.dumps(msg) + "\n")
            mcp_proc.stdin.flush()
            line = mcp_proc.stdout.readline()
            return json.loads(line)

        # 1. Initialize
        init_res = send_rpc({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}
        })
        assert init_res["result"]["serverInfo"]["name"] == "dfd_mcp_server"
        print("✓ initialize successful")

        # 2. Tools List
        tools_res = send_rpc({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        })
        tools = tools_res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        expected_tools = [
            "dfd_get_sync_status",
            "dfd_add_folder_to_sync",
            "dfd_get_drive_url",
            "dfd_list_tracked_folders",
            "dfd_open_acl_dialog"
        ]
        for et in expected_tools:
            assert et in tool_names, f"Missing tool: {et}"
        print(f"✓ tools/list verified: {len(tools)} tools registered")

        # 3. Add folder to sync
        test_folder = "/tmp/test_sync_folder"
        os.makedirs(test_folder, exist_ok=True)
        add_res = send_rpc({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "dfd_add_folder_to_sync",
                "arguments": {
                    "path": test_folder,
                    "sync_mode": "mirror",
                    "account_id": "test@example.com"
                }
            }
        })
        assert "Successfully added" in add_res["result"]["content"][0]["text"]
        print("✓ dfd_add_folder_to_sync verified")

        # 4. List tracked folders
        list_res = send_rpc({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "dfd_list_tracked_folders",
                "arguments": {}
            }
        })
        data = json.loads(list_res["result"]["content"][0]["text"])
        assert test_folder in data["tracked_folders"]
        print(f"✓ dfd_list_tracked_folders verified: {data['tracked_folders']}")

        # 5. Get sync status
        status_res = send_rpc({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "dfd_get_sync_status",
                "arguments": {
                    "path": test_folder
                }
            }
        })
        status_data = json.loads(status_res["result"]["content"][0]["text"])
        assert status_data["status"] == "synced"
        print(f"✓ dfd_get_sync_status verified: status={status_data['status']}")

        # 6. Get Drive URL
        url_res = send_rpc({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "dfd_get_drive_url",
                "arguments": {
                    "path": test_folder
                }
            }
        })
        url_data = json.loads(url_res["result"]["content"][0]["text"])
        assert "drive.google.com" in url_data["drive_url"]
        print(f"✓ dfd_get_drive_url verified: url={url_data['drive_url']}")

        mcp_proc.terminate()
        print("\n=== ALL INTEGRATION TESTS PASSED SUCCESSFULLY! ===")

    finally:
        daemon.stop()
        if os.path.exists(test_db):
            os.unlink(test_db)

if __name__ == '__main__':
    run_tests()

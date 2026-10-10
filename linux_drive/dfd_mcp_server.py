#!/usr/bin/env python3
"""
Google Drive for Desktop (DFD) MCP Server for Linux.
Provides autonomous agents and assistants with Model Context Protocol access
to Google Drive Desktop via /tmp/drivefs_ipc.<UID>.
"""

import sys
import os
import json
import socket
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_drive.ipc_wire import (
    McpIpcRequest,
    McpIpcResponse,
    frame_message,
    read_framed_message
)

SYSTEM_PROMPT = """Google Drive for Desktop (DFD) MCP Server enables autonomous agents and assistants to seamlessly interact with local and cloud Google Drive filesystems.

### Capabilities & Tool Guide:
1. `dfd_get_sync_status`: Check if a local file or directory is actively synced with Google Drive, determine the sync mode ('stream' vs 'mirror'), and check sync state ('synced', 'syncing', 'error', 'untracked').
2. `dfd_add_folder_to_sync`: Configure a local folder to sync with Google Drive. In stream mode, the folder is safely relocated into My Drive and replaced with a local symlink so workflows remain uninterrupted.
3. `dfd_get_drive_url`: Obtain the web-accessible Google Drive URL (https://drive.google.com/...) for any synced file or folder to share with users or collaborators.
4. `dfd_list_tracked_folders`: Retrieve all local and cloud folders currently configured for Drive sync across active accounts.
5. `dfd_open_acl_dialog`: Open the native Google Drive sharing and access-control dialog for a synced item. Use this whenever a user requests to share a folder, manage permissions, add collaborators, or adjust viewer/editor roles.

### Agent Best Practices:
- **Folder Synchronization**: When a user asks to save, backup, or sync a project to Google Drive, call `dfd_add_folder_to_sync`.
- **Sharing & Collaboration**: Agents cannot silently modify ACLs without user consent. Always invoke `dfd_open_acl_dialog` to display the native OS sharing dialog when permission changes are requested.
- **Sharing Links**: When sharing links to files or project folders, invoke `dfd_get_drive_url` rather than attempting to construct URLs manually.
- **Sync Verification**: Use `dfd_get_sync_status` to verify whether a local path is already tracked before attempting to sync it."""

TOOLS = [
    {
        "name": "dfd_get_sync_status",
        "description": "Returns the synchronization status of a file or folder",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The absolute path of the file or folder"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "dfd_add_folder_to_sync",
        "description": "Adds a folder to be synchronized with a Google Drive account. You MUST ask the user which synchronization mode they use if it is not explicitly provided. NEVER guess or default the sync_mode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The absolute path of the folder to sync"
                },
                "account_id": {
                    "type": "string",
                    "description": "The Google account ID (email) to sync with"
                },
                "sync_mode": {
                    "type": "string",
                    "enum": ["mirror", "stream"],
                    "description": "Synchronization mode: 'mirror' or 'stream'. REQUIRED. You MUST ask the user if unknown. NEVER guess or assume a default mode."
                },
                "my_drive_path": {
                    "type": "string",
                    "description": "Optional path to the root of My Drive"
                }
            },
            "required": ["path", "sync_mode"]
        }
    },
    {
        "name": "dfd_get_drive_url",
        "description": "Returns the Google Drive web URL for a tracked file or folder",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The absolute path of the tracked file or folder"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "dfd_list_tracked_folders",
        "description": "Returns a list of all folders currently being synchronized",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "dfd_open_acl_dialog",
        "description": "Launches the Drive sharing dialog for a folder. No permissions are changed automatically - the user reviews and confirms all changes in the UI that appears.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "The email address of the user to share the item with or whose access needs to be fixed"
                },
                "item_id": {
                    "type": "string",
                    "description": "The Google Drive item ID of the file or folder"
                },
                "account_id": {
                    "type": "string",
                    "description": "Optional: The email address of the signed-in DFD user who is sharing (defaults to the active account if only one is signed in)"
                }
            },
            "required": ["email", "item_id"]
        }
    }
]

class DfdIpcClient:
    def __init__(self):
        self.uid = os.getuid()
        self.socket_path = f"/tmp/drivefs_ipc.{self.uid}"

    def send_request(self, req: McpIpcRequest) -> McpIpcResponse:
        if not os.path.exists(self.socket_path):
            raise ConnectionError(f"Google Drive is not running (Can't connect to socket at: {self.socket_path}). Please start Google Drive.")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
            payload = req.serialize()
            sock.sendall(frame_message(payload))
            raw_resp = read_framed_message(sock)
            if not raw_resp:
                raise ConnectionError("Connection closed before response was received.")
            return McpIpcResponse.deserialize(raw_resp)
        finally:
            sock.close()

class DfdMcpServer:
    def __init__(self):
        self.client = DfdIpcClient()
        self.req_counter = 1

    def _next_req_id(self) -> int:
        self.req_counter += 1
        return self.req_counter

    def handle_tool_call(self, tool_name: str, arguments: dict) -> dict:
        req_id = self._next_req_id()

        try:
            if tool_name == "dfd_get_sync_status":
                req = McpIpcRequest(request_id=req_id)
                req.get_sync_status = {'path': arguments.get('path', '')}
                resp = self.client.send_request(req)
                data = resp.get_sync_status or {}
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "path": arguments.get('path'),
                            "status": data.get('status', 'unknown'),
                            "description": data.get('description', '')
                        }, indent=2)
                    }]
                }

            elif tool_name == "dfd_add_folder_to_sync":
                path = arguments.get('path', '')
                sync_mode = arguments.get('sync_mode', 'mirror')
                account_id = arguments.get('account_id', '')

                if sync_mode == "stream":
                    # In stream mode, find My Drive mount and relocate folder
                    mount_req = McpIpcRequest(request_id=req_id)
                    mount_req.get_streaming_mount_points = {'account_id': account_id}
                    mount_resp = self.client.send_request(mount_req)
                    mps = (mount_resp.get_streaming_mount_points or {}).get('mount_points', [])
                    my_drive_path = arguments.get('my_drive_path')
                    if not my_drive_path and mps:
                        my_drive_path = mps[0].get('my_drive_path')

                    if not my_drive_path:
                        return {
                            "isError": True,
                            "content": [{"type": "text", "text": "Streaming mount point is not available. Please ensure Google Drive streaming is mounted."}]
                        }

                    target_dir = Path(my_drive_path) / Path(path).name
                    # Move folder and symlink back
                    os.rename(path, target_dir)
                    os.symlink(target_dir, path)

                    return {
                        "content": [{
                            "type": "text",
                            "text": f"Successfully relocated '{path}' to '{target_dir}' and created symlink for streaming synchronization."
                        }]
                    }
                else:
                    req = McpIpcRequest(request_id=req_id)
                    req.add_folder_to_sync = {'path': path, 'account_id': account_id}
                    resp = self.client.send_request(req)
                    data = resp.add_folder_to_sync or {}
                    if not data.get('success'):
                        return {
                            "isError": True,
                            "content": [{"type": "text", "text": f"Failed to add folder to sync: {data.get('error_message')}"}]
                        }
                    return {
                        "content": [{
                            "type": "text",
                            "text": f"Successfully added '{path}' to Google Drive mirror sync."
                        }]
                    }

            elif tool_name == "dfd_get_drive_url":
                req = McpIpcRequest(request_id=req_id)
                req.get_drive_url = {'path': arguments.get('path', '')}
                resp = self.client.send_request(req)
                data = resp.get_drive_url or {}
                if data.get('error_message'):
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": data.get('error_message')}]
                    }
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"drive_url": data.get('drive_url')}, indent=2)
                    }]
                }

            elif tool_name == "dfd_list_tracked_folders":
                req = McpIpcRequest(request_id=req_id)
                req.list_tracked_folders = True
                resp = self.client.send_request(req)
                data = resp.list_tracked_folders or {}
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({"tracked_folders": data.get('paths', [])}, indent=2)
                    }]
                }

            elif tool_name == "dfd_open_acl_dialog":
                req = McpIpcRequest(request_id=req_id)
                req.fix_acl = {
                    'email': arguments.get('email', ''),
                    'item_id': arguments.get('item_id', ''),
                    'account_id': arguments.get('account_id', '')
                }
                resp = self.client.send_request(req)
                data = resp.fix_acl or {}
                if not data.get('success'):
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Failed to open ACL dialog: {data.get('error_message')}"}]
                    }
                return {
                    "content": [{
                        "type": "text",
                        "text": f"Opened Google Drive sharing dialog for item {arguments.get('item_id')}."
                    }]
                }

            else:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
                }

        except Exception as e:
            return {
                "isError": True,
                "content": [{"type": "text", "text": str(e)}]
            }

    def run(self):
        """Runs the JSON-RPC MCP server on stdin/stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {str(e)}"}
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()
                continue

            msg_id = msg.get("id")
            method = msg.get("method")

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {}
                        },
                        "serverInfo": {
                            "name": "dfd_mcp_server",
                            "version": "1.0.0"
                        },
                        "instructions": SYSTEM_PROMPT
                    }
                }
            elif method == "notifications/initialized":
                continue # no response for notification
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": TOOLS
                    }
                }
            elif method == "tools/call":
                params = msg.get("params", {})
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                result = self.handle_tool_call(tool_name, arguments)
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result
                }
            elif method == "ping":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {}
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }

            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

if __name__ == '__main__':
    server = DfdMcpServer()
    server.run()

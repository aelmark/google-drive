# Google Drive Desktop & MCP Server for Linux

[![Platform: Linux](https://img.shields.io/badge/platform-Linux-blue.svg)](https://aisgit.armellini.net/aelmark/google-drive)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![GUI: GTK 3](https://img.shields.io/badge/gui-GTK%203-red.svg)](https://www.gtk.org/)
[![MCP: 2026 Ready](https://img.shields.io/badge/mcp-Model%20Context%20Protocol-purple.svg)](https://modelcontextprotocol.io/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A native Linux desktop client, background synchronization engine, and Model Context Protocol (MCP) server for Google Drive. Clean-room engineered from the official Google Drive Desktop binary protocols, providing Linux users with first-class desktop integration and enabling AI coding assistants to seamlessly interact with local and cloud files.

---

## Highlights

- **Multi-Account Concurrency**: Connect multiple Google Accounts (e.g., Google Workspace + personal Gmail) simultaneously on the same machine, each with independent sync modes (Stream vs Mirror), isolated mount points (`~/GoogleDrive`, `~/GoogleDrive - user@domain.com`), and dedicated token storage.
- **Pure-Python Sync Engine**: Background file sync engine built strictly with the Python standard library; compares MD5/mtime/size hashes in SQLite, uploads changes via Drive API v3 multipart upload, and provides live sync progress tracking.
- **Modernized Desktop Interface**: Polished GTK 3 interface following Google Material and GNOME Adwaita design language, featuring instant sync mode auto-saving, account switching, dynamic folder removal, and quick file manager shortcuts.
- **Reverse-Engineered IPC Protocol**: Drop-in compatible with the official Google Drive C++ core engine (`Cello`) using length-delimited Protocol Buffers over local UNIX domain sockets (`/tmp/drivefs_ipc.<UID>`) with kernel `SO_PEERCRED` UID security validation and extended request tags for real-time control.
- **Embedded MCP Server (`dfd_mcp_server`)**: Exposes native Model Context Protocol tools directly to AI coding assistants (Google Antigravity, Claude Desktop, Cursor, Zed) for automated syncing, file sharing, URL resolution, and quota tracking.
- **Seamless OAuth 2.0 PKCE Flow**: Zero-configuration out of the box using built-in official desktop credentials or custom Google Cloud Console project credentials. Automates browser consent and loopback token capture on `127.0.0.1:8085`.
- **Zero Third-Party Pip Dependencies**: Engineered strictly with the Python 3.10+ standard library (`urllib`, `http.server`, `hashlib`, `secrets`, `sqlite3`), ensuring zero dependency conflicts and ultra-fast Debian `.deb` packaging.
- **Auto-Starting Background Daemon**: If any interface (`google-drive`, Preferences GUI, `drivectl`, or `dfd_mcp_server`) is invoked while the background daemon is inactive, it automatically starts the service without manual intervention.
- **Persistent Reboot & Post-Install Activation**: Starts immediately upon installation and autostarts on reboot via dual integration: systemd user units (`default.target.wants`) and XDG Autostart (`~/.config/autostart`).
- **Comprehensive CLI (`drivectl`)**: Control daemon state, manage synced folders, inspect live cloud storage quotas, switch accounts, trigger manual syncs, and configure streaming parameters directly from the terminal.

---

## Architecture

```mermaid
flowchart TD
    subgraph Userspace ["Linux Desktop & Development Environment"]
        GUI["GTK 3 Preferences GUI<br/>(google-drive)"]
        CLI["CLI Management Utility<br/>(drivectl)"]
        AI["AI Assistants<br/>(Antigravity / Claude / Cursor)"]
    end

    subgraph CoreServices ["Google Drive Core Services"]
        MCP["Model Context Protocol Server<br/>(dfd_mcp_server)"]
        IPC["UNIX Socket IPC Bridge<br/>(/tmp/drivefs_ipc.UID)"]
        DAEMON["Drive IPC Daemon<br/>(linux_drive/ipc_daemon.py)"]
        OAUTH["OAuth 2.0 PKCE Manager<br/>(linux_drive/oauth.py)"]
        DB[(State Database<br/>SQLite daemon_state.db)]
    end

    subgraph GoogleCloud ["Google Cloud Platform"]
        G_AUTH["accounts.google.com<br/>(OAuth 2.0 + PKCE S256)"]
        G_API["googleapis.com<br/>(Drive API v3 + Quota)"]
    end

    GUI -->|IPC / Config| IPC
    CLI -->|IPC / Status| IPC
    AI <-->|JSON-RPC 2.0 stdio| MCP
    MCP <-->|Framed Protobuf| IPC
    IPC <--> DAEMON
    DAEMON <--> DB
    DAEMON <--> OAUTH
    OAUTH <-->|Token Exchange| G_AUTH
    OAUTH <-->|Quota / Metadata| G_API
```

---

## Installation

### Method 1: One-Line Installer (Recommended)

Run the automated installer script via `curl`:

```bash
curl -fsSL https://aisgit.armellini.net/aelmark/google-drive/raw/branch/main/install.sh | bash
```

The script automatically detects your system environment:
- If root or `sudo` is available on Debian/Ubuntu/Pop!_OS, it installs the complete `.deb` package system-wide.
- If running without root, it installs smoothly in **User Mode** directly into `~/.local/bin` and `~/.local/share`.

---

### Method 2: Debian Package (`.deb`)

If you have cloned the repository or downloaded the `.deb` directly:

```bash
# Install via apt to resolve any system dependencies automatically
sudo apt-get update
sudo apt-get install -y --fix-broken ./dist/google-drive-desktop_1.0.0_all.deb

# Enable and start the user background service
systemctl --user daemon-reload
systemctl --user enable --now google-drive
```

---

### Method 3: User-Mode (No Root / Sudo Required)

For environments where you do not have root privileges:

```bash
./install.sh --user
```

This installs binaries into `~/.local/bin`, libraries into `~/.local/share/google-drive`, desktop shortcuts into `~/.local/share/applications`, and activates a user-level systemd service in `~/.config/systemd/user/google-drive.service`.

---

## Connecting Your Google Account

### Option 1: Via the Preferences GUI
1. Open the application launcher and search for **Google Drive**, or run:
   ```bash
   google-drive
   ```
2. Navigate to the **Settings** tab.
3. Click the blue **"Sign in with Google..."** button.
4. Your default browser opens to the Google OAuth consent page. Approve the prompt, and the app connects automatically.

### Option 2: Via the Terminal
Run the CLI authorization command:
```bash
drivectl auth login
```
The command generates an RFC 7636 PKCE pair, opens your browser, listens for the callback on `http://127.0.0.1:8085/`, exchanges the code for offline refresh tokens, and saves credentials in `~/.config/google-drive/tokens.json` (`0600` permissions).

Check authentication state and storage quota anytime:
```bash
drivectl auth status
```

Output:
```text
==================================================
 Google Drive Account & OAuth Status
==================================================
 Credentials Mode : Default Official Desktop Client
 Client ID        : 947318989803-6bn6qk8qdgf4...usercontent.com
 Status           : 🟢 Authenticated
 Email            : user@example.com
 Name             : User Name
 Connected Since  : 2026-10-10T13:47:18Z
 Cloud Storage    : 4.12 GB / 15.0 GB (27.5%)
 Access Token TTL : 3540s remaining (auto-refreshes)
==================================================
```

---

### Custom Google Cloud Project Credentials (Optional)

By default, the client uses the built-in official Google Drive Desktop credentials. If you prefer to use your own Google Cloud Console OAuth 2.0 Client (e.g., for an internal Workspace domain):

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Google Drive API**.
3. Create an **OAuth 2.0 Client ID** with Application type **Desktop app**.
4. Set credentials via CLI:
   ```bash
   drivectl auth set-credentials <CLIENT_ID> <CLIENT_SECRET>
   # or import the downloaded client_secret_*.json
   drivectl auth import-credentials ~/Downloads/client_secret_*.json
   ```
5. Or configure them in the GUI under **Settings > Advanced: Custom Google Cloud OAuth 2.0 Credentials**.
6. Run `drivectl auth reset-credentials` at any time to revert to default credentials.

---

## Multi-Account Concurrency

You can connect and sync multiple Google Accounts simultaneously (for example, your company Google Workspace account alongside your personal Gmail account).

### In the Desktop Interface (`google-drive`)
- **Account Switcher**: Located prominently at the top of the Preferences window.
- **Switch Accounts**: Use the drop-down menu to switch between connected accounts instantly.
- **Add Another Account**: Select **"+ Add Account..."** to initiate an OAuth flow in your browser. The new account is added alongside existing accounts without overwriting them.
- **Independent Configuration**: Each account maintains its own:
  - Google Drive mount location (e.g. `~/GoogleDrive` for primary, `~/GoogleDrive - user@domain.com` for secondary)
  - Sync Mode (**Stream files** vs **Mirror files**)
  - Tracked folder list under "My Computer"
  - Token credentials and cache database
- **Disconnect**: Safely log out and remove an account by selecting it and clicking **Disconnect**.

### Via the Command Line (`drivectl`)
```bash
# List all connected accounts and their sync modes / mount points
drivectl account list

# Add a secondary Google account via browser login
drivectl account add

# Switch active account
drivectl account switch user@domain.com

# Remove an account and revoke tokens
drivectl account remove user@domain.com
```

---

## CLI Reference: `drivectl`

The `drivectl` utility provides complete command-line control:

| Command | Arguments | Description |
|---|---|---|
| `drivectl status` | `[--json]` | Show background daemon health, account status, live sync status, and tracked folders |
| `drivectl account list` | - | List all connected Google accounts, active status, mount points, and quotas |
| `drivectl account switch` | `<EMAIL_OR_ID>` | Switch active account in configuration |
| `drivectl account add` | `[--port PORT]` | Authenticate and register an additional Google account |
| `drivectl account remove` | `<EMAIL_OR_ID>` | Disconnect and revoke credentials for a specific account |
| `drivectl auth login` | `[--no-browser] [--port PORT]` | Authenticate Google Account via browser PKCE flow |
| `drivectl auth status` | - | Display OAuth connection, email, and live cloud storage quota |
| `drivectl auth logout` | - | Revoke Google tokens and disconnect active account |
| `drivectl auth set-credentials` | `<CLIENT_ID> <CLIENT_SECRET>` | Configure custom Google Cloud Console OAuth client |
| `drivectl auth import-credentials` | `<PATH_TO_JSON>` | Import Google Cloud `client_secret_*.json` |
| `drivectl auth reset-credentials` | - | Reset to default official desktop client credentials |
| `drivectl selective-sync list` | `[--account ID]` | List My Drive and all Shared Drives with their sync status |
| `drivectl selective-sync enable` | `<NAME_OR_ID> [--account ID]` | Enable synchronization for a specific Shared Drive or 'my-drive' |
| `drivectl selective-sync disable` | `<NAME_OR_ID> [--account ID]` | Disable synchronization for a specific Shared Drive or 'my-drive' |
| `drivectl selective-sync toggle-my-drive` | `[on\|off] [--account ID]` | Toggle My Drive personal file synchronization |
| `drivectl selective-sync select-all` | `[--account ID]` | Select all available Shared Drives for synchronization |
| `drivectl selective-sync deselect-all` | `[--account ID]` | Deselect all Shared Drives from synchronization |
| `drivectl add` | `<PATH> [--mode mirror|stream] [--account ID]` | Add a local directory to Google Drive synchronization |
| `drivectl remove` / `rm` | `<PATH>` | Remove a directory from Google Drive synchronization |
| `drivectl sync` | `[--account ID]` | Trigger immediate background sync cycle |
| `drivectl list` | `[--account ID]` | List all currently tracked directories and sync states |
| `drivectl settings` | - | Open native GTK 3 Preferences configuration dialog |
| `drivectl start` | - | Launch background IPC daemon & sync engine |
| `drivectl stop` | - | Terminate running IPC daemon & sync engine |
| `drivectl config show` | - | Print entire active JSON configuration |
| `drivectl config set` | `<KEY> <VALUE>` | Update specific setting (e.g. `storage.sync_mode`) |

---

## Selective Sync & Shared Drives (Google Workspace)

To safeguard local storage when connecting enterprise Google Workspace accounts with multiple terabytes of data:
- **My Drive & Shared Drives Separation**: `My Drive` and Google Workspace `Shared drives` are cleanly mounted side-by-side without redundant nesting (`~/GoogleDrive - {email}/My Drive/` and `~/GoogleDrive - {email}/Shared drives/`).
- **Selective Syncing**: Users choose precisely which Shared Drives to synchronize locally. Unselected Shared Drives do not consume disk space or network bandwidth.
- **Configurable via GUI & CLI**:
  - In the GTK Preferences GUI under **Google Drive**, use the **Selective Sync** card to check or uncheck individual Shared Drives or My Drive.
  - In the terminal, use `drivectl selective-sync list` and `drivectl selective-sync enable/disable "<Name>"`.
- **Large File Protection**: Individual files larger than 250 MB are skipped during bulk synchronization passes to protect disk space.
- **Disk Space Safeguard**: Sync operations pause automatically if available local disk space falls below 1 GB.


## Model Context Protocol (MCP) Integration

The included `dfd_mcp_server` implements the Model Context Protocol (JSON-RPC 2.0 stdio), allowing autonomous AI agents to interact with Google Drive.

### Configuration for Antigravity & Claude Desktop

Add the following to your `mcp_config.json` (e.g. `~/.gemini/antigravity-cli/mcp_config.json` or `~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "google-drive": {
      "command": "dfd_mcp_server",
      "args": []
    }
  }
}
```

### Available MCP Tools

1. **`dfd_get_sync_status`**: Check if a local file or directory is actively synced (`synced`, `syncing`, `untracked`).
2. **`dfd_add_folder_to_sync`**: Add local directory to sync in `stream` or `mirror` mode. In stream mode, relocates folder into My Drive and creates a transparent symlink.
3. **`dfd_get_drive_url`**: Retrieve the web URL (`https://drive.google.com/...`) for any tracked file or folder.
4. **`dfd_list_tracked_folders`**: List all synced local directories across accounts.
5. **`dfd_open_acl_dialog`**: Launch the Google Drive sharing and permission dialog for an item.
6. **`dfd_get_account_info`**: Query account authentication status, user profile, and cloud storage quota metrics.

---

## File Synchronization Modes

| Mode | Behavior | Best Used For |
|---|---|---|
| **Stream** | Stores all files in the cloud only. Files are downloaded on-demand when accessed and cached locally with LRU eviction. Folders are symlinked into My Drive. | Conserving local SSD storage, large video or dataset repositories. |
| **Mirror** | Stores all files both in the cloud and locally on your computer. All files are available offline at all times. Bidirectional syncing reflects edits immediately. | Working offline, project source code repositories, frequent document editing. |

---

## Updating & Uninstallation

### Updating
Run the update script to pull the latest changes and update your installation:
```bash
./update.sh
```

### Uninstallation
Run the uninstaller script to cleanly remove application binaries, desktop shortcuts, and systemd services:
```bash
./uninstall.sh

# To also delete credentials, config, and cache databases:
./uninstall.sh --purge
```

---

## Repository & Development

- **Forgejo Repository**: [https://aisgit.armellini.net/aelmark/google-drive](https://aisgit.armellini.net/aelmark/google-drive)
- **Maintainer**: Mark Sterling (`aelmark`)
- **License**: Apache-2.0

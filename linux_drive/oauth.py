"""
Google Drive Linux - OAuth 2.0 Authentication Manager
Supports standard Google OAuth 2.0 PKCE flow, loopback redirect server,
browser launch, token exchange, user profile retrieval, Drive API quota inspection,
and automatic token refreshing using pure Python standard library.
"""

import os
import sys
import json
import time
import socket
import secrets
import hashlib
import base64
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable

# Configuration and token storage paths
CONFIG_DIR = Path.home() / ".config" / "google-drive"
TOKENS_FILE = CONFIG_DIR / "tokens.json"

# Extracted default official Google Drive Desktop credentials
DEFAULT_CLIENT_ID = "947318989803-6bn6qk8qdgf4n4g3pfee6491hc0brc4i.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "gtDkCw7oR54acTIqay0eliAO"

# Google OAuth Endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user,storageQuota"

# OAuth Scopes for Google Drive desktop client
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "email"
]

def base64url_encode(data: bytes) -> str:
    """Base64url encode without padding as required by RFC 7636 (PKCE)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

def generate_pkce_pair() -> Tuple[str, str]:
    """Generate (code_verifier, code_challenge) using SHA-256 (S256)."""
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64url_encode(verifier_bytes)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64url_encode(digest)
    return verifier, challenge

def find_available_port(start_port: int = 8085, max_attempts: int = 20) -> int:
    """Find an available TCP port for the loopback redirect server."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    # Fallback to dynamic port assigned by OS
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the OAuth2 loopback redirect callback."""
    
    server: "OAuthLoopbackServer"  # type annotation for IDE

    def log_message(self, format, *args):
        # Suppress standard HTTP server access logs
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            err = params.get("error", ["Unknown error"])[0]
            self.server.oauth_error = err
            self._respond_html(400, "Google Drive - Authentication Failed", self._build_error_html(err))
            return

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if not code:
            self._respond_html(400, "Invalid Request", self._build_error_html("No authorization code received."))
            return

        if state != self.server.expected_state:
            self._respond_html(400, "Security Verification Failed", self._build_error_html("CSRF state mismatch."))
            return

        self.server.auth_code = code
        self._respond_html(200, "Google Drive Connected!", self._build_success_html())

    def _respond_html(self, status_code: int, title: str, body_html: str):
        content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f8f9fa;
            color: #202124;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }}
        .card {{
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
            padding: 40px;
            max-width: 480px;
            text-align: center;
            border: 1px solid #dadce0;
        }}
        .logo {{
            width: 72px;
            height: 72px;
            margin-bottom: 20px;
        }}
        h1 {{
            font-size: 24px;
            font-weight: 500;
            margin-bottom: 12px;
            color: #202124;
        }}
        p {{
            font-size: 15px;
            line-height: 1.5;
            color: #5f6368;
            margin-bottom: 24px;
        }}
        .badge {{
            display: inline-block;
            background: #e8f0fe;
            color: #1a73e8;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
        }}
        .error {{
            background: #fce8e6;
            color: #d93025;
        }}
    </style>
</head>
<body>
    <div class="card">
        {body_html}
    </div>
</body>
</html>"""
        encoded = content.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _build_success_html(self) -> str:
        svg_drive = """<svg class="logo" viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
            <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da"/>
            <path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44a9.06 9.06 0 0 0 -1.2 4.5h27.5z" fill="#00ac47"/>
            <path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.502l5.852 11.5z" fill="#ea4335"/>
            <path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d"/>
            <path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc"/>
            <path d="m73.4 26.5-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 28h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00"/>
        </svg>"""
        return f"""{svg_drive}
        <h1>Connected to Google Drive</h1>
        <p>Your Google Drive account has been authenticated successfully for Linux desktop sync and MCP tools.</p>
        <div class="badge">✓ Safe to close this tab and return to the application</div>"""

    def _build_error_html(self, err_msg: str) -> str:
        return f"""<h1>Authentication Failed</h1>
        <p>Google returned an authentication error:</p>
        <div class="badge error">{err_msg}</div>
        <p style="margin-top:20px;">Please return to the application and try again.</p>"""


class OAuthLoopbackServer(HTTPServer):
    """Local single-use HTTP server to capture authorization code."""
    def __init__(self, server_address, RequestHandlerClass, expected_state: str):
        super().__init__(server_address, RequestHandlerClass)
        self.expected_state = expected_state
        self.auth_code: Optional[str] = None
        self.oauth_error: Optional[str] = None


class OAuthManager:
    """Manages Google OAuth2 client credentials, authentication, and tokens."""

    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def get_client_credentials(self) -> Tuple[str, str]:
        """
        Returns (client_id, client_secret).
        Checks in order:
        1. Environment variables GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET
        2. Config file custom credentials (~/.config/google-drive/config.json)
        3. ~/.config/google-drive/client_secrets.json (Google Cloud Console download)
        4. Extracted default desktop credentials
        """
        env_id = os.environ.get("GOOGLE_DRIVE_CLIENT_ID")
        env_secret = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET")
        if env_id and env_secret:
            return env_id.strip(), env_secret.strip()

        # Check config.json
        from linux_drive.config import load_config
        cfg = load_config()
        oauth_cfg = cfg.get("oauth", {})
        if oauth_cfg.get("use_custom_credentials") and oauth_cfg.get("custom_client_id"):
            return (
                oauth_cfg.get("custom_client_id", "").strip(),
                oauth_cfg.get("custom_client_secret", "").strip()
            )

        # Check client_secrets.json
        secrets_path = CONFIG_DIR / "client_secrets.json"
        if secrets_path.exists():
            try:
                with open(secrets_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                client_dict = data.get("installed") or data.get("web") or data
                if "client_id" in client_dict and "client_secret" in client_dict:
                    return client_dict["client_id"].strip(), client_dict["client_secret"].strip()
            except Exception:
                pass

        # Default extracted desktop credentials
        return DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET

    def set_custom_client_credentials(self, client_id: str, client_secret: str) -> None:
        """Saves custom Google Cloud credentials in config.json and client_secrets.json."""
        from linux_drive.config import load_config, save_config
        cfg = load_config()
        if "oauth" not in cfg:
            cfg["oauth"] = {}
        cfg["oauth"]["use_custom_credentials"] = True
        cfg["oauth"]["custom_client_id"] = client_id.strip()
        cfg["oauth"]["custom_client_secret"] = client_secret.strip()
        if "account" not in cfg:
            cfg["account"] = {}
        cfg["account"]["client_id"] = client_id.strip()
        save_config(cfg)

        # Also persist to standard client_secrets.json
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            secrets_path = CONFIG_DIR / "client_secrets.json"
            data = {
                "installed": {
                    "client_id": client_id.strip(),
                    "client_secret": client_secret.strip(),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "redirect_uris": ["http://localhost", "http://127.0.0.1"]
                }
            }
            with open(secrets_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.chmod(secrets_path, 0o600)
        except Exception:
            pass

    def reset_to_default_credentials(self) -> None:
        """Resets to use the built-in desktop credentials."""
        from linux_drive.config import load_config, save_config
        cfg = load_config()
        if "oauth" not in cfg:
            cfg["oauth"] = {}
        cfg["oauth"]["use_custom_credentials"] = False
        save_config(cfg)

    def import_credentials_file(self, filepath: str) -> Tuple[str, str]:
        """Imports Google Cloud client_secret_*.json and saves it."""
        path = Path(filepath).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        client_dict = data.get("installed") or data.get("web") or data
        if "client_id" not in client_dict or "client_secret" not in client_dict:
            raise ValueError("Invalid client_secrets.json: missing 'client_id' or 'client_secret'")

        client_id = client_dict["client_id"].strip()
        client_secret = client_dict["client_secret"].strip()

        # Save copy to ~/.config/google-drive/client_secrets.json
        dest = CONFIG_DIR / "client_secrets.json"
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(dest, 0o600)

        self.set_custom_client_credentials(client_id, client_secret)
        return client_id, client_secret

    def build_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        client_id: Optional[str] = None
    ) -> str:
        """Constructs the Google OAuth2 consent URL."""
        if not client_id:
            client_id, _ = self.get_client_credentials()

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(DEFAULT_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256"
        }
        return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def start_oauth_flow(
        self,
        port: Optional[int] = None,
        timeout_seconds: int = 180,
        open_browser: bool = True,
        url_callback: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        """
        Executes complete OAuth 2.0 PKCE flow:
        1. Spins up loopback HTTP server
        2. Opens browser to Google Sign-in
        3. Captures code callback
        4. Exchanges code for tokens
        5. Fetches profile and storage quota
        6. Saves credentials securely in ~/.config/google-drive/tokens.json
        """
        client_id, client_secret = self.get_client_credentials()
        actual_port = port if port else find_available_port()
        redirect_uri = f"http://127.0.0.1:{actual_port}/"

        verifier, challenge = generate_pkce_pair()
        state = secrets.token_hex(16)
        auth_url = self.build_authorization_url(redirect_uri, state, challenge, client_id)

        server = OAuthLoopbackServer(("127.0.0.1", actual_port), OAuthCallbackHandler, expected_state=state)
        server.timeout = 1.0

        if url_callback:
            url_callback(auth_url)

        if open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            server.handle_request()
            if server.auth_code or server.oauth_error:
                break

        server.server_close()

        if server.oauth_error:
            raise RuntimeError(f"OAuth error from Google: {server.oauth_error}")

        if not server.auth_code:
            raise TimeoutError("OAuth sign-in timed out waiting for browser callback.")

        # Exchange authorization code for tokens
        token_data = self.exchange_code(server.auth_code, verifier, redirect_uri, client_id, client_secret)

        # Retrieve user profile and Drive quota
        profile = self.fetch_user_profile(token_data["access_token"])
        quota = self.fetch_storage_quota(token_data["access_token"])

        # Save tokens and update config
        self.save_tokens(token_data, profile, quota)

        return {
            "token": token_data,
            "profile": profile,
            "quota": quota
        }

    def exchange_code(
        self,
        code: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str
    ) -> Dict[str, Any]:
        """Exchanges authorization code for access & refresh tokens."""
        post_data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier
        }
        encoded = urllib.parse.urlencode(post_data).encode("utf-8")
        req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GoogleDriveDesktop/1.0.0 (Linux)"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Token exchange failed ({e.code}): {err_body}")

        data["expires_at"] = time.time() + data.get("expires_in", 3600)
        return data

    def refresh_access_token(self) -> str:
        """Refreshes the stored access token using the refresh_token."""
        tokens = self.load_tokens()
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No refresh_token found. User must re-authenticate.")

        client_id, client_secret = self.get_client_credentials()
        post_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        encoded = urllib.parse.urlencode(post_data).encode("utf-8")
        req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GoogleDriveDesktop/1.0.0 (Linux)"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                new_tokens = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Failed to refresh token ({e.code}): {err_body}")

        tokens["access_token"] = new_tokens["access_token"]
        tokens["expires_in"] = new_tokens.get("expires_in", 3600)
        tokens["expires_at"] = time.time() + tokens["expires_in"]
        if "refresh_token" in new_tokens:
            tokens["refresh_token"] = new_tokens["refresh_token"]

        self.save_tokens(tokens)
        return tokens["access_token"]

    def get_valid_access_token(self) -> str:
        """Returns a valid access token, automatically refreshing it if expired or expiring within 60s."""
        tokens = self.load_tokens()
        if not tokens or "access_token" not in tokens:
            raise RuntimeError("Not authenticated. Run 'drivectl auth login' or connect via Preferences.")

        expires_at = tokens.get("expires_at", 0)
        # Refresh if token expires in less than 60 seconds
        if time.time() >= (expires_at - 60):
            return self.refresh_access_token()

        return tokens["access_token"]

    def fetch_user_profile(self, access_token: str) -> Dict[str, Any]:
        """
        Fetches Google user profile metadata.
        Queries Drive API v3 'about' endpoint (which returns user.displayName,
        user.emailAddress, user.photoLink) and complements with userinfo endpoint.
        Does not require the restricted 'userinfo.profile' scope.
        """
        profile = {"email": "", "name": "Google User", "picture": "", "sub": ""}

        # 1. Drive API v3 about endpoint (works with drive scope)
        try:
            req = urllib.request.Request(
                GOOGLE_DRIVE_ABOUT_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": "GoogleDriveDesktop/1.0.0 (Linux)"
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                user = data.get("user", {})
                if user.get("emailAddress"):
                    profile["email"] = user["emailAddress"]
                if user.get("displayName"):
                    profile["name"] = user["displayName"]
                if user.get("photoLink"):
                    profile["picture"] = user["photoLink"]
                if user.get("permissionId"):
                    profile["sub"] = user["permissionId"]
        except Exception:
            pass

        # 2. Complement with userinfo endpoint (email scope provides email and sub)
        try:
            req = urllib.request.Request(
                GOOGLE_USERINFO_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": "GoogleDriveDesktop/1.0.0 (Linux)"
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                uinfo = json.loads(resp.read().decode("utf-8"))
                if not profile.get("email") and uinfo.get("email"):
                    profile["email"] = uinfo["email"]
                if not profile.get("sub") and uinfo.get("sub"):
                    profile["sub"] = uinfo["sub"]
                if (not profile.get("name") or profile["name"] == "Google User") and uinfo.get("name"):
                    profile["name"] = uinfo["name"]
                if not profile.get("picture") and uinfo.get("picture"):
                    profile["picture"] = uinfo["picture"]
        except Exception:
            pass

        return profile

    def fetch_storage_quota(self, access_token: str) -> Dict[str, Any]:
        """Fetches Google Drive storage quota info from Drive API v3."""
        req = urllib.request.Request(
            GOOGLE_DRIVE_ABOUT_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "GoogleDriveDesktop/1.0.0 (Linux)"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                quota = data.get("storageQuota", {})
                return {
                    "limit": int(quota.get("limit", 0)),
                    "usage": int(quota.get("usage", 0)),
                    "usage_in_drive": int(quota.get("usageInDrive", 0)),
                    "usage_in_drive_trash": int(quota.get("usageInDriveTrash", 0))
                }
        except Exception:
            return {"limit": 0, "usage": 0, "usage_in_drive": 0}

    def save_tokens(
        self,
        tokens: Dict[str, Any],
        profile: Optional[Dict[str, Any]] = None,
        quota: Optional[Dict[str, Any]] = None
    ) -> None:
        """Saves tokens with 0600 permissions and updates config.json."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        os.chmod(TOKENS_FILE, 0o600)

        # Update config.json with account info
        from linux_drive.config import load_config, save_config
        cfg = load_config()

        email = profile.get("email", "") if profile else tokens.get("email", "")
        name = profile.get("name", "") if profile else ""
        avatar = profile.get("picture", "") if profile else ""
        sub = profile.get("sub", "") if profile else ""

        if "account" not in cfg:
            cfg["account"] = {}

        if email:
            cfg["account"]["email"] = email
        if name:
            cfg["account"]["name"] = name
        if avatar:
            cfg["account"]["avatar_url"] = avatar
        if sub:
            cfg["account"]["sub"] = sub
        cfg["account"]["authenticated"] = True
        cfg["account"]["connected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if quota:
            cfg["account"]["storage_quota"] = quota

        save_config(cfg)

    def load_tokens(self) -> Dict[str, Any]:
        """Loads tokens from ~/.config/google-drive/tokens.json."""
        if not TOKENS_FILE.exists():
            return {}
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def is_authenticated(self) -> bool:
        """Checks if a valid token or refresh token is available."""
        tokens = self.load_tokens()
        return bool(tokens.get("access_token") or tokens.get("refresh_token"))

    def revoke_tokens(self) -> bool:
        """Revokes token with Google and cleans up local credentials."""
        tokens = self.load_tokens()
        token_to_revoke = tokens.get("refresh_token") or tokens.get("access_token")

        if token_to_revoke:
            try:
                encoded = urllib.parse.urlencode({"token": token_to_revoke}).encode("utf-8")
                req = urllib.request.Request(
                    GOOGLE_REVOKE_URL,
                    data=encoded,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass

        if TOKENS_FILE.exists():
            try:
                TOKENS_FILE.unlink()
            except OSError:
                pass

        from linux_drive.config import load_config, save_config
        cfg = load_config()
        if "account" in cfg:
            cfg["account"]["authenticated"] = False
            cfg["account"]["email"] = ""
            cfg["account"]["name"] = ""
            cfg["account"]["avatar_url"] = ""
            cfg["account"]["storage_quota"] = {"limit": 0, "usage": 0}
        save_config(cfg)
        return True


# Global singleton instance
oauth_manager = OAuthManager()

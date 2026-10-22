"""Glean: search your company's documents, chats and tickets from promptloom.

`promptloom glean login` signs in through the browser (OAuth, as Glean's MCP
clients do) and searches over Glean's MCP endpoint; the instance is found from
the Glean desktop app when installed. Alternatively an API token (Client API,
SEARCH scope) in provider.toml (mode 600) or GLEAN_API_TOKEN uses the REST
Search API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import config, providers

OAUTH_FILE = config.CONFIG_DIR / "glean-oauth.json"  # holds tokens: mode 600
MCP_PATH = "/mcp/default"


class GleanError(Exception):
    pass


def backend_url(value: str) -> str:
    """Accept 'acme', 'acme-be.glean.com' or a full URL; return the API base."""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"{value}-be.glean.com" if "." not in value else value
        value = "https://" + value
    return value


def installed_url() -> str:
    """The backend the Glean desktop app is signed in to (only its address is read)."""
    if sys.platform == "darwin":
        base = Path("~/Library/Application Support").expanduser()
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    try:
        data = json.loads((base / "Glean" / "glean.json").read_text())
        return backend_url(data["state"]["app"]["auth"]["qeBase"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def settings(cfg: dict | None = None) -> dict:
    cfg = cfg or config.load()
    glean = cfg.get("glean", {})
    return {
        "url": backend_url(os.environ.get("GLEAN_URL") or glean.get("url", "")) or installed_url(),
        "token": os.environ.get("GLEAN_API_TOKEN") or cfg.get("keys", {}).get("glean", ""),
        "auth_type": glean.get("auth_type", ""),
        "actas": glean.get("actas", ""),
    }


def configured(cfg: dict | None = None) -> bool:
    s = settings(cfg)
    return bool(s["url"] and (s["token"] or OAUTH_FILE.exists()))


def save(url: str = "", token: str = "") -> None:
    if url:
        config.save_settings("glean", {"url": url})
    if token:
        stored = providers.load_file(config.PROVIDER_FILE)
        stored.setdefault("keys", {})["glean"] = token
        providers.save_file(config.PROVIDER_FILE, stored)


def search(query: str, limit: int = 10, cfg: dict | None = None, timeout: float = 20) -> list[dict]:
    s = settings(cfg)
    if not s["url"]:
        raise GleanError("Glean is not set up (promptloom glean login)")
    if not s["token"]:
        return _mcp_search(s["url"], query, limit, timeout)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {s['token']}"}
    if s["auth_type"]:
        headers["X-Glean-Auth-Type"] = s["auth_type"]  # e.g. "OAUTH" for user-scoped tokens
    if s["actas"]:
        headers["X-Scio-Actas"] = s["actas"]  # global tokens: the user to search as
    body = {"query": query, "pageSize": max(1, min(limit, 50)), "maxSnippetSize": 240}
    request = urllib.request.Request(s["url"] + "/rest/api/v1/search",
                                     data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GleanError(f"Glean rejected the token ({exc.code})") from exc
        raise GleanError(f"Glean returned {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
    except (OSError, ValueError) as exc:
        raise GleanError(f"Glean unreachable: {exc}") from exc
    return [_hit(r) for r in data.get("results", []) if isinstance(r, dict)]


def _hit(result: dict) -> dict:
    doc = result.get("document") or {}
    snippets = [s.get("text", "") for s in result.get("snippets", []) if isinstance(s, dict)]
    return {
        "title": result.get("title") or doc.get("title") or result.get("url", ""),
        "url": result.get("url") or doc.get("url", ""),
        "source": doc.get("datasource", ""),
        "type": doc.get("docType", ""),
        "snippet": " … ".join(s.strip() for s in snippets if s.strip())[:400],
    }


# Sign in with the browser (OAuth), the way Glean's own MCP clients do: no API
# token to create, and it works for anyone who can sign in to Glean.

def _post_form(url: str, fields: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                     headers={"Accept": "application/json"})
    return _open(request)


def _open(request, timeout: float = 30) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise GleanError(f"Glean returned {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
    except (OSError, ValueError) as exc:
        raise GleanError(f"Glean unreachable: {exc}") from exc


def _save_oauth(data: dict) -> None:
    OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(OAUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.chmod(OAUTH_FILE, 0o600)


def login(url: str = "", wait: float = 300, announce=print) -> str:
    """Sign in through the browser. Returns the backend URL signed in to."""
    url = backend_url(url) or settings()["url"]
    if not url:
        raise GleanError("Which Glean? promptloom glean login --url COMPANY")
    meta = _open(urllib.request.Request(url + "/.well-known/oauth-authorization-server"))
    if not meta.get("registration_endpoint"):
        raise GleanError("this Glean does not allow browser sign-in; use an API token instead")

    got: dict = {}
    done = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            got.update({k: v[-1] for k, v in urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Promptloom is connected to Glean.</h3>You can close this tab.")
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    client = _open(urllib.request.Request(
        meta["registration_endpoint"], headers={"Content-Type": "application/json"},
        data=json.dumps({"client_name": "Promptloom", "redirect_uris": [redirect],
                         "grant_types": ["authorization_code", "refresh_token"],
                         "response_types": ["code"],
                         "token_endpoint_auth_method": "none"}).encode()))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "scope": "mcp", "resource": url + MCP_PATH})
    link = meta["authorization_endpoint"] + "?" + query
    announce("Opening your browser to sign in to Glean…\nIf nothing opens, go to:\n" + link)
    webbrowser.open(link)

    server.timeout = 1
    deadline = time.time() + wait
    while not done.is_set() and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise GleanError("sign-in was not completed" + (f": {got['error']}" if "error" in got else ""))
    token = _post_form(meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": client["client_id"], "code_verifier": verifier, "resource": url + MCP_PATH})
    _save_oauth({"url": url, "client_id": client["client_id"],
                 "token_endpoint": meta["token_endpoint"], **_tokens(token)})
    return url


def logout() -> bool:
    if OAUTH_FILE.exists():
        OAUTH_FILE.unlink()
        return True
    return False


def _tokens(token: dict) -> dict:
    return {"access_token": token["access_token"],
            "refresh_token": token.get("refresh_token", ""),
            "expires_at": time.time() + float(token.get("expires_in") or 3600)}


def _access_token() -> dict:
    try:
        saved = json.loads(OAUTH_FILE.read_text())
    except (OSError, ValueError) as exc:
        raise GleanError("not signed in to Glean (promptloom glean login)") from exc
    if saved["expires_at"] - 60 > time.time():
        return saved
    if not saved.get("refresh_token"):
        raise GleanError("the Glean sign-in expired (promptloom glean login)")
    try:
        fresh = _post_form(saved["token_endpoint"], {
            "grant_type": "refresh_token", "refresh_token": saved["refresh_token"],
            "client_id": saved["client_id"]})
    except GleanError as exc:
        raise GleanError("the Glean sign-in expired (promptloom glean login)") from exc
    saved.update(_tokens({"refresh_token": saved["refresh_token"], **fresh}))
    _save_oauth(saved)
    return saved


class _Mcp:
    """Just enough of MCP over HTTP to call one of Glean's tools."""

    def __init__(self, url: str, token: str, timeout: float):
        self.url, self.token, self.timeout, self.session, self.n = url, token, timeout, "", 0

    def rpc(self, method: str, params: dict | None = None, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            body["id"] = self.n
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id") or self.session
                raw = response.read().decode()
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise GleanError("Glean rejected the sign-in (promptloom glean login)") from exc
            raise GleanError(f"Glean returned {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
        except OSError as exc:
            raise GleanError(f"Glean unreachable: {exc}") from exc
        if notify or not raw.strip():
            return {}
        if "text/event-stream" in kind:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    message = json.loads(line[5:])
                    if message.get("id") == self.n:
                        break
            else:
                raise GleanError("Glean sent no answer")
        else:
            message = json.loads(raw)
        if "error" in message:
            raise GleanError(str(message["error"].get("message", message["error"])))
        return message.get("result") or {}


def _mcp_search(url: str, query: str, limit: int, timeout: float) -> list[dict]:
    saved = _access_token()
    mcp = _Mcp(saved["url"] + MCP_PATH, saved["access_token"], max(timeout, 60))
    mcp.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "promptloom", "version": "1"}})
    mcp.rpc("notifications/initialized", notify=True)
    tools = {t["name"]: t for t in mcp.rpc("tools/list").get("tools", [])}
    name = "search" if "search" in tools else next((n for n in tools if "search" in n), "")
    if not name:
        raise GleanError("this Glean offers no search tool over MCP")
    result = mcp.rpc("tools/call", {"name": name, "arguments": {"query": query}})
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        raise GleanError(text[:200] or "Glean search failed")
    return _from_text(text)[:limit]


def _from_text(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    rows = data.get("results") if isinstance(data, dict) else data
    if isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows):
        return [_hit(r) for r in rows]
    return [{"title": "Glean results", "url": "", "source": "", "type": "", "snippet": text.strip()}] \
        if text.strip() else []

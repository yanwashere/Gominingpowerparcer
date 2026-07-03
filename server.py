#!/usr/bin/env python3
"""
GoMining Power Checker — local web server.
Serves the UI, proxies GoMining API calls, and auto-extracts the JWT from Safari.

Usage:
    python server.py
    python server.py --port 8080
    python server.py --token eyJ...   # override token manually
"""

import argparse
import http.server
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import webbrowser
from pathlib import Path

PORT = 8080
GOMINING_API = "https://api.gomining.com"
GOMINING_HOST = "app.gomining.com"

# ── My-miners cache (filled from /api/nft/get-my) ────────────────────────────
_my_miners_cache: dict | None = None
_my_miners_cache_ts: float = 0.0
_MY_MINERS_TTL = 1800  # 30 minutes

# ── Token extraction ──────────────────────────────────────────────────────────

def _safari_cookies_db() -> Path | None:
    """Return the path to Safari's Cookies.binarycookies converted to SQLite, or None."""
    home = Path.home()
    safari_bin = home / "Library/Cookies/Cookies.binarycookies"
    if safari_bin.exists():
        return safari_bin
    return None

def _read_token_browser_cookie3() -> str | None:
    """Try browser_cookie3 (pip install browser-cookie3) — works on Mac."""
    try:
        import browser_cookie3
        jar = browser_cookie3.safari(domain_name=GOMINING_HOST)
        for c in jar:
            if c.name == "access_token":
                return c.value
        # also try Chrome / Firefox as fallback
        for loader in (browser_cookie3.chrome, browser_cookie3.firefox):
            try:
                jar = loader(domain_name=GOMINING_HOST)
                for c in jar:
                    if c.name == "access_token":
                        return c.value
            except Exception:
                pass
    except ImportError:
        pass
    except Exception:
        pass
    return None

def _read_token_sqlite_direct() -> str | None:
    """
    On macOS, copy Safari's Cookies.binarycookies to a temp file and parse it.
    The binary format: magic + pages → we look for 'access_token' followed by the value.
    Falls back to trying the Chrome SQLite cookie DB.
    """
    # Chrome path on Mac
    chrome_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    if chrome_path.exists():
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            import shutil
            shutil.copy2(str(chrome_path), tmp_path)
            conn = sqlite3.connect(tmp_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM cookies WHERE host_key LIKE ? AND name=?",
                (f"%{GOMINING_HOST}%", "access_token"),
            )
            row = cur.fetchone()
            conn.close()
            os.unlink(tmp_path)
            if row:
                val = row[0]
                if isinstance(val, bytes):
                    # Chrome on Mac encrypts cookies — skip
                    pass
                elif val:
                    return val
        except Exception:
            pass

    # Firefox path on Mac
    ff_root = Path.home() / "Library/Application Support/Firefox/Profiles"
    if ff_root.exists():
        for profile in ff_root.iterdir():
            cookies_db = profile / "cookies.sqlite"
            if cookies_db.exists():
                try:
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                        tmp_path = tmp.name
                    import shutil
                    shutil.copy2(str(cookies_db), tmp_path)
                    conn = sqlite3.connect(tmp_path)
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT value FROM moz_cookies WHERE host LIKE ? AND name=?",
                        (f"%{GOMINING_HOST}%", "access_token"),
                    )
                    row = cur.fetchone()
                    conn.close()
                    os.unlink(tmp_path)
                    if row and row[0]:
                        return row[0]
                except Exception:
                    pass
    return None

def _read_token_osascript() -> str | None:
    """
    Ask Safari via AppleScript to dump localStorage from app.gomining.com.
    This only works when Safari has the page open and Web Inspector is enabled.
    """
    if platform.system() != "Darwin":
        return None
    script = """
    tell application "Safari"
        set theResult to ""
        set theWindows to every window
        repeat with w in theWindows
            set theTabs to every tab of w
            repeat with t in theTabs
                if URL of t contains "app.gomining.com" then
                    set theResult to do JavaScript "
                        (function(){
                            var t = localStorage.getItem('access_token');
                            if(!t){ var k = Object.keys(localStorage).find(k=>k.toLowerCase().includes('token')); t = k ? localStorage.getItem(k) : ''; }
                            return t || '';
                        })()
                    " in t
                    if theResult is not "" then return theResult
                end if
            end repeat
        end repeat
        return theResult
    end tell
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        val = result.stdout.strip()
        if val and len(val) > 20:
            return val
    except Exception:
        pass
    return None

def extract_token() -> tuple[str | None, str]:
    """
    Try multiple strategies to find the GoMining JWT.
    Returns (token_or_None, source_description).
    """
    # 1. browser_cookie3 (most reliable if installed)
    token = _read_token_browser_cookie3()
    if token:
        return token, "browser_cookie3"

    # 2. Direct SQLite (Chrome/Firefox only — Safari uses binary format)
    token = _read_token_sqlite_direct()
    if token:
        return token, "cookie-db"

    # 3. AppleScript → Safari localStorage
    token = _read_token_osascript()
    if token:
        return token, "safari-js"

    return None, "not-found"


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    token: str | None = None          # set by main() after extraction
    html_path: Path = Path("index.html")

    def log_message(self, fmt, *args):
        pass  # silence default access log

    def send_json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── /token — return current JWT info ─────────────────────────────────
        if path == "/token":
            if self.token:
                # decode exp from JWT payload (no verification needed)
                try:
                    import base64
                    payload_b64 = self.token.split(".")[1]
                    payload_b64 += "=" * (-len(payload_b64) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                    exp = payload.get("exp", 0)
                except Exception:
                    exp = 0
                self.send_json(200, {"ok": True, "token": self.token, "exp": exp})
            else:
                self.send_json(200, {"ok": False, "token": None, "exp": 0})
            return

        # ── /refresh-token — re-extract from browser ─────────────────────────
        if path == "/refresh-token":
            token, source = extract_token()
            if token:
                Handler.token = token
                self.send_json(200, {"ok": True, "source": source})
            else:
                self.send_json(200, {"ok": False, "source": source})
            return

        # ── /set-token — accept token from user paste ─────────────────────────
        if path == "/set-token":
            t = qs.get("t", [""])[0].strip()
            if t and len(t) > 20:
                Handler.token = t
                self.send_json(200, {"ok": True})
            else:
                self.send_json(400, {"ok": False, "error": "empty token"})
            return

        # ── /proxy — forward to GoMining API ─────────────────────────────────
        if path.startswith("/proxy/"):
            gm_path = path[len("/proxy"):]  # keep leading slash
            gm_url = GOMINING_API + gm_path
            if parsed.query:
                gm_url += "?" + parsed.query

            if not Handler.token:
                self.send_json(401, {"error": "no_token", "message": "Токен не найден"})
                return

            req = urllib.request.Request(gm_url)
            req.add_header("Authorization", f"Bearer {Handler.token}")
            req.add_header("Accept", "application/json")
            req.add_header("Origin", "https://app.gomining.com")
            req.add_header("Referer", "https://app.gomining.com/")

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_json(502, {"error": "proxy_error", "message": str(e)})
            return

        # ── / or /index.html — serve UI ───────────────────────────────────────
        if path in ("/", "/index.html", "/local"):
            html_file = self.html_path
            if not html_file.exists():
                self.send_json(404, {"error": "index.html not found"})
                return
            content = html_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # ── /gm-by-address — probe GoMining API for miner by blockchain address ──
        if path == "/gm-by-address":
            addr = qs.get("addr", [""])[0].strip()  # 0:hex format
            if not addr or not Handler.token:
                self.send_json(400, {"error": "addr and token required"})
                return
            result = self._probe_gm_by_address(addr)
            self.send_json(200, result)
            return

        # ── /tonapi-debug — return raw tonapi NFT response for debugging ─────────
        if path == "/tonapi-debug":
            addr = qs.get("addr", [""])[0].strip()
            if not addr:
                self.send_json(400, {"error": "addr required"})
                return
            try:
                req = urllib.request.Request(f"https://tonapi.io/v2/nfts/{urllib.parse.quote(addr)}")
                req.add_header("Accept", "application/json")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── /getgems-uuid — scrape getgems NFT page to find GoMining UUID ───────
        if path == "/getgems-uuid":
            nft_addr = qs.get("addr", [""])[0].strip()
            if not nft_addr:
                self.send_json(400, {"error": "addr required"})
                return
            uuid = self._scrape_getgems_uuid(nft_addr)
            if uuid:
                self.send_json(200, {"ok": True, "uuid": uuid})
            else:
                self.send_json(200, {"ok": False, "uuid": None})
            return

        # ── /gm-my-miners — return cached /api/nft/get-my result ─────────────────
        if path == "/gm-my-miners":
            global _my_miners_cache, _my_miners_cache_ts
            if not Handler.token:
                self.send_json(401, {"ok": False, "error": "no_token"})
                return
            now = time.time()
            force = qs.get("force", [""])[0] == "1"
            if not force and _my_miners_cache and (now - _my_miners_cache_ts < _MY_MINERS_TTL):
                self.send_json(200, {"ok": True, "cached": True, "data": _my_miners_cache})
                return
            data = self._gm_request("/api/nft/get-my")
            if data is None:
                self.send_json(502, {"ok": False, "error": "GoMining API returned nothing"})
                return
            # GoMining wraps the list in {data:[...]} or returns it directly
            miners_list = data if isinstance(data, list) else (data.get("data") or [])
            by_address  = {}
            by_token_id = {}
            for m in miners_list:
                addr = m.get("address", "")
                tid  = m.get("tokenId")
                uuid = m.get("externalUrlId")
                if addr and uuid:
                    by_address[addr.lower()] = m
                if tid is not None and uuid:
                    by_token_id[str(tid)] = m
            _my_miners_cache = {
                "by_address": by_address,
                "by_token_id": by_token_id,
                "count": len(miners_list),
            }
            _my_miners_cache_ts = now
            self.send_json(200, {"ok": True, "cached": False, "data": _my_miners_cache})
            return

        # ── /gm-by-token-id — look up any miner by tokenId ───────────────────────
        if path == "/gm-by-token-id":
            token_id = qs.get("tokenId", [""])[0].strip()
            if not token_id or not Handler.token:
                self.send_json(400, {"ok": False, "error": "tokenId and token required"})
                return
            result = self._probe_gm_by_token_id(token_id)
            self.send_json(200, result)
            return

        self.send_json(404, {"error": "not found"})

    def _gm_request(self, path: str, params: dict | None = None) -> dict | None:
        """Make a single GoMining API GET request, return parsed JSON or None."""
        url = GOMINING_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {Handler.token}")
        req.add_header("Accept", "application/json")
        req.add_header("Origin", "https://app.gomining.com")
        req.add_header("Referer", "https://app.gomining.com/")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                print(f"[gm] {path} → HTTP {e.code}")
        except Exception:
            pass
        return None

    def _probe_gm_by_address(self, raw_addr: str) -> dict:
        """Try every plausible GoMining endpoint that accepts a blockchain address."""
        candidates = [
            ("/api/nft/get-by-address",          {"address": raw_addr}),
            ("/api/nft/get-by-nft-address",       {"address": raw_addr}),
            ("/api/nft/get-by-ton-address",        {"address": raw_addr}),
            ("/api/nft/get-by-blockchain-address", {"address": raw_addr}),
            ("/api/nft/by-address",                {"address": raw_addr}),
            ("/api/nft",                           {"address": raw_addr}),
            ("/api/v1/nfts/by-address",            {"address": raw_addr}),
            ("/api/nft/get-by-address",            {"nftAddress": raw_addr}),
            ("/api/nft/get-by-address",            {"blockchainAddress": raw_addr}),
            ("/api/nft/get-by-address",            {"tonAddress": raw_addr}),
        ]
        for endpoint, params in candidates:
            data = self._gm_request(endpoint, params)
            if data:
                print(f"[gm] HIT: {endpoint} params={list(params.keys())}")
                return {"ok": True, "endpoint": endpoint, "params": list(params.keys()), "data": data}
        return {"ok": False, "tried": len(candidates)}

    def _probe_gm_by_token_id(self, token_id: str) -> dict:
        """Try GoMining endpoints that accept a tokenId (miner number from name)."""
        # Collection IDs seen in /api/nft/get-my responses
        col_ids = [514, 513, 511, 510]
        candidates = [
            ("/api/nft/get-by-token-id", {"tokenId": token_id}),
            ("/api/nft/get-by-token-id", {"token_id": token_id}),
            ("/api/v1/nfts/by-token-id",  {"tokenId": token_id}),
        ]
        # Also try with explicit collection IDs
        for cid in col_ids:
            candidates.append(("/api/nft/get-by-token-id", {"tokenId": token_id, "nftCollectionId": cid}))
        for endpoint, params in candidates:
            data = self._gm_request(endpoint, params)
            if data:
                print(f"[gm] token-id HIT: {endpoint} params={list(params.keys())}")
                return {"ok": True, "endpoint": endpoint, "data": data}
        return {"ok": False, "tried": len(candidates)}

    def _scrape_getgems_uuid(self, nft_addr: str) -> str | None:
        """Fetch the getgems NFT page and extract GoMining externalUrlId (UUID)."""
        import re
        urls_to_try = [
            f"https://getgems.io/nft/{nft_addr}",
            f"https://getgems.io/collection/{nft_addr}",
        ]
        uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        for url in urls_to_try:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                    # look for gomining.com URL containing UUID
                    m = re.search(r"gomining\.com/nft/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", html, re.I)
                    if m:
                        return m.group(1)
                    # fallback: any UUID in the page near "gomining"
                    for chunk in re.findall(r".{0,50}gomining.{0,50}", html, re.I):
                        m2 = uuid_re.search(chunk)
                        if m2:
                            return m2.group(0)
            except Exception:
                pass
        return None

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # ── /proxy-getgems — forward GraphQL to getgems (no CORS) ────────────
        if path == "/proxy-getgems":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = urllib.request.Request(
                "https://api.getgems.io/graphql",
                data=body,
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("Origin", "https://getgems.io")
            req.add_header("Referer", "https://getgems.io/")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
            req.add_header("Accept-Language", "en-US,en;q=0.9")
            req.add_header("sec-ch-ua", '"Google Chrome";v="125"')
            req.add_header("sec-ch-ua-platform", '"macOS"')
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp_body = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_body)
            except urllib.error.HTTPError as e:
                resp_body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)
            except Exception as e:
                self.send_json(502, {"error": "proxy_error", "message": str(e)})
            return

        self.send_json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="GoMining Power Checker — local server")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--token", default="", help="GoMining JWT token (skip auto-extract)")
    ap.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = ap.parse_args()

    Handler.html_path = Path(__file__).parent / "index.html"

    # Extract token
    if args.token:
        Handler.token = args.token
        print(f"[token] using manually provided token")
    else:
        print("[token] searching Safari/Chrome/Firefox cookies...")
        token, source = extract_token()
        if token:
            Handler.token = token
            print(f"[token] found via {source} ✓")
        else:
            print("[token] not found — you can paste it in the UI")

    url = f"http://localhost:{args.port}"
    print(f"[server] listening on {url}")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopped")


if __name__ == "__main__":
    main()

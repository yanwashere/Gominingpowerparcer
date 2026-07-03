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
import urllib.request
import urllib.parse
import urllib.error
import webbrowser
from pathlib import Path

PORT = 8080
GOMINING_API = "https://api.gomining.com"
GOMINING_HOST = "app.gomining.com"

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

        self.send_json(404, {"error": "not found"})

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

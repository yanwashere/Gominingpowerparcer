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

# ── GoMining marketplace cache (filled from marketplace listing API) ───────────
_gm_market_cache: dict | None = None  # {"by_name_num": {"213775": miner, ...}, "count": N}
_gm_market_cache_ts: float = 0.0
_GM_MARKET_TTL = 900  # 15 minutes (listings change more often)

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

        # ── /gm-market-debug — probe many marketplace endpoint variants ────────────
        if path == "/gm-market-debug":
            if not Handler.token:
                self.send_json(401, {"error": "no_token"})
                return
            candidates = [
                ("GET",  "/api/nft/marketplace-index",       {}),
                ("GET",  "/api/nft/marketplace/index",       {}),
                ("GET",  "/api/marketplace/index",           {}),
                ("GET",  "/api/marketplace/nft",             {"status": "available", "page": 1, "perPage": 20}),
                ("GET",  "/api/marketplace/nft",             {"page": 1, "limit": 20}),
                ("GET",  "/api/nft/marketplace",             {"page": 1, "limit": 20}),
                ("GET",  "/api/nft/get-marketplace",         {}),
                ("POST", "/api/nft/marketplace-index",       {}),
                ("GET",  "/api/nft/marketplace-index",       {"page": 1, "perPage": 20}),
                ("GET",  "/api/v1/marketplace/nft",          {"page": 1, "limit": 20}),
                ("GET",  "/api/nft",                         {"status": "available", "marketplace": "gmt-secondary", "page": 1}),
            ]
            results = []
            for method, ep, params in candidates:
                url = GOMINING_API + ep
                if method == "GET" and params:
                    url += "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, method=method)
                if method == "POST":
                    req.data = json.dumps(params).encode()
                    req.add_header("Content-Type", "application/json")
                req.add_header("Authorization", f"Bearer {Handler.token}")
                req.add_header("Accept", "application/json")
                req.add_header("Origin", "https://app.gomining.com")
                req.add_header("Referer", "https://app.gomining.com/")
                try:
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        body = resp.read()
                        parsed = json.loads(body)
                        inner = parsed.get("data", {}) if isinstance(parsed, dict) else {}
                        arr_len = 0
                        if isinstance(inner, dict):
                            arr = inner.get("array") or inner.get("items") or inner.get("nfts") or []
                            arr_len = len(arr) if isinstance(arr, list) else 0
                        elif isinstance(parsed, list):
                            arr_len = len(parsed)
                        results.append({
                            "method": method, "url": url, "status": 200,
                            "top_keys": list(parsed.keys()) if isinstance(parsed, dict) else f"list[{arr_len}]",
                            "arr_len": arr_len, "hit": arr_len > 0,
                        })
                except urllib.error.HTTPError as e:
                    results.append({"method": method, "url": url, "status": e.code, "hit": False})
                except Exception as ex:
                    results.append({"method": method, "url": url, "error": str(ex)[:80], "hit": False})
            hits = [r for r in results if r.get("hit")]
            self.send_json(200, {"hits": hits, "all": results})
            return

        # ── /gm-market-scan — scan GoMining marketplace, cache name→miner ──────────
        if path == "/gm-market-scan":
            global _gm_market_cache, _gm_market_cache_ts
            if not Handler.token:
                self.send_json(401, {"ok": False, "error": "no_token"})
                return
            now = time.time()
            force = qs.get("force", [""])[0] == "1"
            if not force and _gm_market_cache and (now - _gm_market_cache_ts < _GM_MARKET_TTL):
                self.send_json(200, {"ok": True, "cached": True, "data": _gm_market_cache})
                return
            result = self._scan_gm_marketplace()
            if result is None:
                self.send_json(502, {"ok": False, "error": "Could not reach GoMining marketplace API"})
                return
            _gm_market_cache = result
            _gm_market_cache_ts = now
            self.send_json(200, {"ok": True, "cached": False, "data": _gm_market_cache})
            return

        # ── /gm-lookup-by-name — look up a miner by its name number ──────────────
        if path == "/gm-lookup-by-name":
            name = qs.get("name", [""])[0].strip()  # e.g. "MINEBOX 213775" or "213775"
            if not name or not Handler.token:
                self.send_json(400, {"ok": False, "error": "name and token required"})
                return
            import re as _re
            num_match = _re.search(r"\d{4,7}", name)
            if not num_match:
                self.send_json(400, {"ok": False, "error": "no number found in name"})
                return
            num = num_match.group(0)
            # Ensure marketplace cache is loaded
            now = time.time()
            if not _gm_market_cache or (now - _gm_market_cache_ts >= _GM_MARKET_TTL):
                result = self._scan_gm_marketplace()
                if result:
                    _gm_market_cache = result
                    _gm_market_cache_ts = now
            if _gm_market_cache:
                miner = _gm_market_cache.get("by_name_num", {}).get(num)
                if miner:
                    self.send_json(200, {"ok": True, "found": True, "data": miner})
                    return
                # Also try exact name match (e.g. "MINEBOX 349973" → "349973")
                for key, val in _gm_market_cache.get("by_name_num", {}).items():
                    if num in key:
                        self.send_json(200, {"ok": True, "found": True, "data": val})
                        return
            self.send_json(200, {"ok": True, "found": False, "num": num,
                                 "cached": _gm_market_cache is not None,
                                 "cacheSize": len((_gm_market_cache or {}).get("by_name_num", {}))})
            return

        # ── /gm-lookup-by-ton-addr — find miner UUID via marketplace TON address index ──
        if path == "/gm-lookup-by-ton-addr":
            addr = qs.get("addr", [""])[0].strip().lower()
            if not addr or not Handler.token:
                self.send_json(400, {"ok": False, "error": "addr and token required"})
                return
            now = time.time()
            if not _gm_market_cache or (now - _gm_market_cache_ts >= _GM_MARKET_TTL):
                result = self._scan_gm_marketplace()
                if result:
                    _gm_market_cache = result
                    _gm_market_cache_ts = now
            if not _gm_market_cache:
                self.send_json(502, {"ok": False, "error": "marketplace unavailable"})
                return
            miner = _gm_market_cache.get("by_ton_address", {}).get(addr)
            if not miner:
                self.send_json(200, {"ok": True, "found": False,
                                     "cacheSize": len(_gm_market_cache.get("by_ton_address", {}))})
                return
            uuid = miner.get("externalUrlId")
            if not uuid:
                ipfs_url = miner.get("ipfs")
                if ipfs_url:
                    uuid = self._uuid_from_ipfs(ipfs_url)
            self.send_json(200, {"ok": True, "found": True, "uuid": uuid, "miner": miner})
            return

        # ── /gm-baseline — get BASELINE_POWER from on-chain NFT attributes ─────────
        if path == "/gm-baseline":
            addr = qs.get("addr", [""])[0].strip()
            if not addr:
                self.send_json(400, {"ok": False, "error": "addr required"})
                return
            try:
                import re as _re
                nft_url = f"https://tonapi.io/v2/nfts/{urllib.parse.quote(addr, safe='')}"
                req = urllib.request.Request(nft_url)
                req.add_header("Accept", "application/json")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    nft = json.loads(resp.read())
                attrs = nft.get("metadata", {}).get("attributes", [])
                baseline = None
                for a in attrs:
                    if str(a.get("trait_type", "")).upper() == "BASELINE_POWER":
                        try: baseline = float(a.get("value", 0))
                        except: pass
                        break
                self.send_json(200, {"ok": True, "baseline_power": baseline, "attributes": attrs})
            except Exception as ex:
                self.send_json(502, {"ok": False, "error": str(ex)[:200]})
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

        # ── /debug-nft-blockchain — get_nft_data + metadata URL probe ───────────
        if path == "/debug-nft-blockchain":
            addr = qs.get("addr", [""])[0].strip()
            tonapi_key = qs.get("key", [""])[0].strip()
            if not addr:
                self.send_json(400, {"error": "addr required"})
                return
            result = self._debug_nft_blockchain(addr, tonapi_key)
            self.send_json(200, result)
            return

        # ── /debug-getgems-nft — query getgems GraphQL for an NFT address ────────
        if path == "/debug-getgems-nft":
            addr = qs.get("addr", [""])[0].strip()
            if not addr:
                self.send_json(400, {"error": "addr required"})
                return
            result = self._debug_getgems_nft(addr)
            self.send_json(200, result)
            return

        self.send_json(404, {"error": "not found"})

    @staticmethod
    def _decode_boc_string(hex_str: str) -> str | None:
        """
        Extract the text content from a simple single-cell TON BOC (hex-encoded).
        Works by finding the longest run of printable ASCII bytes in the raw BOC data —
        the BOC header is always non-printable, so the first long printable run IS the content.
        """
        try:
            data = bytes.fromhex(str(hex_str).replace(" ", ""))
            best, current = "", []
            for b in data:
                if 0x20 <= b <= 0x7E:   # printable ASCII
                    current.append(chr(b))
                else:
                    if len(current) > len(best):
                        best = "".join(current)
                    current = []
            if current and len(current) > len(best):
                best = "".join(current)
            return best if len(best) >= 2 else None
        except Exception:
            return None

    def _debug_nft_blockchain(self, nft_addr: str, tonapi_key: str = "") -> dict:
        """
        Deep-probe an NFT address to find the GoMining UUID without auth:
        1. tonapi /v2/nfts/{addr}            — parsed metadata + top-level fields
        2. blockchain get_nft_data           — raw individual_content BOC from chain
        3. blockchain get_collection_data    — collection base URL (to construct full metadata URL)
        4. Decode BOC → path, combine with base URL, fetch GoMining metadata JSON
        """
        import re as _re
        UUID_RE = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _re.I)
        results = {}

        def _tonapi_get(path: str, params: dict | None = None) -> tuple[int, dict | None]:
            url = f"https://tonapi.io{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            if tonapi_key:
                req.add_header("Authorization", f"Bearer {tonapi_key}")
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, None
            except Exception as ex:
                return 0, {"error": str(ex)}

        def _fetch_json(url: str) -> tuple[int, dict | None]:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, None
            except Exception as ex:
                return 0, {"error": str(ex)[:80]}

        addr_enc = urllib.parse.quote(nft_addr, safe="")

        # ── 1. tonapi NFT metadata ────────────────────────────────────────────────
        status, nft = _tonapi_get(f"/v2/nfts/{addr_enc}")
        meta = (nft or {}).get("metadata") or {}
        # extract owner wallet for wallet-based GoMining lookups
        owner_wallet: str | None = None
        if nft:
            owner_obj = nft.get("owner") or {}
            owner_wallet = owner_obj.get("address") if isinstance(owner_obj, dict) else None
            all_meta = json.dumps(nft)
            results["tonapi_nft"] = {
                "http_status": status,
                "index": nft.get("index"),
                "name": meta.get("name"),
                "image": meta.get("image"),
                "external_url": meta.get("external_url"),
                "owner_wallet": owner_wallet,
                "buttons": meta.get("buttons"),
                "attributes": meta.get("attributes"),
                "metadata_keys": list(meta.keys()),
                "top_level_keys": list(nft.keys()),
                "uuids_found": list(set(UUID_RE.findall(all_meta))),
            }
        else:
            results["tonapi_nft"] = {"http_status": status, "error": "no data"}

        # ── 2. blockchain get_nft_data — raw individual_content + collection addr ──
        status2, chain = _tonapi_get(
            f"/v2/blockchain/accounts/{addr_enc}/methods/get_nft_data"
        )
        ind_boc = None
        collection_addr_raw = None
        decoded_path = None
        if chain:
            decoded = chain.get("decoded") or {}
            ind_boc = decoded.get("individual_content")
            collection_addr_raw = decoded.get("collection_address")
            if ind_boc:
                decoded_path = self._decode_boc_string(ind_boc)
            stack_str = json.dumps(chain.get("stack") or [])
            results["blockchain_get_nft_data"] = {
                "http_status": status2,
                "decoded_index": decoded.get("index"),
                "individual_content_boc": ind_boc,
                "individual_content_decoded": decoded_path,
                "collection_address": collection_addr_raw,
                "uuids_in_stack": list(set(UUID_RE.findall(stack_str))),
            }
        else:
            results["blockchain_get_nft_data"] = {"http_status": status2, "error": "no data"}

        # ── 3. get_collection_data — find the base metadata URL ──────────────────
        # collection_content points to the collection's own metadata FILE.
        # The item metadata directory = parent of that file.
        # e.g. "https://s.getgems.io/nft/b/c/694ea6dd.../meta.json"
        #   → directory: "https://s.getgems.io/nft/b/c/694ea6dd.../"
        #   → item URL:   "https://s.getgems.io/nft/b/c/694ea6dd.../1070/meta.json"
        collection_meta_url = None   # URL of collection's meta.json
        collection_dir_url = None    # directory (parent of meta.json) — base for items
        if collection_addr_raw:
            col_enc = urllib.parse.quote(collection_addr_raw, safe="")
            status3, coll = _tonapi_get(
                f"/v2/blockchain/accounts/{col_enc}/methods/get_collection_data"
            )
            if coll:
                coll_decoded = coll.get("decoded") or {}
                col_content_boc = coll_decoded.get("collection_content")
                col_base_decoded = self._decode_boc_string(col_content_boc) if col_content_boc else None
                # Derive directory by stripping the filename part
                if col_base_decoded:
                    collection_meta_url = col_base_decoded
                    if "/" in col_base_decoded:
                        collection_dir_url = col_base_decoded.rsplit("/", 1)[0] + "/"
                    else:
                        collection_dir_url = col_base_decoded.rstrip("/") + "/"
                results["blockchain_get_collection_data"] = {
                    "http_status": status3,
                    "collection_content_boc": col_content_boc,
                    "collection_meta_url": collection_meta_url,
                    "collection_dir_url": collection_dir_url,
                    "decoded_keys": list(coll_decoded.keys()),
                }
            else:
                results["blockchain_get_collection_data"] = {"http_status": status3, "error": "no data"}

        # ── 4. Construct full metadata URL and fetch it ───────────────────────────
        # The correct item URL = collection_dir_url + individual_content_path
        # e.g. "https://s.getgems.io/nft/b/c/694ea6dd.../" + "1070/meta.json"
        index = (nft or {}).get("index")
        metadata_probes: dict = {}

        def _probe(url: str, extra_headers: dict | None = None) -> None:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
            # For getgems CDN: add referer + origin so it looks like a browser request
            if "getgems" in url or "s.getgems" in url:
                req.add_header("Origin", "https://getgems.io")
                req.add_header("Referer", "https://getgems.io/")
                req.add_header("Accept-Language", "en-US,en;q=0.9")
                req.add_header("sec-ch-ua", '"Google Chrome";v="125", "Chromium";v="125"')
                req.add_header("sec-ch-ua-mobile", "?0")
                req.add_header("sec-ch-ua-platform", '"macOS"')
                req.add_header("sec-fetch-dest", "empty")
                req.add_header("sec-fetch-mode", "cors")
                req.add_header("sec-fetch-site", "same-site")
            if extra_headers:
                for k, v in extra_headers.items():
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read())
                    all_text = json.dumps(body)
                    uuids = list(set(UUID_RE.findall(all_text)))
                    metadata_probes[url] = {
                        "ok": True,
                        "keys": list(body.keys()) if isinstance(body, dict) else "list",
                        "uuids_found": uuids,
                        "preview": {k: str(v)[:120] for k, v in list(body.items())[:12]} if isinstance(body, dict) else {},
                    }
            except urllib.error.HTTPError as e:
                metadata_probes[url] = {"ok": False, "http_status": e.code}
            except Exception as ex:
                metadata_probes[url] = {"ok": False, "error": str(ex)[:80]}

        # PRIMARY: collection_dir + decoded_path — this is the correct construction
        if collection_dir_url and decoded_path:
            _probe(collection_dir_url + decoded_path.lstrip("/"))

        # Also try the collection meta.json to see what it contains
        if collection_meta_url:
            _probe(collection_meta_url)

        # Fallback: known GoMining CDN domains + decoded path
        if decoded_path:
            for base in [
                "https://nft.gomining.com",
                "https://api.gomining.com/nft-metadata",
            ]:
                _probe(f"{base}/{decoded_path.lstrip('/')}")

        results["metadata_url_probes"] = metadata_probes

        # ── 5. GoMining API probes — try to find UUID by token index / address ────
        gm_probes: dict = {}

        def _probe_gm(url: str, headers_extra: dict | None = None) -> None:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
            req.add_header("Origin", "https://app.gomining.com")
            req.add_header("Referer", "https://app.gomining.com/")
            if Handler.token:
                req.add_header("Authorization", f"Bearer {Handler.token}")
            if headers_extra:
                for k, v in headers_extra.items():
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read())
                    body_str = json.dumps(body)
                    uuids = list(set(UUID_RE.findall(body_str)))
                    gm_probes[url] = {
                        "ok": True,
                        "http_status": resp.status,
                        "uuids_found": uuids,
                        "keys": list(body.keys()) if isinstance(body, dict) else "list",
                        "preview": {k: str(v)[:120] for k, v in list(body.items())[:8]} if isinstance(body, dict) else {},
                    }
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode(errors="replace")[:200]
                except Exception:
                    err_body = ""
                gm_probes[url] = {"ok": False, "http_status": e.code, "body": err_body}
            except Exception as ex:
                gm_probes[url] = {"ok": False, "error": str(ex)[:80]}

        tok_index = (nft or {}).get("index")
        if tok_index is not None:
            # GoMining API: try various endpoint patterns with token index
            for ep in [
                f"/api/nft/get-by-token-id?tokenId={tok_index}",
                f"/api/nft/{tok_index}",
                f"/api/nft/details/{tok_index}",
                f"/api/nft/info?tokenId={tok_index}",
                f"/api/marketplace/nft/{tok_index}",
            ]:
                _probe_gm(f"https://api.gomining.com{ep}")

        # Try by TON address
        _probe_gm(f"https://api.gomining.com/api/nft/by-address?address={urllib.parse.quote(nft_addr, safe='')}")
        _probe_gm(f"https://api.gomining.com/api/nft/ton/{urllib.parse.quote(nft_addr, safe='')}")

        # Try following the buttons URI (might redirect to GoMining app with UUID)
        buttons_uri = None
        if meta.get("buttons") and isinstance(meta["buttons"], list):
            first_btn = meta["buttons"][0]
            if isinstance(first_btn, dict):
                buttons_uri = first_btn.get("uri") or first_btn.get("url")
        if buttons_uri and "gomining" not in buttons_uri.lower():
            # follow redirect without reading body — just capture final URL
            try:
                req = urllib.request.Request(buttons_uri)
                req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
                req.add_header("Accept", "text/html,application/xhtml+xml,*/*")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    final_url = resp.url
                    uuids_in_redirect = list(set(UUID_RE.findall(final_url)))
                    gm_probes["buttons_uri_redirect"] = {
                        "original": buttons_uri,
                        "final_url": final_url,
                        "uuids_found": uuids_in_redirect,
                    }
            except urllib.error.HTTPError as e:
                gm_probes["buttons_uri_redirect"] = {
                    "original": buttons_uri,
                    "http_status": e.code,
                    "uuids_found": [],
                }
            except Exception as ex:
                gm_probes["buttons_uri_redirect"] = {
                    "original": buttons_uri,
                    "error": str(ex)[:80],
                    "uuids_found": [],
                }

        # Wallet-based lookups — try the owner's wallet address
        # GoMining might have public-read endpoints (like get-by-external-url-id)
        # that don't check ownership, only require auth
        wallets_to_try: list[str] = []
        if owner_wallet:
            wallets_to_try.append(owner_wallet)
        # Also try UQ-form if owner_wallet looks like raw (0:...)
        for w in list(wallets_to_try):
            if w.startswith("0:"):
                try:
                    # convert 0:hex → UQ... (non-bounceable) by naive base64url
                    import base64
                    raw_bytes = b"\x51\xb8" + bytes.fromhex(w[2:])  # workchain=0 non-bounceable
                    crc = 0
                    for b in raw_bytes:
                        crc ^= b << 8
                        for _ in range(8):
                            crc = (crc << 1) ^ (0x1021 if crc & 0x8000 else 0)
                    crc &= 0xFFFF
                    full = raw_bytes + bytes([crc >> 8, crc & 0xFF])
                    wallets_to_try.append(base64.urlsafe_b64encode(full).decode().rstrip("="))
                except Exception:
                    pass

        for w_addr in wallets_to_try:
            w_enc = urllib.parse.quote(w_addr, safe="")
            for ep, pname in [
                ("/api/nft/get-by-wallet-address", "walletAddress"),
                ("/api/nft/get-by-wallet-address", "address"),
                ("/api/nft/get-by-owner",           "ownerAddress"),
                ("/api/nft/get-by-owner",           "wallet"),
                ("/api/nft/get-by-owner-wallet",    "walletAddress"),
                ("/api/nft/get-my",                 "walletAddress"),
                ("/api/user/get-by-wallet",         "walletAddress"),
                ("/api/wallet/nfts",                "address"),
            ]:
                _probe_gm(f"https://api.gomining.com{ep}?{pname}={w_enc}")

        results["gomining_api_probes"] = gm_probes

        # ── Summary: only collect actual UUID-format strings ─────────────────────
        all_uuids: set = set()
        def _collect_uuids(d: dict) -> None:
            for v in d.values():
                if isinstance(v, str):
                    all_uuids.update(UUID_RE.findall(v))
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, str):
                            all_uuids.update(UUID_RE.findall(item))
                elif isinstance(v, dict):
                    _collect_uuids(v)
        _collect_uuids(results)

        correct_item_url = (collection_dir_url + decoded_path.lstrip("/")) \
            if collection_dir_url and decoded_path else None

        return {
            "ok": True,
            "addr": nft_addr,
            "decoded_path": decoded_path,
            "collection_meta_url": collection_meta_url,
            "collection_dir_url": collection_dir_url,
            "correct_item_url": correct_item_url,
            "all_uuids_found": list(all_uuids),
            "results": results,
        }

    @staticmethod
    def _getgems_headers() -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://getgems.io",
            "Referer": "https://getgems.io/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Google Chrome";v="125"',
            "sec-ch-ua-platform": '"macOS"',
        }

    def _debug_getgems_nft(self, nft_addr: str) -> dict:
        """Query getgems for an NFT address via GraphQL + public REST API."""
        import re as _re
        UUID_RE = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _re.I)

        results = {}

        # ── 1. GraphQL (same query as the browser uses, only valid fields) ────────
        query = """
query Q($address: String!) {
  alphaNftItem(address: $address) {
    address index name description externalLink
    attributes { key value }
    sale { ... on NftSaleFix { fullPrice } }
  }
}"""
        gql_body = json.dumps({"query": query, "variables": {"address": nft_addr}}).encode()
        gql_req = urllib.request.Request("https://api.getgems.io/graphql", data=gql_body, method="POST")
        for k, v in self._getgems_headers().items():
            gql_req.add_header(k, v)
        try:
            with urllib.request.urlopen(gql_req, timeout=15) as resp:
                gql_raw = json.loads(resp.read())
            item = gql_raw.get("data", {}).get("alphaNftItem") or {}
            all_text = json.dumps(item)
            results["graphql"] = {
                "ok": True,
                "item": item,
                "item_keys": list(item.keys()),
                "external_link": item.get("externalLink"),
                "uuids_found": list(set(UUID_RE.findall(all_text))),
                "gomining_urls": _re.findall(r"https?://[^\s\"'<>]*gomining[^\s\"'<>]*", all_text, _re.I),
                "errors": gql_raw.get("errors", []),
            }
        except urllib.error.HTTPError as e:
            raw_body = e.read().decode(errors="replace")
            urls_in_error = _re.findall(r"https?://[^\s\"'\\}<>\]]+", raw_body)
            results["graphql"] = {
                "ok": False,
                "http_status": e.code,
                "body": raw_body[:2000],
                "urls_in_error": urls_in_error,
            }
        except Exception as ex:
            results["graphql"] = {"ok": False, "error": str(ex)}

        # ── 2. Official API from error body + known REST endpoints ────────────────
        official_urls_to_try: list[str] = []

        # Extract URLs from error body and try them with the NFT address
        for err_url in results.get("graphql", {}).get("urls_in_error", []):
            if "getgems" in err_url and "graphql" not in err_url:
                official_urls_to_try.append(err_url.rstrip("/") + "/" + urllib.parse.quote(nft_addr, safe=""))
                official_urls_to_try.append(err_url)  # also try as-is (might be a docs URL)

        # Known REST endpoint patterns (fallback if error has no URL)
        official_urls_to_try += [
            f"https://api.getgems.io/v2/nft/item/{urllib.parse.quote(nft_addr, safe='')}",
            f"https://api.getgems.io/v2/nft/{urllib.parse.quote(nft_addr, safe='')}",
            f"https://api.getgems.io/v1/nft/{urllib.parse.quote(nft_addr, safe='')}",
        ]

        seen_urls: set = set()
        for url in official_urls_to_try:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            rest_req = urllib.request.Request(url)
            for k, v in self._getgems_headers().items():
                rest_req.add_header(k, v)
            try:
                with urllib.request.urlopen(rest_req, timeout=10) as resp:
                    rest_raw = json.loads(resp.read())
                all_text = json.dumps(rest_raw)
                results["rest"] = {
                    "ok": True,
                    "url": url,
                    "data": rest_raw,
                    "uuids_found": list(set(UUID_RE.findall(all_text))),
                    "gomining_urls": _re.findall(r"https?://[^\s\"'<>]*gomining[^\s\"'<>]*", all_text, _re.I),
                    "external_link": rest_raw.get("externalLink") or rest_raw.get("external_url") or rest_raw.get("external_link"),
                }
                break
            except urllib.error.HTTPError as e2:
                results[f"rest_{urllib.parse.quote(url, safe='')[-40:]}"] = {"ok": False, "http_status": e2.code, "url": url}
            except Exception as ex2:
                results[f"rest_err_{url[-30:]}"] = {"ok": False, "error": str(ex2)[:80], "url": url}

        # ── Summary ───────────────────────────────────────────────────────────────
        all_uuids = set()
        for v in results.values():
            if isinstance(v, dict):
                all_uuids.update(v.get("uuids_found", []))

        return {
            "ok": True,
            "addr": nft_addr,
            "uuids_found": list(all_uuids),
            "results": results,
        }

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

    def _gm_post(self, path: str, body: dict) -> dict | None:
        """Make a GoMining API POST request with JSON body, return parsed JSON or None."""
        url = GOMINING_API + path
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {Handler.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Origin", "https://app.gomining.com")
        req.add_header("Referer", "https://app.gomining.com/")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"[gm POST] {path} → HTTP {e.code}: {e.read()[:200]}")
        except Exception as ex:
            print(f"[gm POST] {path} → {ex}")
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
        # All known nftCollectionIds seen in responses (244 = older virtual; 510-514 = TON-minted)
        col_ids = [514, 513, 511, 510, 244, 245, 246, 512]
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

    def _scan_gm_marketplace(self, max_pages: int = 50, per_page: int = 100) -> dict | None:
        """
        Scan GoMining marketplace via POST /api/nft/marketplace-index.
        Returns {"by_name_num": {"4806": miner, ...}, "by_ext_id": {ext_id: miner}, "count": N}
        """
        import re as _re
        ENDPOINT = "/api/nft/marketplace-index"

        by_name_num:    dict = {}
        by_ext_id:      dict = {}
        by_ton_address: dict = {}

        def _extract(data) -> tuple[list, dict]:
            """Return (items_list, meta_dict) from any response shape."""
            if isinstance(data, list):
                return data, {}
            if not isinstance(data, dict):
                return [], {}
            inner = data.get("data")
            if isinstance(inner, dict):
                arr = inner.get("array") or inner.get("items") or inner.get("nfts") or []
                return (arr if isinstance(arr, list) else []), inner
            arr = data.get("array") or data.get("items") or []
            return (arr if isinstance(arr, list) else []), data

        # Page-body variants to try (GoMining browser sends 444 bytes so there's a body)
        def _make_body(page: int) -> dict:
            return {
                "page": page,
                "perPage": per_page,
                "limit": per_page,
                "offset": (page - 1) * per_page,
                "filters": {},
                "sort": {},
            }

        total_known: int | None = None

        for page in range(1, max_pages + 1):
            body = _make_body(page) if page > 1 else {}   # try empty body first
            data = self._gm_post(ENDPOINT, body)

            # If empty body only returned 20, retry with explicit perPage
            if page == 1 and data is not None:
                items0, meta0 = _extract(data)
                total_known = meta0.get("total") or meta0.get("count") or meta0.get("totalCount")
                if total_known:
                    try: total_known = int(total_known)
                    except: total_known = None
                for m in items0:
                    self._index_market_miner(m, by_name_num, by_ext_id, by_ton_address, _re)
                print(f"[marketplace] page 1 (empty body): {len(items0)} items, total={total_known}")
                # If first page returned less than per_page, try again with explicit body
                if len(items0) < per_page:
                    data2 = self._gm_post(ENDPOINT, _make_body(1))
                    if data2:
                        items2, meta2 = _extract(data2)
                        if len(items2) > len(items0):
                            # Explicit body gives more — use it
                            by_name_num.clear(); by_ext_id.clear()
                            for m in items2: self._index_market_miner(m, by_name_num, by_ext_id, by_ton_address, _re)
                            total_known = meta2.get("total") or meta2.get("count") or total_known
                            print(f"[marketplace] page 1 (explicit body): {len(items2)} items")
                            if len(items2) < per_page:
                                break  # no more pages
                            continue
                if len(items0) < per_page:
                    break
                continue

            if data is None:
                print(f"[marketplace] page {page} returned None — stopping")
                break

            items, meta = _extract(data)
            if not items:
                break

            for m in items:
                self._index_market_miner(m, by_name_num, by_ext_id, by_ton_address, _re)
            print(f"[marketplace] page {page}: {len(items)} items (total indexed: {len(by_name_num)})")

            if total_known and len(by_name_num) >= total_known:
                break
            if len(items) < per_page:
                break

        if not by_name_num and not by_ext_id:
            return None

        print(f"[marketplace] done — {len(by_name_num)} miners indexed, {len(by_ton_address)} by TON addr")
        return {
            "by_name_num":    by_name_num,
            "by_ext_id":      by_ext_id,
            "by_ton_address": by_ton_address,
            "count":          len(by_name_num),
            "endpoint":       ENDPOINT,
        }

    @staticmethod
    def _index_market_miner(m: dict, name_idx: dict, ext_idx: dict, addr_idx: dict, re_mod) -> None:
        """Index a marketplace miner entry by name-number, externalUrlId, and wallet address."""
        name = m.get("name", "")
        match = re_mod.search(r"\d{3,7}", name)
        if match:
            name_idx[match.group(0)] = m
        ext = m.get("externalUrlId")
        if ext:
            ext_idx[str(ext)] = m
        wallet = m.get("wallet") or {}
        addr = wallet.get("address") if isinstance(wallet, dict) else None
        if addr:
            addr_idx[addr.lower()] = m

    def _uuid_from_ipfs(self, ipfs_url: str) -> str | None:
        """Fetch IPFS metadata and extract UUID from external_url field."""
        import re
        try:
            req = urllib.request.Request(ipfs_url)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            ext_url = data.get("external_url", "")
            m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ext_url)
            return m.group(0) if m else None
        except Exception:
            return None

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

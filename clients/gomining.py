"""
GoMining NFT app API client.
Fetches REAL miner power (including upgrades) from the GoMining backend.

Authentication:
  GoMining uses JWT tokens. You can get your token by:
  1. Opening https://nft.gomining.com in browser
  2. Login with your wallet (Tonkeeper / TonConnect)
  3. Open DevTools → Network → find any /api/ request → copy the Authorization header value

  Pass the token via --token argument or GOMINING_TOKEN env var.

  Without a token, real-power lookup is skipped and only blockchain/marketplace
  data is used (BASELINE_POWER from NFT attributes).
"""

import httpx
from typing import Optional
from config import GOMINING_API, REQUEST_TIMEOUT


class GoMiningClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token
        self._headers = {"Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            resp = httpx.get(
                f"{GOMINING_API}{path}",
                params=params,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            if resp.status_code == 401:
                raise PermissionError("GoMining token is invalid or expired.")
            resp.raise_for_status()
            return resp.json()
        except PermissionError:
            raise
        except Exception:
            return None

    def get_miner_by_index(self, index: int) -> Optional[dict]:
        """Get miner data by its sequential number (e.g. 349973)."""
        # Try common GoMining API patterns
        for path in (
            f"/miners/{index}",
            f"/nfts/{index}",
            f"/digital-miners/{index}",
        ):
            data = self._get(path)
            if data:
                return data
        return None

    def get_miner_by_address(self, ton_address: str) -> Optional[dict]:
        """Get miner data by its TON NFT contract address."""
        for path in (
            f"/miners/by-address/{ton_address}",
            f"/nfts/by-address/{ton_address}",
        ):
            data = self._get(path, params={"address": ton_address})
            if data:
                return data

        # Try query parameter approach
        for path in ("/miners", "/nfts"):
            data = self._get(path, params={"address": ton_address})
            if data:
                return data

        return None

    def get_user_miners(self) -> list[dict]:
        """Get all miners for the authenticated user."""
        if not self.token:
            return []
        for path in ("/user/miners", "/miners/my", "/user/nfts"):
            data = self._get(path)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("miners", "nfts", "items", "data"):
                    if isinstance(data.get(key), list):
                        return data[key]
        return []

    @staticmethod
    def parse_power(miner_data: dict) -> Optional[float]:
        """Extract real power (TH/s) from GoMining API response."""
        if not miner_data:
            return None
        # Try common field names
        for key in (
            "power", "hashrate", "computing_power", "computingPower",
            "currentPower", "current_power", "th", "ths",
            "total_power", "totalPower",
        ):
            val = miner_data.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        # Sometimes nested under "stats" or "attributes"
        for container in ("stats", "attributes", "data", "miner"):
            sub = miner_data.get(container)
            if isinstance(sub, dict):
                result = GoMiningClient.parse_power(sub)
                if result is not None:
                    return result

        return None

    @staticmethod
    def parse_efficiency(miner_data: dict) -> Optional[float]:
        """Extract efficiency (W/TH) from GoMining API response."""
        if not miner_data:
            return None
        for key in (
            "efficiency", "energy_efficiency", "energyEfficiency",
            "wth", "w_per_th", "wPerTh",
        ):
            val = miner_data.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        for container in ("stats", "attributes", "data", "miner"):
            sub = miner_data.get(container)
            if isinstance(sub, dict):
                result = GoMiningClient.parse_efficiency(sub)
                if result is not None:
                    return result

        return None

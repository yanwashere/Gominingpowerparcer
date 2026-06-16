"""Data models for GoMining NFT miner data."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MinerInfo:
    # Identification
    index: int                          # NFT index within collection (e.g. 349973)
    address: Optional[str] = None       # TON smart contract address of NFT item
    collection_name: Optional[str] = None
    name: Optional[str] = None          # e.g. "The Mine Box #349973"

    # Marketplace data (from getgems)
    price_ton: Optional[float] = None   # Sale price in TON
    price_usd: Optional[float] = None   # Sale price in USD (if available)
    is_on_sale: bool = False

    # Blockchain attributes (BASELINE = original NFT attribute, no upgrades)
    baseline_power_th: Optional[float] = None   # TH/s from NFT metadata
    baseline_efficiency_wth: Optional[float] = None  # W/TH from NFT metadata

    # GoMining app data (real values including upgrades)
    real_power_th: Optional[float] = None
    real_efficiency_wth: Optional[float] = None

    # Computed metrics
    @property
    def upgrade_ratio(self) -> Optional[float]:
        """How much power has been added via upgrades (ratio, e.g. 1.025 means +2.5%)."""
        if self.real_power_th and self.baseline_power_th and self.baseline_power_th > 0:
            return self.real_power_th / self.baseline_power_th
        return None

    @property
    def added_power_th(self) -> Optional[float]:
        """Extra TH/s added by upgrades."""
        if self.real_power_th and self.baseline_power_th:
            return self.real_power_th - self.baseline_power_th
        return None

    @property
    def th_per_ton(self) -> Optional[float]:
        """Real TH/s per TON — the main value metric (higher = better deal)."""
        power = self.real_power_th or self.baseline_power_th
        if power and self.price_ton and self.price_ton > 0:
            return power / self.price_ton
        return None

    @property
    def th_per_ton_baseline(self) -> Optional[float]:
        """Baseline TH/s per TON (what buyer appears to pay for)."""
        if self.baseline_power_th and self.price_ton and self.price_ton > 0:
            return self.baseline_power_th / self.price_ton
        return None

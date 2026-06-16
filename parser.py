"""
Core parser logic: fetches and combines data from getgems, tonapi, and GoMining.
"""

import sys
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import COLLECTIONS
from models import MinerInfo
from clients.getgems import (
    get_nft_item_by_index,
    get_all_collection_sales,
    parse_attributes,
    parse_price_ton,
)
from clients.tonapi import get_nft_item, get_nft_address_by_index, parse_nft_attributes
from clients.gomining import GoMiningClient


def _collection_for(index: int) -> Optional[tuple[str, str]]:
    """
    Guess the collection by index range.
    Release 1 has 2500 miners (indices 1-2500).
    Release 2 has 34936 miners (indices above that).
    Returns (name, address) or None if ambiguous.
    """
    items = list(COLLECTIONS.items())
    if len(items) == 2:
        name1, addr1 = items[0]  # GoMining Digital Miners (Release 1)
        name2, addr2 = items[1]  # GoMining Digital Miners: Release 2
        if index <= 2500:
            return name1, addr1
        else:
            return name2, addr2
    return None


def fetch_single_miner(
    index: int,
    collection_name: Optional[str] = None,
    gomining_client: Optional[GoMiningClient] = None,
    tonapi_key: Optional[str] = None,
    verbose: bool = False,
) -> MinerInfo:
    """Fetch all available data for a single miner by its sequential index."""

    info = MinerInfo(index=index)

    # Determine collection
    if collection_name and collection_name in COLLECTIONS:
        info.collection_name = collection_name
        collection_address = COLLECTIONS[collection_name]
    else:
        guess = _collection_for(index)
        if guess:
            info.collection_name, collection_address = guess
        else:
            # Default to Release 2
            info.collection_name = "GoMining Digital Miners: Release 2"
            collection_address = list(COLLECTIONS.values())[1]

    # 1. Try getgems GraphQL for NFT item data
    if verbose:
        print(f"  → Querying getgems for #{index}...")
    gg_item = get_nft_item_by_index(collection_address, index)

    if gg_item:
        info.address = gg_item.get("address")
        info.name = gg_item.get("name") or f"#{index}"
        info.is_on_sale = bool(gg_item.get("sale"))
        info.price_ton = parse_price_ton(gg_item)

        attrs = parse_attributes(gg_item.get("attributes", []))
        baseline = attrs.get("BASELINE_POWER") or attrs.get("POWER") or attrs.get("HASHRATE")
        if baseline is not None:
            try:
                info.baseline_power_th = float(baseline)
            except (ValueError, TypeError):
                pass
        eff = attrs.get("ENERGY_EFFICIENCY") or attrs.get("EFFICIENCY") or attrs.get("W/TH")
        if eff is not None:
            try:
                info.baseline_efficiency_wth = float(eff)
            except (ValueError, TypeError):
                pass

    # 2. If we still don't have an address, try tonapi to resolve it
    if not info.address and tonapi_key:
        if verbose:
            print(f"  → Resolving NFT address via tonapi for #{index}...")
        info.address = get_nft_address_by_index(collection_address, index, tonapi_key)

    # 3. Get tonapi NFT item data for additional metadata
    if info.address and (not info.baseline_power_th):
        if verbose:
            print(f"  → Getting on-chain metadata from tonapi for #{index}...")
        ton_item = get_nft_item(info.address, tonapi_key)
        if ton_item:
            if not info.name:
                info.name = ton_item.get("metadata", {}).get("name") or f"#{index}"
            ton_attrs = parse_nft_attributes(ton_item)
            baseline = (
                ton_attrs.get("BASELINE_POWER")
                or ton_attrs.get("POWER")
                or ton_attrs.get("HASHRATE")
            )
            if baseline is not None and not info.baseline_power_th:
                try:
                    info.baseline_power_th = float(baseline)
                except (ValueError, TypeError):
                    pass

    # Fallback name
    if not info.name:
        info.name = f"#{index}"

    # 4. Get real power from GoMining API
    if gomining_client:
        if verbose:
            print(f"  → Fetching real power from GoMining API for #{index}...")
        miner_data = None

        # Try by index first
        miner_data = gomining_client.get_miner_by_index(index)

        # Fallback: try by NFT address
        if not miner_data and info.address:
            miner_data = gomining_client.get_miner_by_address(info.address)

        if miner_data:
            real_power = GoMiningClient.parse_power(miner_data)
            if real_power is not None:
                info.real_power_th = real_power
            real_eff = GoMiningClient.parse_efficiency(miner_data)
            if real_eff is not None:
                info.real_efficiency_wth = real_eff

    return info


def fetch_miners_parallel(
    indices: list[int],
    collection_name: Optional[str] = None,
    gomining_client: Optional[GoMiningClient] = None,
    tonapi_key: Optional[str] = None,
    max_workers: int = 8,
    progress_callback=None,
) -> list[MinerInfo]:
    """Fetch data for multiple miners in parallel."""
    results: list[MinerInfo] = []
    errors: list[tuple[int, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(
                fetch_single_miner,
                idx,
                collection_name,
                gomining_client,
                tonapi_key,
            ): idx
            for idx in indices
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                miner = future.result()
                results.append(miner)
            except PermissionError as e:
                # Auth error — stop further GoMining API calls
                print(f"\n[Auth Error] {e}", file=sys.stderr)
                errors.append((idx, str(e)))
            except Exception as e:
                errors.append((idx, str(e)))
                results.append(MinerInfo(index=idx))

            if progress_callback:
                progress_callback(len(results) + len(errors), len(indices))

    results.sort(key=lambda m: m.index)
    return results


def fetch_collection_sales(
    collection_name: str,
    gomining_client: Optional[GoMiningClient] = None,
    max_items: int = 500,
    progress_callback=None,
) -> list[MinerInfo]:
    """Fetch all currently listed (on sale) miners from a collection."""
    if collection_name not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection_name!r}. "
                         f"Available: {list(COLLECTIONS.keys())}")

    collection_address = COLLECTIONS[collection_name]
    raw_items = get_all_collection_sales(collection_address, max_items=max_items)

    miners: list[MinerInfo] = []
    for i, item in enumerate(raw_items):
        index_raw = item.get("index")
        try:
            index = int(index_raw)
        except (TypeError, ValueError):
            continue

        info = MinerInfo(
            index=index,
            collection_name=collection_name,
            address=item.get("address"),
            name=item.get("name") or f"#{index}",
            is_on_sale=True,
            price_ton=parse_price_ton(item),
        )

        attrs = parse_attributes(item.get("attributes", []))
        baseline = attrs.get("BASELINE_POWER") or attrs.get("POWER") or attrs.get("HASHRATE")
        if baseline is not None:
            try:
                info.baseline_power_th = float(baseline)
            except (ValueError, TypeError):
                pass
        eff = attrs.get("ENERGY_EFFICIENCY") or attrs.get("EFFICIENCY") or attrs.get("W/TH")
        if eff is not None:
            try:
                info.baseline_efficiency_wth = float(eff)
            except (ValueError, TypeError):
                pass

        miners.append(info)

        if progress_callback:
            progress_callback(i + 1, len(raw_items))

    # Optionally enrich with real power from GoMining API
    if gomining_client and miners:
        print("\nFetching real power from GoMining API...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=8) as executor:
            def enrich(m: MinerInfo) -> MinerInfo:
                miner_data = gomining_client.get_miner_by_index(m.index)
                if not miner_data and m.address:
                    miner_data = gomining_client.get_miner_by_address(m.address)
                if miner_data:
                    rp = GoMiningClient.parse_power(miner_data)
                    if rp is not None:
                        m.real_power_th = rp
                    re = GoMiningClient.parse_efficiency(miner_data)
                    if re is not None:
                        m.real_efficiency_wth = re
                return m

            futures = {executor.submit(enrich, m): m for m in miners}
            enriched = []
            for j, fut in enumerate(as_completed(futures)):
                enriched.append(fut.result())
                if progress_callback:
                    progress_callback(j + 1, len(miners))
            miners = sorted(enriched, key=lambda m: m.index)

    return miners

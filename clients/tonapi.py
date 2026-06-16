"""
TON API client (tonapi.io v2).
Used to:
  - Resolve NFT item address from collection address + item index
  - Get NFT metadata (attributes, owner, etc.)
"""

import httpx
from typing import Optional
from config import TONAPI_BASE, REQUEST_TIMEOUT


def _get(path: str, params: dict = None, api_key: str = None) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = httpx.get(
        f"{TONAPI_BASE}{path}",
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_nft_item(address: str, api_key: str = None) -> Optional[dict]:
    """Get NFT item data by its TON address."""
    try:
        return _get(f"/nfts/{address}", api_key=api_key)
    except Exception:
        return None


def get_collection_items(
    collection_address: str,
    limit: int = 1000,
    offset: int = 0,
    api_key: str = None,
) -> list[dict]:
    """Get NFT items from a collection (paginated)."""
    try:
        data = _get(
            f"/nfts/collections/{collection_address}/items",
            params={"limit": min(limit, 1000), "offset": offset},
            api_key=api_key,
        )
        return data.get("nft_items", [])
    except Exception:
        return []


def get_nft_address_by_index(
    collection_address: str,
    index: int,
    api_key: str = None,
) -> Optional[str]:
    """
    Resolve NFT item address from collection + index.
    TON NFT standard: each item's address is deterministic from collection + index.
    Uses tonapi runMethod to call `get_nft_address_by_index` on the collection contract.
    """
    try:
        # tonapi v2: run get method on a contract
        data = _get(
            f"/blockchain/accounts/{collection_address}/methods/get_nft_address_by_index",
            params={"args": str(index)},
            api_key=api_key,
        )
        # Response: {"stack": [{"type": "cell" | "slice", "cell": "..."}]}
        stack = data.get("stack", [])
        if stack:
            # The first stack item is the NFT address as a slice/cell
            item = stack[0]
            # tonapi returns decoded address
            if "address" in item:
                return item["address"]
            # Or it may be in the 'value' field
            val = item.get("value")
            if val:
                return val
    except Exception:
        pass
    return None


def parse_nft_attributes(item: dict) -> dict:
    """Extract attributes from tonapi NFT item response."""
    metadata = item.get("metadata", {})
    attrs = {}

    # Attributes array: [{trait_type, value}]
    for attr in metadata.get("attributes", []):
        key = str(attr.get("trait_type", "")).upper().replace(" ", "_")
        attrs[key] = attr.get("value")

    # Also check direct fields
    for field in ("name", "description", "image"):
        if field in metadata:
            attrs[field.upper()] = metadata[field]

    return attrs

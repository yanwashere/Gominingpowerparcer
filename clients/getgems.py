"""
Getgems GraphQL API client.
Provides marketplace data: prices, sale listings, NFT attributes.
"""

import httpx
from typing import Optional, Any
from config import GETGEMS_GRAPHQL, REQUEST_TIMEOUT


def _post(query: str, variables: dict) -> dict:
    resp = httpx.post(
        GETGEMS_GRAPHQL,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data.get("data", {})


# ── NFT item by address ─────────────────────────────────────────────────────

_ITEM_BY_ADDRESS_QUERY = """
query NftItem($address: String!) {
  nftItem(address: $address) {
    address
    index
    name
    collection { address name }
    attributes { key value }
    sale {
      ... on NftSaleFix {
        fullPrice
        seller { address }
      }
    }
  }
}
"""

def get_nft_item_by_address(address: str) -> Optional[dict]:
    try:
        data = _post(_ITEM_BY_ADDRESS_QUERY, {"address": address})
        return data.get("nftItem")
    except Exception:
        return None


# ── NFT item by collection + index ──────────────────────────────────────────

_ITEM_BY_INDEX_QUERY = """
query NftItemByIndex($collectionAddress: String!, $index: String!) {
  alphaNftItemByIndex(collectionAddress: $collectionAddress, itemIndex: $index) {
    address
    index
    name
    collection { address name }
    attributes { key value }
    sale {
      ... on NftSaleFix {
        fullPrice
        seller { address }
      }
    }
  }
}
"""

def get_nft_item_by_index(collection_address: str, index: int) -> Optional[dict]:
    try:
        data = _post(_ITEM_BY_INDEX_QUERY, {
            "collectionAddress": collection_address,
            "index": str(index),
        })
        # Try different field names
        item = data.get("alphaNftItemByIndex") or data.get("nftItemByIndex")
        return item
    except Exception:
        return None


# ── Sale listings for a collection ──────────────────────────────────────────

_SALES_QUERY = """
query NftSales($collectionAddress: String!, $count: Int!, $cursor: String) {
  nftSearch(
    collections: [$collectionAddress]
    count: $count
    cursor: $cursor
    filters: { saleType: FIX_PRICE }
    sort: { field: PRICE, asc: true }
  ) {
    cursor
    items {
      ... on NftItem {
        address
        index
        name
        attributes { key value }
        sale {
          ... on NftSaleFix {
            fullPrice
            seller { address }
          }
        }
      }
    }
  }
}
"""

def get_collection_sales(
    collection_address: str,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """Return (items, next_cursor). Items are NFTs currently on sale."""
    try:
        data = _post(_SALES_QUERY, {
            "collectionAddress": collection_address,
            "count": min(limit, 500),
            "cursor": cursor,
        })
        result = data.get("nftSearch", {})
        return result.get("items", []), result.get("cursor")
    except Exception:
        return [], None


def get_all_collection_sales(collection_address: str, max_items: int = 2000) -> list[dict]:
    """Paginate through all sale listings for a collection."""
    all_items: list[dict] = []
    cursor: Optional[str] = None

    while len(all_items) < max_items:
        batch_size = min(200, max_items - len(all_items))
        items, cursor = get_collection_sales(collection_address, batch_size, cursor)
        all_items.extend(items)
        if not cursor or not items:
            break

    return all_items


# ── Parse helpers ────────────────────────────────────────────────────────────

def parse_attributes(attributes: list[dict]) -> dict[str, Any]:
    """Convert [{key, value}] → {key: value} normalised dict."""
    result = {}
    for attr in attributes or []:
        key = attr.get("key", "").upper().replace(" ", "_")
        result[key] = attr.get("value")
    return result


def parse_price_ton(item: dict) -> Optional[float]:
    """Extract sale price in TON from a getgems NFT item dict."""
    from config import NANOTON
    sale = item.get("sale") or {}
    full_price = sale.get("fullPrice")
    if full_price is not None:
        try:
            return int(full_price) / NANOTON
        except (ValueError, TypeError):
            pass
    return None

"""
GoMining NFT Parser — collection addresses and API endpoints.
"""

# TON collection contract addresses (user-friendly format)
COLLECTIONS = {
    "GoMining Digital Miners": "EQBY-QwusK_kNxUy2F7LpPa7-e8XKEskLedb6fXzJ6n9JjWD",
    "GoMining Digital Miners: Release 2": "EQBykWvdmoyFD2kB4BJbdrxRqyWKzSLDn-SgwNvznRfbsaKv",
}

# API base URLs
GETGEMS_GRAPHQL = "https://api.getgems.io/graphql"
TONAPI_BASE = "https://tonapi.io/v2"
GOMINING_API = "https://nft.gomining.com/api"

# TON: 1 TON = 10^9 nanotons
NANOTON = 1_000_000_000

# Default request timeout (seconds)
REQUEST_TIMEOUT = 30

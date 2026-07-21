"""X Layer JSON-RPC helpers for USDT0 Transfer verification.

Uses ``httpx.Client`` with an explicit per-call timeout — respx mocks httpx (not
requests), and oversized/hung ``eth_getLogs`` calls HANG rather than error on
rpc.xlayer.tech, so every call is time-boxed. All functions are sync so the
sweeper can run a tick under ``asyncio.to_thread`` without blocking the loop.
"""

from __future__ import annotations

import httpx

from app import config


class ChainError(RuntimeError):
    """A JSON-RPC endpoint returned an ``error`` object."""


def _rpc(method: str, params: list, timeout: float | None = None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    with httpx.Client(timeout=timeout or config.RPC_TIMEOUT, trust_env=False) as client:
        resp = client.post(config.RPC_URL, json=body)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ChainError(f"{method}: {data['error']}")
    return data.get("result")


def block_number(timeout: float | None = None) -> int:
    return int(_rpc("eth_blockNumber", [], timeout), 16)


def pad_address(addr: str) -> str:
    """Left-pad a 20-byte address to a 32-byte log topic."""
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def get_logs(
    from_block: int, to_block: int, addresses: list[str], timeout: float | None = None
) -> list[dict]:
    """USDT0 Transfer logs whose `to` topic is any of `addresses`, in the given
    inclusive block window."""
    params = [
        {
            "address": config.USDT0,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": [
                config.TRANSFER_TOPIC,
                None,
                [pad_address(a) for a in addresses],
            ],
        }
    ]
    return _rpc("eth_getLogs", params, timeout) or []


def get_transaction_receipt(tx_hash: str, timeout: float | None = None) -> dict | None:
    return _rpc("eth_getTransactionReceipt", [tx_hash], timeout)


def decode_transfer_log(log: dict) -> dict:
    """Decode one ERC-20 Transfer log into address/value/locator fields."""
    topics = log["topics"]
    return {
        "from": "0x" + topics[1][-40:].lower(),
        "to": "0x" + topics[2][-40:].lower(),
        "value": int(log["data"], 16),
        "tx_hash": log["transactionHash"].lower(),
        "log_index": int(log["logIndex"], 16),
        "block_number": int(log["blockNumber"], 16),
    }

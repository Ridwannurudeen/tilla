"""X Layer JSON-RPC helpers for USDT0 Transfer verification.

Uses ``httpx.Client`` with an explicit per-call timeout — respx mocks httpx (not
requests), and oversized/hung ``eth_getLogs`` calls HANG rather than error on
rpc.xlayer.tech, so every call is time-boxed. All functions are sync so the
sweeper can run a tick under ``asyncio.to_thread`` without blocking the loop.
"""

from __future__ import annotations

import threading

import httpx

from app import config
from app.payment import ChainConfig


class ChainError(RuntimeError):
    """A JSON-RPC endpoint returned an ``error`` object."""


class ChainBusy(ChainError):
    """The RPC concurrency cap is saturated; the call failed fast (<=
    RPC_ACQUIRE_TIMEOUT) instead of piling another slow request onto the anyio
    threadpool. A ChainError subclass so every existing handler (submit_txhash ->
    502, refresh_order -> serve DB state, the sweeper -> retry next tick) already
    covers it."""


# Bound how many threads may be inside an RPC call at once so a slow endpoint can't
# pin the whole request threadpool; excess callers wait RPC_ACQUIRE_TIMEOUT then
# raise ChainBusy. Module-level so it is shared across every caller in the process.
_RPC_SEMAPHORE = threading.BoundedSemaphore(config.RPC_MAX_CONCURRENT)


def _rpc(cfg: ChainConfig, method: str, params: list, timeout: float | None = None):
    if not _RPC_SEMAPHORE.acquire(timeout=config.RPC_ACQUIRE_TIMEOUT):
        raise ChainBusy(f"{method}: RPC concurrency cap reached")
    try:
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        with httpx.Client(
            timeout=timeout or config.RPC_TIMEOUT, trust_env=False
        ) as client:
            resp = client.post(cfg.rpc_url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise ChainError(f"{method}: {data['error']}")
        return data.get("result")
    finally:
        _RPC_SEMAPHORE.release()


def block_number(cfg: ChainConfig, timeout: float | None = None) -> int:
    return int(_rpc(cfg, "eth_blockNumber", [], timeout), 16)


def eth_call(
    cfg: ChainConfig, to: str, data: str, timeout: float | None = None
) -> str | None:
    """Read-only contract call against the latest block of ``cfg``'s chain.
    Returns the raw hex result (or None). Time-boxed + sema-capped like every
    other RPC (ChainBusy on cap)."""
    return _rpc(cfg, "eth_call", [{"to": to, "data": data}, "latest"], timeout)


def pad_address(addr: str) -> str:
    """Left-pad a 20-byte address to a 32-byte log topic."""
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def get_logs(
    cfg: ChainConfig,
    from_block: int,
    to_block: int,
    addresses: list[str],
    timeout: float | None = None,
) -> list[dict]:
    """`cfg.asset` Transfer logs whose `to` topic is any of `addresses`, in the
    given inclusive block window."""
    params = [
        {
            "address": cfg.asset,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": [
                config.TRANSFER_TOPIC,
                None,
                [pad_address(a) for a in addresses],
            ],
        }
    ]
    return _rpc(cfg, "eth_getLogs", params, timeout) or []


def get_transaction_receipt(
    cfg: ChainConfig, tx_hash: str, timeout: float | None = None
) -> dict | None:
    return _rpc(cfg, "eth_getTransactionReceipt", [tx_hash], timeout)


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

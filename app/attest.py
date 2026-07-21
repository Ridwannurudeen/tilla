"""M11 on-chain depth: the EAS receipt attester — DORMANT by default.

For every terminal-delivered order the worker issues one EAS attestation (recipient
= the buyer's wallet) recording ``(buyer, storeId, amountUsdt6, paymentTxHash)`` on
X Layer, giving the buyer an on-chain, revocable receipt. Modeled on the two proven
in-repo precedents:

  - ``warden_hire`` — a dormant signing component: flag + key + pinned expectations,
    refuses before it ever signs. Here three independent, default-closed gates keep
    the attester inert (``ATTEST_ENABLED`` default off, ``TILLA_ATTESTER_KEY`` absent,
    the lifespan task refuses to start without both). Flag off OR key unset => zero
    on-chain tx, zero RPC, zero gas, and ``web3``/``eth_account`` are never imported
    (they load LAZILY inside ``_Attester``, the ``app.mpp`` lazy-SDK precedent).
  - ``webhooks`` — an outbox drained by a lifespan background loop. Orders are queued
    (``attest_status='pending'``) as a pure DB write in the SAME transaction as the
    delivered transition (in ``checkout.deliver`` for web orders, in
    ``agentic.record_settlement`` on the settling->delivered flip for agent orders),
    and ``attest_loop`` drains them off the buyer-critical path.

IDEMPOTENCY (one attestation per order, never double-attest): a three-phase, race-proof
transition (the M3 pattern) with a crash-safe send window. The attest tx is BUILT and
SIGNED first (yielding its hash + nonce), then the order is claimed
``pending -> sending`` recording ``attest_tx`` + ``attest_nonce`` BEFORE the tx is
broadcast, then broadcast, then flipped ``sending -> sent`` and finally
``sent -> attested`` with the parsed UID. A crash BETWEEN the broadcast and the DB claim
can therefore only ever leave a ``sending`` row (never lose the broadcast intent), so a
restart RECONCILES it against the chain instead of blind-re-broadcasting: found on chain
=> ``attested``; nonce consumed by a different mined tx => our attestation is definitively
not on chain, re-attest fresh; nonce still free => re-broadcast the SAME-nonce tx
(idempotent — at most one tx per nonce can ever mine). ``sent`` rows reconcile by fetching
the attest tx receipt only — they never re-send. Because every transition is a conditional
UPDATE, only one caller can ever attest a given order. This is GAS safety (avoid a wasted
duplicate OKB attest tx), NOT fund loss.

FAIL-SAFE: any misconfiguration (wrong chain, missing key, gas over cap, NULL
buyer/payment tx, RPC error) refuses/idles and never signs; a bad tick is logged and
the loop continues, exactly like the sweeper and webhook loops.
"""

from __future__ import annotations

import asyncio
import logging

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address
from sqlalchemy import select, update

from app import checkout, config
from app.db import SessionLocal
from app.models import Order, Store, log_event

logger = logging.getLogger("tilla")

# The schema's resolver + revocable flag. Together with config.ATTEST_SCHEMA these
# define the schema UID (keccak of schema+resolver+revocable) — protocol constants,
# not app config. revocable=True preserves a takedown lever on a published receipt.
SCHEMA_RESOLVER = "0x0000000000000000000000000000000000000000"
SCHEMA_REVOCABLE = True

# event Attested(address indexed recipient, address indexed attester,
#                bytes32 uid, bytes32 indexed schemaUID) — uid is the FIRST 32 bytes
# of the (non-indexed) log data.
ATTESTED_TOPIC0 = keccak(text="Attested(address,address,bytes32,bytes32)")

# How many ticks a 'sent' row may sit with an un-findable receipt before the reconcile
# gives up and marks it 'failed' (process-local; resets on restart, which is fine —
# every boot grants a fresh few attempts). A found receipt resolves immediately.
RECONCILE_MAX_TICKS = 3

SCHEMA_REGISTRY_ABI = [
    {
        "name": "register",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "schema", "type": "string"},
            {"name": "resolver", "type": "address"},
            {"name": "revocable", "type": "bool"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "getSchema",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "uid", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "uid", "type": "bytes32"},
                    {"name": "resolver", "type": "address"},
                    {"name": "revocable", "type": "bool"},
                    {"name": "schema", "type": "string"},
                ],
            }
        ],
    },
]

EAS_ABI = [
    {
        "name": "attest",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "request",
                "type": "tuple",
                "components": [
                    {"name": "schema", "type": "bytes32"},
                    {
                        "name": "data",
                        "type": "tuple",
                        "components": [
                            {"name": "recipient", "type": "address"},
                            {"name": "expirationTime", "type": "uint64"},
                            {"name": "revocable", "type": "bool"},
                            {"name": "refUID", "type": "bytes32"},
                            {"name": "data", "type": "bytes"},
                            {"name": "value", "type": "uint256"},
                        ],
                    },
                ],
            }
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "getAttestation",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "uid", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "uid", "type": "bytes32"},
                    {"name": "schema", "type": "bytes32"},
                    {"name": "time", "type": "uint64"},
                    {"name": "expirationTime", "type": "uint64"},
                    {"name": "revocationTime", "type": "uint64"},
                    {"name": "refUID", "type": "bytes32"},
                    {"name": "recipient", "type": "address"},
                    {"name": "attester", "type": "address"},
                    {"name": "revocable", "type": "bool"},
                    {"name": "data", "type": "bytes"},
                ],
            }
        ],
    },
]


class _GasTooHigh(RuntimeError):
    """The estimated gas cost exceeds TILLA_ATTEST_MAX_GAS_WEI — refuse, don't sign."""


# ---------------------------------------------------------------- pure helpers
def schema_uid() -> bytes:
    """The EAS schema UID = keccak256(abi.encodePacked(schema, resolver, revocable)).
    Pure (no web3), so the golden test pins it against the Spike-4 precompute."""
    packed = (
        config.ATTEST_SCHEMA.encode()
        + bytes.fromhex(SCHEMA_RESOLVER[2:])
        + (b"\x01" if SCHEMA_REVOCABLE else b"\x00")
    )
    return keccak(packed)


def build_attestation_data(
    from_addr: str, slug: str, expected_micro: int, tx_hash: str
) -> bytes:
    """abi.encode the receipt blob in the schema's field order (load-bearing):
    (address buyer, string storeId, uint256 amountUsdt6, bytes32 paymentTxHash)."""
    return abi_encode(
        ["address", "string", "uint256", "bytes32"],
        [
            to_checksum_address(from_addr),
            slug,
            int(expected_micro),
            bytes.fromhex(tx_hash[2:] if tx_hash.startswith("0x") else tx_hash),
        ],
    )


def decode_attestation_data(data: bytes) -> tuple[str, str, int, bytes]:
    """Inverse of :func:`build_attestation_data` (used by the golden round-trip test
    and the runbook read-back)."""
    return abi_decode(["address", "string", "uint256", "bytes32"], data)


def _attest_request(
    from_addr: str, slug: str, expected_micro: int, tx_hash: str
) -> dict:
    """The EAS attest() request tuple for one receipt. recipient = the buyer wallet.
    Takes primitives (not the ORM row) so it is called after the read session closes,
    with no detached-instance risk."""
    return {
        "schema": schema_uid(),
        "data": {
            "recipient": to_checksum_address(from_addr),
            "expirationTime": 0,
            "revocable": SCHEMA_REVOCABLE,
            "refUID": b"\x00" * 32,
            "data": build_attestation_data(from_addr, slug, expected_micro, tx_hash),
            "value": 0,
        },
    }


def _uid_from_receipt(receipt) -> str | None:
    """The EAS UID = first 32 bytes of the Attested log's data, or None if absent."""
    eas_addr = config.EAS_ADDR.lower()
    for log in receipt["logs"]:
        topics = log["topics"]
        if not topics:
            continue
        if (
            str(log["address"]).lower() == eas_addr
            and bytes(topics[0]) == ATTESTED_TOPIC0
        ):
            return "0x" + bytes(log["data"])[:32].hex()
    return None


# --------------------------------------------------- lazy web3 signer (enabled only)
class _Attester:
    """The Web3 client + signing account + EAS/registry contract handles. Constructed
    ONLY in the enabled path (flag + key), so ``web3`` and ``eth_account`` import
    lazily here and never load on a dormant boot (asserted by the dormancy test)."""

    def __init__(self) -> None:
        from eth_account import Account
        from web3 import Web3

        self.w3 = Web3(
            Web3.HTTPProvider(
                config.ATTEST_RPC, request_kwargs={"timeout": config.RPC_TIMEOUT}
            )
        )
        self.account = Account.from_key(config.TILLA_ATTESTER_KEY)
        self.eas = self.w3.eth.contract(address=config.EAS_ADDR, abi=EAS_ABI)
        self.registry = self.w3.eth.contract(
            address=config.SCHEMA_REGISTRY_ADDR, abi=SCHEMA_REGISTRY_ABI
        )


def _default_attester_factory() -> _Attester | None:
    """A live attester iff BOTH the flag and the key are set, else None (dormant).
    Indirection so tests inject a mock; the real path is never touched by the suite."""
    if not (config.ATTEST_ENABLED and config.TILLA_ATTESTER_KEY):
        return None
    return _Attester()


# Tests replace this with a factory returning a mock attester; the SDK/web3 stay
# unimported, nothing is broadcast, no funds move.
_attester_factory = _default_attester_factory

# In-process caches (reset by :func:`_reset_state` in tests).
_schema_registered = False
_reconcile_seen: dict[str, int] = {}


def _reset_state() -> None:
    global _schema_registered
    _schema_registered = False
    _reconcile_seen.clear()


# ------------------------------------------------------------ on-chain seams
def _chain_id(attester: _Attester) -> int:
    return attester.w3.eth.chain_id


def _schema_is_registered(attester: _Attester) -> bool:
    """True iff the receipt schema already exists on the SchemaRegistry."""
    record = attester.registry.functions.getSchema(schema_uid()).call()
    return bool(record) and record[0] != b"\x00" * 32


def _hex(tx_hash) -> str:
    return (
        tx_hash.to_0x_hex()
        if hasattr(tx_hash, "to_0x_hex")
        else "0x" + bytes(tx_hash).hex()
    )


def _prepare_signed(attester: _Attester, fn, nonce=None):
    """Estimate gas, REFUSE over the cap, then build + SIGN ``fn`` WITHOUT broadcasting.
    Returns ``(signed_tx, tx_hash, nonce)``. Passing an explicit ``nonce`` pins it (a
    reconcile re-broadcast of the same order); otherwise the account's next free nonce is
    used. The nonce is the dedup key — at most one tx per nonce can mine — so recording it
    before broadcast makes the send window crash-safe."""
    w3, account = attester.w3, attester.account
    gas_estimate = fn.estimate_gas({"from": account.address, "value": 0})
    gas_price = max(w3.eth.gas_price, w3.to_wei(1, "gwei"))
    gas_cost = int(gas_estimate) * int(gas_price)
    if gas_cost > config.TILLA_ATTEST_MAX_GAS_WEI:
        raise _GasTooHigh(gas_cost)
    if nonce is None:
        nonce = w3.eth.get_transaction_count(account.address)
    tx = fn.build_transaction(
        {
            "from": account.address,
            "value": 0,
            "nonce": nonce,
            "chainId": config.ATTEST_CHAIN_ID,
            "gas": int(int(gas_estimate) * 1.3),
            "gasPrice": int(gas_price),
        }
    )
    signed = account.sign_transaction(tx)
    return signed, _hex(signed.hash), nonce


def _broadcast(attester: _Attester, signed) -> str:
    """Broadcast a pre-signed tx. Returns its hash (equal to the prepared hash)."""
    return _hex(attester.w3.eth.send_raw_transaction(signed.raw_transaction))


def _send_signed(attester: _Attester, fn) -> str:
    """Prepare + broadcast ``fn`` in one shot (schema register). Signs at most once."""
    signed, _tx_hash, _nonce = _prepare_signed(attester, fn)
    return _broadcast(attester, signed)


def _register_schema(attester: _Attester) -> str:
    fn = attester.registry.functions.register(
        config.ATTEST_SCHEMA, SCHEMA_RESOLVER, SCHEMA_REVOCABLE
    )
    return _send_signed(attester, fn)


def _prepare_attestation(
    attester: _Attester,
    from_addr: str,
    slug: str,
    expected_micro: int,
    tx_hash: str,
    nonce=None,
):
    """Build + sign one attestation tx WITHOUT broadcasting. Returns
    ``(signed_tx, tx_hash, nonce)`` — the caller records the nonce, then broadcasts."""
    fn = attester.eas.functions.attest(
        _attest_request(from_addr, slug, expected_micro, tx_hash)
    )
    return _prepare_signed(attester, fn, nonce=nonce)


def _wait_receipt(attester: _Attester, tx_hash: str):
    """Time-boxed receipt wait (X Layer RPC hangs rather than errors — the chain.py
    lesson). Returns the receipt, or None on timeout/error (reconciled on a later
    tick), never re-sending."""
    try:
        return attester.w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=config.RPC_TIMEOUT
        )
    except Exception:
        logger.warning(
            "attest: receipt wait timed out for %s (will reconcile)", tx_hash
        )
        return None


def _get_receipt(attester: _Attester, tx_hash: str):
    """One-shot receipt fetch for reconcile. Returns the receipt, or None if the tx
    is not yet mined / not found."""
    try:
        return attester.w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return None


def _confirmed_nonce(attester: _Attester) -> int | None:
    """The attester account's mined (latest-block) transaction count = the next free
    nonce. Used for nonce-based dedup on reconcile. None on RPC error (retry later)."""
    try:
        return int(attester.w3.eth.get_transaction_count(attester.account.address))
    except Exception:
        return None


def _receipt_status(receipt) -> int:
    return int(receipt["status"])


# ------------------------------------------------------------- worker tick
def _ensure_schema(attester: _Attester) -> bool:
    """Register the receipt schema once (cached in-process). Enabling the attester IS
    the user's approval for this one-time gas. Returns True iff the schema is present
    and usable; False (idle this tick) on any error or a gas-over-cap refusal."""
    global _schema_registered
    if _schema_registered:
        return True
    try:
        if _schema_is_registered(attester):
            _schema_registered = True
            return True
        tx = _register_schema(attester)
        receipt = _wait_receipt(attester, tx)
        if receipt is not None and _receipt_status(receipt) == 1:
            _schema_registered = True
            logger.info("attest: receipt schema registered (%s)", tx)
            return True
        logger.warning("attest: schema register did not confirm (%s)", tx)
        return False
    except _GasTooHigh as exc:
        logger.warning("attest: schema register refused, gas %s over cap", exc)
        return False
    except Exception:
        logger.exception("attest: schema ensure failed")
        return False


def _claim(order_id: str, from_statuses, **values) -> bool:
    """Race-proof conditional UPDATE on attest_status. Returns True iff exactly one
    row moved — the M3 transition pattern, applied to the attestation lifecycle."""
    with SessionLocal() as session:
        moved = session.execute(
            update(Order)
            .where(Order.id == order_id, Order.attest_status.in_(tuple(from_statuses)))
            .values(**values)
        ).rowcount
        session.commit()
        return moved == 1


def _log(order_id: str, store_id: int | None, event: str, data=None) -> None:
    with SessionLocal() as session:
        log_event(
            session, "attest", event, store_id=store_id, order_id=order_id, data=data
        )
        session.commit()


def _pending_orders() -> list[tuple[str, int]]:
    """(order_id, store_id) for up to MAX_PER_TICK terminal orders awaiting
    attestation, oldest paid first. _attest_one re-reads each row inside its own
    session (the pre-send status re-check), so no other fields are carried here."""
    with SessionLocal() as session:
        rows = session.execute(
            select(Order.id, Order.store_id)
            .where(
                Order.attest_status == "pending",
                Order.status.in_(checkout.TERMINAL_DELIVERED),
            )
            .order_by(Order.paid_at)
            .limit(config.ATTEST_MAX_PER_TICK)
        ).all()
        return [(r[0], r[1]) for r in rows]


def _sent_orders() -> list[tuple[str, int, str | None]]:
    with SessionLocal() as session:
        rows = session.execute(
            select(Order.id, Order.store_id, Order.attest_tx).where(
                Order.attest_status == "sent"
            )
        ).all()
        return [(r[0], r[1], r[2]) for r in rows]


def _sending_orders() -> list[tuple[str, int]]:
    """(order_id, store_id) for rows left mid-broadcast. _reconcile_sending_one re-reads
    each row's broadcast intent inside its own session."""
    with SessionLocal() as session:
        rows = session.execute(
            select(Order.id, Order.store_id).where(Order.attest_status == "sending")
        ).all()
        return [(r[0], r[1]) for r in rows]


def _attest_one(attester: _Attester, order_id: str, store_id: int) -> None:
    """Build+sign, claim pending->sending (recording the nonce), broadcast, then
    sending->sent + resolve one pending order. A missing buyer/payment tx fails the row
    (can't attest a receipt without them); a gas-over-cap refusal leaves it pending for a
    calmer window. The nonce is recorded BEFORE the broadcast, so a crash in the send
    window leaves a reconcilable 'sending' row — never a blind re-broadcast."""
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        store = session.get(Store, store_id) if order is not None else None
        if order is None or store is None or order.attest_status != "pending":
            return
        if not order.from_addr or not order.tx_hash:
            _claim(order_id, ("pending",), attest_status="failed")
            _log(
                order_id,
                store_id,
                "attest.failed",
                {"reason": "missing buyer or payment tx"},
            )
            return
        # Capture primitives inside the session — the send happens after it closes.
        from_addr, slug = order.from_addr, store.slug
        expected_micro, tx_hash = order.expected_micro, order.tx_hash

    # Build + sign BEFORE any broadcast (gas-over-cap refuses here, leaving it pending).
    try:
        signed, tx, nonce = _prepare_attestation(
            attester, from_addr, slug, expected_micro, tx_hash
        )
    except _GasTooHigh as exc:
        logger.warning("attest: order %s refused, gas %s over cap", order_id, exc)
        return
    except Exception:
        logger.exception("attest: prepare failed for %s (stays pending)", order_id)
        return

    # Claim pending->sending recording the broadcast intent (tx hash + nonce) BEFORE
    # broadcasting, so a crash in the send window leaves a reconcilable 'sending' row.
    if not _claim(
        order_id,
        ("pending",),
        attest_status="sending",
        attest_tx=tx,
        attest_nonce=nonce,
    ):
        logger.warning(
            "attest: lost pending->sending claim for %s (already handled)", order_id
        )
        return
    _log(order_id, store_id, "attest.sending", {"tx": tx, "nonce": nonce})

    try:
        _broadcast(attester, signed)
    except Exception:
        # Broadcast failed/uncertain — leave it 'sending'; reconcile checks the chain by
        # nonce before ever re-broadcasting (never blind, never double).
        logger.exception("attest: broadcast failed for %s (will reconcile)", order_id)
        return

    if not _claim(order_id, ("sending",), attest_status="sent"):
        return
    _log(order_id, store_id, "attest.sent", {"tx": tx})

    receipt = _wait_receipt(attester, tx)
    if receipt is None:
        return  # reconcile on a later tick
    _resolve_receipt(order_id, store_id, receipt)


def _resolve_receipt(order_id: str, store_id: int, receipt) -> None:
    """Drive a broadcast ('sending'/'sent') order to attested (status 1 + UID) or failed
    (status 0). Accepts both states so a 'sending' row whose tx is already mined resolves
    directly on reconcile."""
    from_statuses = ("sending", "sent")
    if _receipt_status(receipt) != 1:
        _claim(order_id, from_statuses, attest_status="failed")
        _log(order_id, store_id, "attest.failed", {"reason": "attestation tx reverted"})
        return
    uid = _uid_from_receipt(receipt)
    if uid is None:
        _claim(order_id, from_statuses, attest_status="failed")
        _log(
            order_id,
            store_id,
            "attest.failed",
            {"reason": "no Attested log in receipt"},
        )
        return
    if _claim(order_id, from_statuses, attest_status="attested", attestation_uid=uid):
        _reconcile_seen.pop(order_id, None)
        _log(order_id, store_id, "attest.attested", {"uid": uid})


def _reconcile_sending_one(attester: _Attester, order_id: str, store_id: int) -> None:
    """Resolve one row left 'sending' by a crash between the pending->sending claim and
    the broadcast-confirmed sending->sent flip. Nonce-based dedup:
      - attest tx found on chain => resolve (attested / failed);
      - nonce consumed by a DIFFERENT mined tx => our attestation is definitively not on
        chain and can never mine at this nonce => re-attest fresh (back to pending);
      - nonce still free (tx pending in mempool or never broadcast) => re-broadcast the
        SAME-nonce tx (idempotent: at most one tx per nonce mines) and advance to sent.
    """
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        store = session.get(Store, store_id) if order is not None else None
        if order is None or order.attest_status != "sending":
            return
        attest_tx, attest_nonce = order.attest_tx, order.attest_nonce
        from_addr, tx_hash = order.from_addr, order.tx_hash
        expected_micro = order.expected_micro
        slug = store.slug if store is not None else None
    if (
        not attest_tx
        or attest_nonce is None
        or slug is None
        or not from_addr
        or not tx_hash
    ):
        _claim(order_id, ("sending",), attest_status="failed")
        _log(
            order_id,
            store_id,
            "attest.failed",
            {"reason": "sending row missing broadcast intent"},
        )
        return

    receipt = _get_receipt(attester, attest_tx)
    if receipt is not None:
        _resolve_receipt(order_id, store_id, receipt)
        return

    confirmed = _confirmed_nonce(attester)
    if confirmed is None:
        return  # RPC hiccup — retry on a later tick
    if confirmed > attest_nonce:
        # The nonce slot was filled by a different mined tx (our hash is not on chain).
        # Our attestation is definitively not on chain — re-attest fresh (new nonce).
        _reconcile_seen.pop(order_id, None)
        if _claim(
            order_id,
            ("sending",),
            attest_status="pending",
            attest_tx=None,
            attest_nonce=None,
        ):
            _log(
                order_id,
                store_id,
                "attest.reattest",
                {"reason": "nonce consumed by other tx", "nonce": attest_nonce},
            )
        return

    # Nonce still free — re-broadcast pinned to the SAME nonce (dedup: one tx per nonce
    # can ever mine) and keep watching the (possibly new) hash.
    try:
        signed, new_tx, _ = _prepare_attestation(
            attester, from_addr, slug, expected_micro, tx_hash, nonce=attest_nonce
        )
        _broadcast(attester, signed)
    except _GasTooHigh as exc:
        logger.warning(
            "attest: reconcile rebroadcast of %s refused, gas %s over cap",
            order_id,
            exc,
        )
        return
    except Exception:
        logger.exception(
            "attest: reconcile rebroadcast of %s failed (stays sending)", order_id
        )
        return
    if _claim(order_id, ("sending",), attest_status="sent", attest_tx=new_tx):
        _log(
            order_id,
            store_id,
            "attest.sent",
            {"tx": new_tx, "nonce": attest_nonce, "rebroadcast": True},
        )


def _reconcile_sending(attester: _Attester) -> None:
    """Restart safety for the send window: resolve every row left 'sending' by a crash
    between the broadcast and the sending->sent claim (see _reconcile_sending_one)."""
    for order_id, store_id in _sending_orders():
        _reconcile_sending_one(attester, order_id, store_id)


def _reconcile_sent(attester: _Attester) -> None:
    """Restart safety: resolve rows left 'sent' by a crash between the send and the
    attested-commit. NEVER re-sends — fetches the attest tx receipt only. A not-found
    receipt is retried for a bounded number of ticks, then failed."""
    for order_id, store_id, attest_tx in _sent_orders():
        if not attest_tx:
            _claim(order_id, ("sent",), attest_status="failed")
            _log(
                order_id,
                store_id,
                "attest.failed",
                {"reason": "sent without attest_tx"},
            )
            continue
        receipt = _get_receipt(attester, attest_tx)
        if receipt is None:
            seen = _reconcile_seen.get(order_id, 0) + 1
            _reconcile_seen[order_id] = seen
            if seen >= RECONCILE_MAX_TICKS:
                _reconcile_seen.pop(order_id, None)
                _claim(order_id, ("sent",), attest_status="failed")
                _log(
                    order_id,
                    store_id,
                    "attest.failed",
                    {"reason": "attest tx receipt not found"},
                )
            continue
        _resolve_receipt(order_id, store_id, receipt)


def attest_tick() -> int:
    """One attestation pass. Refuses (idles, signs nothing) unless a live attester is
    configured AND the connected chain matches ATTEST_CHAIN_ID. Reconciles in-flight
    'sending' then 'sent' rows, ensures the schema is registered once, then attests up to
    MAX_PER_TICK pending orders. Returns the number of orders acted on."""
    attester = _attester_factory()
    if attester is None:
        return 0  # dormant / misconfigured — no RPC, no signing
    try:
        chain_id = _chain_id(attester)
    except Exception:
        logger.warning("attest: chain_id unavailable; idling this tick")
        return 0
    if chain_id != config.ATTEST_CHAIN_ID:
        logger.error(
            "attest: connected chain_id %s != expected %s; refusing to sign",
            chain_id,
            config.ATTEST_CHAIN_ID,
        )
        return 0

    _reconcile_sending(attester)
    _reconcile_sent(attester)
    if not _ensure_schema(attester):
        return 0
    acted = 0
    for order_id, store_id in _pending_orders():
        _attest_one(attester, order_id, store_id)
        acted += 1
    return acted


async def attest_loop() -> None:
    """Background loop (lifespan task) mirroring sweeper_loop/webhook_loop: each tick
    runs off-thread; a bad tick is logged and the loop continues; idle when nothing is
    pending. Started ONLY when SWEEP_ENABLED + ATTEST_ENABLED + a key are all set."""
    logger.info("tilla attest loop started (interval=%ss)", config.ATTEST_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(attest_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("attest tick failed")
        await asyncio.sleep(config.ATTEST_INTERVAL)

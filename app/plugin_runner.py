"""M15.4 — out-of-process plugin runner: CLIENT SHIM + CONTRACT ONLY.

INV-1 forbids third-party *code* from ever importing into the app process. When a
code plugin (a `delivery` / `payment_rail` kind) eventually exists it will run as a
SEPARATE, unprivileged systemd unit and speak a versioned JSON contract to this app
over a localhost unix socket — never in-process, never with a DB handle or inherited
env. This module is the buildable half of that boundary: the client shim + the wire
contract + its fail-closed behavior. It is deliberately NOT wired into any live
delivery/settle path, because **no external code plugin exists** (EXTERNALLY-BLOCKED,
per the M15 spec's own precondition). Building past this boundary with zero real
plugin authors would be scaffolding theater.

Design committed here so nobody "temporarily" imports third-party code in-process:

  Transport   one connection per call; a 2s deadline (``DEFAULT_TIMEOUT``) covers
              connect + send + receive. PROD family is ``AF_UNIX`` on a socket the
              systemd unit owns (``PrivateTmp``, ``ProtectHome``, dedicated user, no
              env inheritance). The family/address are injectable so CI — where
              ``AF_UNIX`` is unavailable — can exercise the exact same shim over a
              loopback TCP echo server.
  Framing     one newline-terminated JSON object per message, each way.
  Contract    request  {"v":1,"method":"mint"|"revoke"|"gate","order_id":str,"params":{}}
              response {"v":1,"ok":bool,"result":<payload>} | {"v":1,"ok":false,"error":str}
  Fail-closed ANY transport error, timeout, malformed/oversized frame, protocol-version
              mismatch, or ``ok:false`` raises :class:`PluginRunnerUnavailable`. The
              caller falls back to the BUILT-IN refusal path — it never delivers, never
              settles, and never half-applies a side effect (no 500-with-side-effects).

Buildable-now acceptance lives in ``tests/test_runner_shim.py``:
``test_runner_timeout_fails_closed`` + the contract goldens. An actual external code
plugin is NOT buildable honestly and stays parked.
"""

from __future__ import annotations

import json
import socket

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT = 2.0  # seconds — connect + send + receive, fail-closed on expiry
DEFAULT_SOCKET_PATH = "/run/tilla/plugin-runner.sock"  # prod AF_UNIX path
_MAX_FRAME_BYTES = 64 * 1024  # a code plugin returns a small payload, never a stream
_METHODS = ("mint", "revoke", "gate")


class PluginRunnerUnavailable(Exception):
    """The out-of-process runner could not be reached OR returned an unusable
    answer. ALWAYS fail-closed: the caller must fall back to the built-in refusal
    path (no delivery, no settle, no side effects)."""


def build_request(method: str, order_id: str, params: dict | None = None) -> bytes:
    """The exact newline-terminated request frame the shim puts on the wire. A pure
    function so the contract can be golden-tested without a socket."""
    if method not in _METHODS:
        raise ValueError(f"unknown runner method {method!r}")
    payload = {
        "v": PROTOCOL_VERSION,
        "method": method,
        "order_id": order_id,
        "params": params or {},
    }
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()


def parse_response(frame: bytes, method: str):
    """Decode a response frame, fail-closed. Returns the ``result`` on a well-formed
    ``ok:true`` answer of the current protocol version; raises
    :class:`PluginRunnerUnavailable` on anything else."""
    try:
        message = json.loads(frame.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PluginRunnerUnavailable(f"malformed runner response: {exc}") from exc
    if not isinstance(message, dict):
        raise PluginRunnerUnavailable("runner response was not a JSON object")
    if message.get("v") != PROTOCOL_VERSION:
        raise PluginRunnerUnavailable(
            f"runner protocol mismatch: expected v{PROTOCOL_VERSION}, "
            f"got v{message.get('v')!r}"
        )
    if message.get("ok") is not True:
        raise PluginRunnerUnavailable(
            f"runner refused {method!r}: {message.get('error', 'ok:false')}"
        )
    return message.get("result")


class PluginRunnerClient:
    """Fail-closed client for an out-of-process code plugin. One short-lived
    connection per call; no state, no retries (a retry would just widen the window
    during which the built-in refusal is deferred)."""

    def __init__(
        self,
        address=DEFAULT_SOCKET_PATH,
        *,
        family: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        # PROD default is AF_UNIX (a filesystem socket path); tests inject AF_INET +
        # a (host, port) tuple. AF_UNIX may be absent (e.g. Windows CI), so resolve
        # lazily and fail-closed rather than at import.
        self.address = address
        self.family = family
        self.timeout = timeout

    def _resolve_family(self) -> int:
        if self.family is not None:
            return self.family
        unix = getattr(socket, "AF_UNIX", None)
        if unix is None:
            raise PluginRunnerUnavailable(
                "AF_UNIX unavailable on this platform; runner transport unusable"
            )
        return unix

    def _call(self, method: str, order_id: str, params: dict | None = None):
        request = build_request(method, order_id, params)
        try:
            family = self._resolve_family()
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.address)
                sock.sendall(request)
                frame = self._recv_frame(sock)
        except PluginRunnerUnavailable:
            raise
        except (OSError, socket.timeout) as exc:
            # connect refused / dead socket / timeout — all fail-closed
            raise PluginRunnerUnavailable(f"runner unreachable: {exc}") from exc
        return parse_response(frame, method)

    def _recv_frame(self, sock: socket.socket) -> bytes:
        """Read one newline-terminated frame, bounded by ``_MAX_FRAME_BYTES``. A
        peer that never terminates (or floods) trips the socket timeout / the size
        cap → fail-closed, never an unbounded read."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                raise PluginRunnerUnavailable("runner closed before a full response")
            newline = chunk.find(b"\n")
            if newline != -1:
                chunks.append(chunk[: newline + 1])
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FRAME_BYTES:
                raise PluginRunnerUnavailable("runner response exceeded frame cap")

    def mint(self, order_id: str, params: dict | None = None) -> str:
        result = self._call("mint", order_id, params)
        if not isinstance(result, str):
            raise PluginRunnerUnavailable("runner mint returned a non-string payload")
        return result

    def revoke(self, order_id: str, params: dict | None = None) -> bool:
        return bool(self._call("revoke", order_id, params))

    def gate(self, order_id: str, params: dict | None = None) -> int | None:
        result = self._call("gate", order_id, params)
        if result is not None and not isinstance(result, int):
            raise PluginRunnerUnavailable("runner gate returned a non-int code")
        return result


def mint_fail_closed(client: PluginRunnerClient, order_id: str) -> str | None:
    """Attempt an out-of-process code-plugin mint; on ANY runner failure return
    ``None`` — the built-in refusal path — leaving all state exactly as it was.

    This is the delivery-side boundary: because ``mint`` is payload-only and this
    never touches the DB, a fail-closed outcome cannot deliver, cannot settle, and
    cannot half-apply a side effect. No external code plugin exists yet, so nothing
    live calls this — it exists so the refusal contract is real and tested."""
    try:
        return client.mint(order_id)
    except PluginRunnerUnavailable:
        return None

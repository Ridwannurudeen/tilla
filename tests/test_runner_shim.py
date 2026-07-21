"""M15.4 — out-of-process runner CLIENT SHIM tests (boundary only).

Proves the wire contract (request/response goldens) and, above all, that the shim
FAILS CLOSED: a dead socket, a hung peer, a protocol mismatch, or a refusal all raise
PluginRunnerUnavailable, and the delivery-side ``mint_fail_closed`` boundary then
leaves the order undelivered with no side effects — never a 500 that half-applies.

AF_UNIX is unavailable on this CI platform, so the identical shim is exercised over a
loopback TCP echo server (family injected); PROD transport is AF_UNIX. An actual
external code plugin is EXTERNALLY-BLOCKED and stays parked — only the shim is built.
"""

import contextlib
import socket
import threading
import time

from app import plugin_runner
from app.db import SessionLocal
from app.models import Delivery, Order
from app.plugin_runner import (
    PROTOCOL_VERSION,
    PluginRunnerClient,
    PluginRunnerUnavailable,
    build_request,
    mint_fail_closed,
    parse_response,
)


class _ScriptedServer:
    """A one-shot loopback TCP server. ``responder(request) -> bytes | None`` returns
    the response frame, or ``None`` to accept-then-hang (to exercise the timeout)."""

    def __init__(self, responder):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.address = self._sock.getsockname()
        self._responder = responder
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            with contextlib.suppress(OSError):
                data = conn.recv(65536)
                resp = self._responder(data)
                if resp is not None:
                    conn.sendall(resp)
                else:
                    time.sleep(2.0)  # never reply → client trips its deadline

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(OSError):
            self._sock.close()


def _tcp_client(address, timeout=2.0):
    return PluginRunnerClient(address, family=socket.AF_INET, timeout=timeout)


def _ok_frame(result):
    import json

    return (
        json.dumps({"v": PROTOCOL_VERSION, "ok": True, "result": result}) + "\n"
    ).encode()


# --------------------------------------------------------------- contract goldens
def test_build_request_goldens():
    assert build_request("mint", "o-1") == (
        b'{"method":"mint","order_id":"o-1","params":{},"v":1}\n'
    )
    assert build_request("gate", "o-9", {"amount_micro": 5}) == (
        b'{"method":"gate","order_id":"o-9","params":{"amount_micro":5},"v":1}\n'
    )


def test_parse_response_goldens():
    assert parse_response(_ok_frame("SECRET-PAYLOAD"), "mint") == "SECRET-PAYLOAD"
    # protocol mismatch → fail-closed
    with contextlib.suppress(PluginRunnerUnavailable):
        parse_response(b'{"v":2,"ok":true,"result":"x"}\n', "mint")
        raise AssertionError("version mismatch must fail closed")
    # ok:false → fail-closed
    try:
        parse_response(b'{"v":1,"ok":false,"error":"nope"}\n', "revoke")
        raise AssertionError("ok:false must fail closed")
    except PluginRunnerUnavailable as exc:
        assert "nope" in str(exc)
    # malformed JSON → fail-closed
    try:
        parse_response(b"not json\n", "mint")
        raise AssertionError("malformed must fail closed")
    except PluginRunnerUnavailable:
        pass


# --------------------------------------------------------- happy path over a socket
def test_echo_roundtrip_over_socket():
    captured = {}

    def responder(request):
        captured["request"] = request
        return _ok_frame("DELIVERED-KEY")

    with _ScriptedServer(responder) as server:
        client = _tcp_client(server.address)
        assert client.mint("order-42") == "DELIVERED-KEY"
    # the server saw exactly the golden request frame
    assert captured["request"] == build_request("mint", "order-42")


def test_gate_and_revoke_typed_results():
    with _ScriptedServer(lambda _r: _ok_frame(402)) as server:
        assert _tcp_client(server.address).gate("o") == 402
    with _ScriptedServer(lambda _r: _ok_frame(None)) as server:
        assert _tcp_client(server.address).gate("o") is None
    with _ScriptedServer(lambda _r: _ok_frame(True)) as server:
        assert _tcp_client(server.address).revoke("o") is True


def test_version_mismatch_over_socket_fails_closed():
    bad = b'{"v":99,"ok":true,"result":"x"}\n'
    with _ScriptedServer(lambda _r: bad) as server:
        try:
            _tcp_client(server.address).mint("o")
            raise AssertionError("must fail closed on version mismatch")
        except PluginRunnerUnavailable as exc:
            assert "protocol mismatch" in str(exc)


# ------------------------------------------------------------- fail-closed: timeout
def test_runner_timeout_fails_closed(make_store):
    """A hung runner (accepts, never replies) trips the deadline → the delivery-side
    boundary refuses, the order stays undelivered, and nothing 500s with a
    half-applied side effect."""
    sid = make_store(slug="runner-timeout")
    with SessionLocal() as s:
        s.add(
            Order(
                id="rt-order",
                store_id=sid,
                pay_to="0x" + "a" * 40,
                amount_micro=1_000_000,
                expected_micro=1_000_000,
                status="confirmed",
            )
        )
        s.commit()

    with _ScriptedServer(lambda _r: None) as server:  # accept-then-hang
        client = _tcp_client(server.address, timeout=0.3)
        start = time.monotonic()
        # the raw shim raises (fail-closed) …
        try:
            client.mint("rt-order")
            raise AssertionError("a hung runner must fail closed")
        except PluginRunnerUnavailable:
            pass
        assert time.monotonic() - start < 2.0  # bounded by the 0.3s deadline
        # … and the delivery boundary refuses without touching state
        assert mint_fail_closed(client, "rt-order") is None

    with SessionLocal() as s:
        assert s.get(Order, "rt-order").status == "confirmed"  # still undelivered
        assert (
            s.scalar(Delivery.__table__.select().where(Delivery.order_id == "rt-order"))
            is None
        )  # no Delivery row written


# ---------------------------------------------------------- fail-closed: dead socket
def test_runner_dead_socket_fails_closed():
    # bind then close → the address is refused on connect
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_address = probe.getsockname()
    probe.close()
    client = _tcp_client(dead_address, timeout=0.5)
    try:
        client.mint("o")
        raise AssertionError("a dead socket must fail closed")
    except PluginRunnerUnavailable:
        pass
    assert mint_fail_closed(client, "o") is None


def test_af_unix_absent_fails_closed(monkeypatch):
    # a default (AF_UNIX) client on a platform without AF_UNIX fails closed rather
    # than raising at import or leaking a different error
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    client = PluginRunnerClient(plugin_runner.DEFAULT_SOCKET_PATH, timeout=0.5)
    try:
        client.mint("o")
        raise AssertionError("missing AF_UNIX must fail closed")
    except PluginRunnerUnavailable as exc:
        assert "AF_UNIX" in str(exc)

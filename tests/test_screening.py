import httpx
import pytest
import respx

from app import screening
from app.config import WARDEN_SCREEN_URL


@respx.mock
def test_scan_returns_verdict_on_allow():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )
    verdict = screening.scan("safe text")
    assert verdict["verdict"] == "ALLOW"


@respx.mock
def test_scan_raises_blocked_on_block_verdict():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"verdict": "BLOCK", "risk_level": "high", "threat_classes": ["scam"]},
        )
    )
    with pytest.raises(screening.ScreeningBlocked) as exc_info:
        screening.scan("bad text")
    assert exc_info.value.verdict["risk_level"] == "high"


@respx.mock
def test_scan_raises_unavailable_on_timeout():
    respx.post(WARDEN_SCREEN_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_raises_unavailable_on_connect_error():
    respx.post(WARDEN_SCREEN_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_raises_unavailable_on_5xx():
    respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_raises_unavailable_on_4xx():
    respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_raises_unavailable_on_missing_verdict():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"risk_level": "none"})
    )
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_raises_unavailable_on_unrecognized_verdict():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "REVIEW"})
    )
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_raises_unavailable_on_non_json_body():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, text="not json")
    )
    with pytest.raises(screening.ScreeningUnavailable):
        screening.scan("text")


@respx.mock
def test_scan_with_retry_returns_allow_on_success():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    assert screening.scan_with_retry("text") == "allow"


@respx.mock
def test_scan_with_retry_returns_pending_after_repeated_unavailability():
    respx.post(WARDEN_SCREEN_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    assert screening.scan_with_retry("text", attempts=2) == "pending"


@respx.mock
def test_scan_with_retry_recovers_on_second_attempt():
    route = respx.post(WARDEN_SCREEN_URL)
    route.side_effect = [
        httpx.TimeoutException("timeout"),
        httpx.Response(200, json={"verdict": "ALLOW"}),
    ]
    assert screening.scan_with_retry("text", attempts=2) == "allow"


@respx.mock
def test_scan_with_retry_pending_on_4xx():
    respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(429))
    assert screening.scan_with_retry("text", attempts=2) == "pending"


@respx.mock
def test_scan_with_retry_pending_on_missing_verdict():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"risk_level": "none"})
    )
    assert screening.scan_with_retry("text", attempts=2) == "pending"


@respx.mock
def test_scan_with_retry_never_swallows_block():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    with pytest.raises(screening.ScreeningBlocked):
        screening.scan_with_retry("text", attempts=2)


# ---------- M10 screen() dispatch (demo path — paid path is in test_warden_hire) ----
@respx.mock
def test_screen_demo_allow_returns_demo_receipt():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )
    outcome = screening.screen("safe")
    assert outcome.status == "allow"
    assert outcome.receipt is not None
    assert outcome.receipt.mode == "demo"
    assert outcome.receipt.verdict == "ALLOW"
    assert outcome.receipt.endpoint == WARDEN_SCREEN_URL
    assert outcome.receipt.amount_micro is None
    assert outcome.receipt.tx_hash is None


@respx.mock
def test_screen_demo_pending_has_no_receipt():
    respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(503))
    outcome = screening.screen("text", attempts=2)
    assert outcome.status == "pending"
    assert outcome.receipt is None


@respx.mock
def test_screen_demo_block_propagates():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    with pytest.raises(screening.ScreeningBlocked):
        screening.screen("bad")

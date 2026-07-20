import os

import pytest

# Import app.main with the x402 paywall and LLM generation both off so the
# suite never needs network, secrets, or a facilitator. These are popped at
# collection time, before any test module imports app.main.
os.environ.pop("OKX_API_KEY", None)
os.environ.pop("TILLA_LLM_KEY", None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """slowapi's in-memory limiter is a module-level singleton keyed by
    client IP; every TestClient call shares one IP, so without a reset each
    test would inherit rate-limit state left over by the previous one."""
    import app.main as main

    main.limiter.reset()
    yield
    main.limiter.reset()

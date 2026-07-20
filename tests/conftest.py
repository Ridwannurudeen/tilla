import os

# Import app.main with the x402 paywall and LLM generation both off so the
# suite never needs network, secrets, or a facilitator. These are popped at
# collection time, before any test module imports app.main.
os.environ.pop("OKX_API_KEY", None)
os.environ.pop("TILLA_LLM_KEY", None)

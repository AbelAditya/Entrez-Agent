"""Shared client-side rate limiting for OpenRouter free-tier keys.

The free tier allows 20 requests/minute and 50 requests/day per key. The
per-minute limit is enforced here by pacing requests before they are sent, which
is cheaper than being rejected and retrying. The daily limit is a budget, not a
pace, so it is handled where work is scheduled (see evals/evals.py).

One limiter per key: rate limits apply per key, so models sharing a key must
share a bucket. Limiters are per-process, so this paces one agent process, not
several running at once.
"""

from langchain_core.rate_limiters import InMemoryRateLimiter

REQUESTS_PER_MINUTE = 20
REQUESTS_PER_DAY = 50

_limiters: dict[str, InMemoryRateLimiter] = {}


def limiter(key_name: str = "OPENROUTER_API_KEY") -> InMemoryRateLimiter:
    """Return the shared limiter for the given API key environment variable."""
    if key_name not in _limiters:
        _limiters[key_name] = InMemoryRateLimiter(
            requests_per_second=REQUESTS_PER_MINUTE / 60,
            check_every_n_seconds=0.5,
            max_bucket_size=4,  # tolerate a short burst, e.g. a tool loop turning over quickly
        )
    return _limiters[key_name]

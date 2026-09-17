"""Redis-backed key/value store, with an in-process fallback.

Every key is namespaced under `entrez:` so the project's state can be inspected
or cleared without touching anything else in the instance:

    redis-cli --scan --pattern 'entrez:*'
    redis-cli --scan --pattern 'entrez:*' | xargs -r -L 500 redis-cli UNLINK

A cache being unavailable must never fail a query, so if Redis cannot be reached
the store falls back to a process-local dict and says so once. Set
ENTREZ_CACHE=off to disable caching entirely (the eval runner does this, so runs
are measured cold).
"""

import hashlib
import logging
import os
import re
import time
import unicodedata
from typing import Optional

log = logging.getLogger(__name__)

NAMESPACE = "entrez"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
VERDICT_TTL = 30 * 24 * 3600  # a month; a policy change invalidates keys on its own

_ZERO_WIDTH = re.compile(r"[​-\u200F﻿]")


def caching_enabled() -> bool:
    return os.getenv("ENTREZ_CACHE", "on").lower() not in ("off", "0", "false", "no")


def canonical(text: str) -> str:
    """Fold away differences that cannot change meaning: unicode form, invisible
    characters, whitespace runs and case."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return " ".join(text.split()).casefold()


class KeyValueStore:
    """Small get/set interface over Redis that degrades to a local dict."""

    def __init__(self, url: str = REDIS_URL, enabled: Optional[bool] = None):
        self.enabled = caching_enabled() if enabled is None else enabled
        self._local: dict[str, tuple[str, float]] = {}
        self._redis = None
        if self.enabled:
            self._redis = self._connect(url)

    def _connect(self, url: str):
        try:
            import redis

            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                # short timeouts: a slow cache must not hold up the agent
                socket_timeout=0.25,
                socket_connect_timeout=0.25,
            )
            client.ping()
            return client
        except Exception as exc:
            log.warning("Redis unavailable (%s: %s); caching in-process only", type(exc).__name__, exc)
            return None

    def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        if self._redis is not None:
            try:
                return self._redis.get(key)
            except Exception as exc:
                log.warning("Redis get failed (%s); falling back to memory", type(exc).__name__)
                self._redis = None
        value = self._local.get(key)
        if value is None:
            return None
        payload, expires_at = value
        if expires_at and expires_at < time.time():
            del self._local[key]
            return None
        return payload

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        if not self.enabled:
            return
        if self._redis is not None:
            try:
                self._redis.set(key, value, ex=ttl)
                return
            except Exception as exc:
                log.warning("Redis set failed (%s); falling back to memory", type(exc).__name__)
                self._redis = None
        self._local[key] = (value, time.time() + ttl if ttl else 0.0)

    @property
    def backend(self) -> str:
        if not self.enabled:
            return "disabled"
        return "redis" if self._redis is not None else "memory"


class VerdictCache:
    """Exact-match cache for one guardrail check.

    The key is a hash of the checked text, so a lookup is a hit or a miss with
    nothing in between. Similarity matching is deliberately not used here: an
    appended jailbreak scores as highly against a cached benign query as an
    honest paraphrase does, and a wrong hit would skip the check with nothing
    downstream to catch it.

    The check prompt and model are part of the key, so editing a prompt or
    swapping the checking model invalidates old verdicts automatically.
    """

    def __init__(self, store: KeyValueStore, kind: str, check_prompt: str, model: str, ttl: int = VERDICT_TTL):
        self.store = store
        self.kind = kind  # "in" or "out"
        self.model = model
        self.ttl = ttl
        self.prompt_id = hashlib.sha256((check_prompt or "").encode()).hexdigest()[:8]

    def key(self, text: str) -> str:
        digest = hashlib.sha256(f"{self.model}\x00{canonical(text)}".encode()).hexdigest()
        return f"{NAMESPACE}:verdict:{self.kind}:{self.prompt_id}:{digest}"

    def get(self, text: str) -> Optional[str]:
        return self.store.get(self.key(text))

    def set(self, text: str, verdict: str) -> None:
        self.store.set(self.key(text), verdict, ttl=self.ttl)

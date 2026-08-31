"""
keka/dao/_http.py
Shared HTTP for the Keka DAOs.

Every Keka list endpoint returns the same envelope:

    { "succeeded": bool, "message": str|null, "errors": [str]|null,
      "data": [ ... ],
      "pageNumber": int, "pageSize": int, "totalPages": int, "totalRecords": int,
      "firstPage": uri|null, "lastPage": uri|null,
      "nextPage": uri|null, "previousPage": uri|null }

`get_all` unwraps it and follows `nextPage` to completion. Keka's docs are explicit
that you follow the returned `nextPage` URI rather than constructing `?pageNumber=N`
offsets, so that is what this does.

Also a TTL cache, because Keka's rate limit is 50 requests/minute for the whole
tenant — shared with the leave flows and everything else on the same API key.
"""

import logging
import threading
import time

import requests

from keka import config
from keka.client import base_url, get_access_token
from keka.models import KekaServiceError

logger = logging.getLogger(__name__)

PAGE_SIZE = 200          # Keka's documented maximum; fewer round trips per month
TIMEOUT = 20
MAX_PAGES = 50           # backstop against a broken nextPage chain


class KekaRateLimited(KekaServiceError):
    """Keka returned 429. Carries the retry hint so the API can pass it through."""

    def __init__(self, retry_after: int = 60):
        super().__init__(f"Keka rate limit exceeded; retry in {retry_after}s")
        self.retry_after = retry_after


# ── TTL cache ─────────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def cached(bucket: str, key: str, producer):
    """Return a cached value for (bucket, key), or produce and store it."""
    ttl = config.cache_ttl(bucket)
    now = time.time()
    ck = (bucket, key)
    with _cache_lock:
        hit = _cache.get(ck)
        if hit and hit[0] > now:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[ck] = (now + ttl, value)
    return value


def invalidate(bucket: str, key: str = None) -> None:
    with _cache_lock:
        for ck in [k for k in _cache if k[0] == bucket and (key is None or k[1] == key)]:
            _cache.pop(ck, None)


def cache_stats() -> dict:
    with _cache_lock:
        now = time.time()
        return {
            "entries": len(_cache),
            "live": sum(1 for exp, _ in _cache.values() if exp > now),
        }


# ── Requests ──────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json",
    }


def _check(resp, what: str) -> dict:
    if resp.status_code == 429:
        try:
            retry = int(resp.headers.get("Retry-After", "60"))
        except ValueError:
            retry = 60
        logger.warning("[keka] 429 on %s — retry in %ss", what, retry)
        raise KekaRateLimited(retry)
    if resp.status_code in (401, 403):
        raise KekaServiceError(
            f"{what}: HTTP {resp.status_code} — check the API key's scopes in Keka Admin"
        )
    if resp.status_code >= 400:
        raise KekaServiceError(f"{what}: HTTP {resp.status_code} — {resp.text[:200]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise KekaServiceError(f"{what}: response was not JSON") from exc

    # `succeeded: false` with HTTP 200 is a Keka-level failure.
    if body.get("succeeded") is False:
        errs = body.get("errors") or []
        msg = errs[0] if errs else (body.get("message") or "unspecified error")
        raise KekaServiceError(f"{what}: {msg}")
    return body


def get_all(path: str, params: dict = None, what: str = None) -> list:
    """
    GET a paged Keka endpoint and return every `data` row across all pages.

    `path` is relative to the configured base URL, e.g. '/psa/timeentries'.
    """
    what = what or path
    url = f"{base_url()}{path}"
    query = dict(params or {})
    query.setdefault("pageSize", PAGE_SIZE)

    rows: list = []
    pages = 0
    while url and pages < MAX_PAGES:
        resp = requests.get(url, headers=_headers(), params=query, timeout=TIMEOUT)
        body = _check(resp, what)
        rows.extend(body.get("data") or [])
        pages += 1

        nxt = body.get("nextPage")
        if not nxt:
            break
        # nextPage carries its own paging state; re-sending our params would
        # override it, so follow the URI as given.
        url, query = nxt, {}

    if pages >= MAX_PAGES:
        logger.warning("[keka] %s stopped at the %d page cap", what, MAX_PAGES)
    logger.info("[keka] %s -> %d rows in %d page(s)", what, len(rows), pages)
    return rows


def get_one(path: str, params: dict = None, what: str = None) -> dict:
    """GET a paged endpoint and return the first row, or None."""
    rows = get_all(path, params, what)
    return rows[0] if rows else None

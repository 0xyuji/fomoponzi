"""Rate-limited, retrying, disk-cached HTTP helpers shared by all stages."""
import hashlib
import json
import random
import threading
import time
from pathlib import Path

import requests

import config


class RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval


rpc_limiter = RateLimiter(config.RPC_RATE_LIMIT)
enhanced_limiter = RateLimiter(config.ENHANCED_RATE_LIMIT)
gecko_limiter = RateLimiter(config.GECKO_RATE_LIMIT)

MAX_RETRIES = 6


def _retryable_post(url, payload, limiter, timeout=60):
    last_err = None
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            _backoff(attempt, f"network error: {e}")
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            _backoff(attempt, f"HTTP {resp.status_code}", retry_after)
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            continue
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()
    raise RuntimeError(f"gave up after {MAX_RETRIES} attempts: {last_err}")


def _retryable_get(url, params, limiter, timeout=30):
    last_err = None
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            _backoff(attempt, f"network error: {e}")
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            _backoff(attempt, f"HTTP {resp.status_code}", resp.headers.get("Retry-After"))
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            continue
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()
    raise RuntimeError(f"gave up after {MAX_RETRIES} attempts: {last_err}")


def _backoff(attempt, why, retry_after=None):
    # Always back off at least exponentially. A server may send `Retry-After: 0`
    # (GeckoTerminal does), and honoring that verbatim burns every retry in
    # milliseconds -- so the header can only ever lengthen the wait, never shorten it.
    delay = (2 ** attempt) + random.random()
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    delay = min(max(delay, 1.0), 60)
    print(f"    retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s ({why})")
    time.sleep(delay)


def rpc_call(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
    data = _retryable_post(config.RPC_URL, payload, rpc_limiter)
    if "error" in data:
        raise RuntimeError(f"RPC error for {method}: {data['error']}")
    return data["result"]


def cache_key(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def cached_json(path: Path, produce):
    """Return cached JSON at `path`, else call produce() and cache the result."""
    if path.exists():
        with path.open() as f:
            return json.load(f)
    value = produce()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(value, f)
    tmp.replace(path)
    return value


def cached_json_gz(path: Path, produce):
    """Same as cached_json but gzipped -- Enhanced tx payloads compress ~7.5x."""
    import gzip

    if path.exists():
        with gzip.open(path, "rt") as f:
            return json.load(f)
    value = produce()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", compresslevel=6) as f:
        json.dump(value, f)
    tmp.replace(path)
    return value

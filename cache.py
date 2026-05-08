"""
Redis-backed cache with in-process dict fallback.
Usage: replace `dict` cache declarations in db.py with CacheDict.
Set REDIS_URL env var to enable Redis (e.g. Upstash free tier).
Without REDIS_URL, behaves exactly like a plain dict.
"""
import json
import os
import time

_redis_client = None
_redis_ok: bool | None = None   # None=未測試 False=不可用 True=可用


def _get_redis():
    """Return connected Redis client, or None if unavailable."""
    global _redis_client, _redis_ok
    if _redis_ok is False:
        return None
    if _redis_ok is True:
        return _redis_client

    redis_url = os.environ.get('REDIS_URL', '')
    if not redis_url:
        _redis_ok = False
        return None
    try:
        import redis as _r
        client = _r.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=0.3,
            socket_connect_timeout=0.3,
            retry_on_timeout=False,
        )
        client.ping()
        _redis_client = client
        _redis_ok = True
        print("[cache] Redis connected ✓")
        return _redis_client
    except Exception as e:
        print(f"[cache] Redis unavailable ({e}), using in-process cache")
        _redis_ok = False
        return None


class CacheDict(dict):
    """
    Drop-in dict replacement that mirrors writes to Redis when available.

    L1 = in-process dict  (zero-cost read, lost on worker restart)
    L2 = Redis            (shared across workers, survives restart)

    redis_ttl: Redis key TTL in seconds. Should be >= the TTL your code checks.
    """

    def __init__(self, name: str, redis_ttl: int = 300):
        super().__init__()
        self._name = name
        self._redis_ttl = redis_ttl

    def _rk(self, key) -> str:
        return f"punch:{self._name}:{key}"

    # ── read ──────────────────────────────────────────────────────

    def __missing__(self, key):
        """Return None instead of raising KeyError — keeps existing code safe."""
        r = _get_redis()
        if r:
            try:
                raw = r.get(self._rk(key))
                if raw is not None:
                    val = json.loads(raw)
                    super().__setitem__(key, val)   # warm L1
                    return val
            except Exception:
                pass
        return None

    def get(self, key, default=None):
        val = self[key]   # calls __missing__ on cache miss
        return val if val is not None else default

    # ── write ─────────────────────────────────────────────────────

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        r = _get_redis()
        if r:
            try:
                r.setex(self._rk(key), self._redis_ttl,
                        json.dumps(value, default=str))
            except Exception:
                pass

    def __delitem__(self, key):
        super().__delitem__(key)
        r = _get_redis()
        if r:
            try:
                r.delete(self._rk(key))
            except Exception:
                pass

    def pop(self, key, *args):
        result = super().pop(key, *args)
        r = _get_redis()
        if r:
            try:
                r.delete(self._rk(key))
            except Exception:
                pass
        return result

    def update(self, other=None, **kwargs):
        items = other.items() if hasattr(other, 'items') else (other or [])
        for k, v in items:
            self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    # ── clear ─────────────────────────────────────────────────────

    def clear(self):
        super().clear()
        r = _get_redis()
        if r:
            try:
                keys = r.keys(f"punch:{self._name}:*")
                if keys:
                    r.delete(*keys)
            except Exception:
                pass

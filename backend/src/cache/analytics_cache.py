"""
Redis-based caching system for RAG Analytics
Provides high-performance caching for analytics queries, metrics, and reports
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Set, Union

try:
    import redis
    import redis.asyncio as redis

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

from src.config.analytics_config import get_analytics_config
from src.core.background_tasks import spawn
from src.core.config import settings
from src.exceptions.analytics_exceptions import CacheError, CacheMissError

logger = logging.getLogger(__name__)

# Every key this cache stores starts with ``analytics:``, so one SCAN (or a
# flush of ``analytics:*``) covers the whole cache and nothing else in the
# shared Redis, which is also the Celery broker.
KEY_PREFIX = "analytics"
_KEY_PREFIX_SEP = f"{KEY_PREFIX}:"
_NAMESPACE_PATTERN = f"{KEY_PREFIX}:*"


def _org_segment(organization_id: Any) -> str:
    """``organization_id`` as a key segment: its canonical UUID string.

    Ids are UUIDs. Anything else is rejected, because a ``:`` (or glob
    syntax) in the id would shift the segments that key patterns rely on,
    and ``str(None)`` would put every org-less caller in one shared entry.
    """
    try:
        return str(uuid.UUID(str(organization_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("analytics cache keys need a UUID organization_id") from exc


def analytics_key(scope: str, kind: str, organization_id: Any, *rest: str) -> str:
    """``analytics:{scope}:{kind}:{org uuid}[:{rest}...]``, validated.

    The one way to build an org-scoped key: the org id must be a UUID and no
    other segment may be empty or contain the ``:`` separator.
    """
    segments = [scope, kind, _org_segment(organization_id), *rest]
    for segment in segments:
        if not segment or ":" in segment:
            raise ValueError(f"invalid analytics cache key segment: {segment!r}")
    return ":".join([KEY_PREFIX, *segments])


def _require_namespaced(key: str) -> None:
    if not isinstance(key, str) or not key.startswith(_KEY_PREFIX_SEP):
        raise ValueError(f"analytics cache keys must start with {_KEY_PREFIX_SEP!r}")


@dataclass
class CacheKey:
    """Cache key structure for analytics data"""

    prefix: str
    organization_id: str
    data_type: str
    filters: Dict[str, Any]
    time_range: Optional[tuple] = None

    def generate_key(self) -> str:
        """Generate a unique cache key"""
        # Create a hash of filters and time_range for consistent key length
        filter_str = json.dumps(sorted(self.filters.items()), sort_keys=True)
        time_str = json.dumps(self.time_range) if self.time_range else ""

        hash_input = f"{self.prefix}:{self.organization_id}:{self.data_type}:{filter_str}:{time_str}"
        # Use MD5 for non-security cache key generation (usedforsecurity=False)
        hash_key = hashlib.md5(hash_input.encode(), usedforsecurity=False).hexdigest()

        return analytics_key(
            self.prefix, self.data_type, self.organization_id, hash_key
        )


class CacheTTL:
    """Cache TTL constants for different data types"""

    # Fast changing data - short TTL
    REALTIME_METRICS = 60  # 1 minute
    DASHBOARD_DATA = 180  # 3 minutes

    # Medium changing data - medium TTL
    AGGREGATED_METRICS = 300  # 5 minutes
    USER_BEHAVIOR = 600  # 10 minutes

    # Slow changing data - long TTL
    QUALITY_SCORES = 1800  # 30 minutes
    PERFORMANCE_SUMMARY = 900  # 15 minutes

    # Reports - very long TTL
    REPORTS = 3600  # 1 hour
    EXPORTS = 7200  # 2 hours


# After any Redis failure the cache serves from memory for this long, then
# makes ONE background reconnect attempt: a Redis outage costs one attempt
# per window, never one per request (circuit breaker, fail open).
_RECONNECT_INTERVAL_SECONDS = 30.0
# Analytics caching is an optimisation: a slow or black-holed Redis must cost
# well under a second, after which the call falls back to memory.
_REDIS_CONNECT_TIMEOUT_S = 0.5
_REDIS_OP_TIMEOUT_S = 0.5
_SCAN_BATCH = 500
# Keys and patterns deleted (or written) while Redis was unreachable are
# replayed as deletes on reconnect, so Redis does not serve the old value
# until its TTL. Past this many the queue collapses into one flush of the
# analytics namespace: bounded memory, and still correct.
_MAX_PENDING_INVALIDATIONS = 1000


def _redis_url(config: Any) -> Optional[str]:
    """``ANALYTICS_redis_analytics_url`` if set, else an explicit ``REDIS_URL``.

    No deployment sets the analytics-specific variable; the live pods get
    Redis only as ``REDIS_URL`` from the ``redis-credentials`` Secret. The
    settings default (``redis://localhost:6379``) does not count: without an
    explicit URL the cache stays in memory and never tries to connect, so a
    local run without Redis does not retry a connect every window.
    """
    if config.redis_analytics_url:
        return config.redis_analytics_url
    if "REDIS_URL" not in getattr(settings, "model_fields_set", ()):
        return None
    return getattr(settings, "REDIS_URL", None) or None


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """Redis glob (``*``, ``?``, backslash escapes) as an anchored regex."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if char == "*" else "." if char == "?" else re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


class AnalyticsCache:
    """Redis-based analytics cache with fallback to in-memory cache"""

    def __init__(self):
        self.config = get_analytics_config()
        self.redis_client = None
        self.fallback_cache = {}  # Simple in-memory fallback
        self.fallback_cache_expiry = {}
        self._connected = False
        # Connecting needs an event loop, which __init__ (module import) does
        # not have. The old code called the async _init_redis() without
        # awaiting it, so Redis never connected and every pod silently used
        # its own in-memory cache. Connect lazily on first use instead.
        self._redis_url = _redis_url(self.config)
        self._redis_enabled = bool(REDIS_AVAILABLE and self._redis_url)
        self._next_connect_attempt = 0.0
        self._connect_task: Optional[asyncio.Task] = None
        self._connect_task_loop: Optional[asyncio.AbstractEventLoop] = None
        # Invalidations Redis has not seen yet (see _MAX_PENDING_INVALIDATIONS).
        self._pending_keys: Set[str] = set()
        self._pending_patterns: Set[str] = set()
        self._pending_flush = False

        if not self._redis_enabled:
            logger.warning("Redis not available, using in-memory fallback cache")

    def _ensure_connecting(self) -> None:
        """Start a background connect if one is due. Never waits for it.

        Request paths call this, so a cold or black-holed Redis never adds
        a connect to a request: the call is served from memory and Redis is
        used from the next call once the background attempt succeeds.
        """
        if self._connected or not self._redis_enabled:
            return
        loop = asyncio.get_running_loop()
        task = self._connect_task
        if task is not None and not task.done() and self._connect_task_loop is loop:
            return  # one attempt in flight
        if loop.time() < self._next_connect_attempt:
            return  # breaker open
        self._next_connect_attempt = loop.time() + _RECONNECT_INTERVAL_SECONDS
        self._connect_task = loop.create_task(self._init_redis())
        self._connect_task_loop = loop

    async def connect(self) -> bool:
        """Connect now and wait for the result (startup, warm-up, health).

        Bounded by ``_REDIS_CONNECT_TIMEOUT_S``. Request paths use
        ``_ensure_connecting`` instead and never wait.
        """
        self._ensure_connecting()
        task = self._connect_task
        if (
            task is not None
            and not task.done()
            and self._connect_task_loop is asyncio.get_running_loop()
        ):
            await asyncio.shield(task)
        return self._connected

    def _trip(self, operation: str, exc: BaseException) -> None:
        """Open the breaker after a Redis failure: memory only for a window."""
        logger.warning(
            "Analytics Redis %s failed (%s); serving from memory for %.0fs",
            operation,
            type(exc).__name__,
            _RECONNECT_INTERVAL_SECONDS,
        )
        self._connected = False
        client, self.redis_client = self.redis_client, None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._next_connect_attempt = loop.time() + _RECONNECT_INTERVAL_SECONDS
        if client is not None:
            spawn(self._close_quietly(client), name="analytics-redis-close")

    @staticmethod
    async def _close_quietly(client: Any) -> None:
        try:
            close = getattr(client, "aclose", None) or client.close
            await close()
        except Exception:
            pass

    async def _init_redis(self) -> None:
        """Open and ping a client; on failure leave the cache in memory mode."""
        client = None
        try:
            client = redis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,  # Use string responses for JSON serialization
                socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_S,
                socket_timeout=_REDIS_OP_TIMEOUT_S,
                retry_on_timeout=False,  # a retry would double every bounded wait
                health_check_interval=30,
            )
            await client.ping()
            # Replay before serving from Redis, and mark connected with no
            # await in between, so no read sees a value deleted in the outage.
            await self._replay_invalidations(client)
            self.redis_client = client
            self._connected = True
            logger.info("Analytics cache connected to Redis")
        except Exception as e:
            logger.warning(
                "Analytics cache could not connect to Redis (%s); using memory",
                type(e).__name__,
            )
            self.redis_client = None
            self._connected = False
            if client is not None:
                await self._close_quietly(client)

    def _remember_invalidation(
        self, key: Optional[str] = None, pattern: Optional[str] = None
    ) -> None:
        """Queue a delete Redis missed, to replay on reconnect (bounded)."""
        if not self._redis_enabled or self._pending_flush:
            return
        if key is not None:
            self._pending_keys.add(key)
        if pattern is not None:
            self._pending_patterns.add(pattern)
        if (
            len(self._pending_keys) + len(self._pending_patterns)
            > _MAX_PENDING_INVALIDATIONS
        ):
            self._pending_keys.clear()
            self._pending_patterns.clear()
            self._pending_flush = True

    async def _replay_invalidations(self, client: Any) -> None:
        """Apply queued invalidations to Redis; on failure keep them queued.

        Loops until the queue is empty: a delete made while the replay awaits
        Redis is still memory-only, so it joins the queue and is replayed too.
        """
        while self._pending_flush or self._pending_keys or self._pending_patterns:
            flush = self._pending_flush
            keys, patterns = self._pending_keys, self._pending_patterns
            self._pending_flush = False
            self._pending_keys, self._pending_patterns = set(), set()
            try:
                if flush:
                    await self._delete_matching(client, _NAMESPACE_PATTERN)
                else:
                    pending = sorted(keys)
                    for i in range(0, len(pending), _SCAN_BATCH):
                        await client.delete(*pending[i : i + _SCAN_BATCH])
                    for pattern in sorted(patterns):
                        await self._delete_matching(client, pattern)
            except BaseException:
                if flush:
                    self._pending_flush = True
                    self._pending_keys.clear()
                    self._pending_patterns.clear()
                else:
                    for key in keys:
                        self._remember_invalidation(key=key)
                    for pattern in patterns:
                        self._remember_invalidation(pattern=pattern)
                raise
            if flush:
                logger.info("Analytics cache flushed its namespace after reconnect")
            else:
                logger.info(
                    "Analytics cache replayed %d key and %d pattern deletes "
                    "after reconnect",
                    len(keys),
                    len(patterns),
                )

    @staticmethod
    async def _delete_matching(client: Any, pattern: str) -> int:
        """Delete every Redis key matching ``pattern`` with SCAN, never KEYS.

        KEYS walks the whole keyspace in one blocking call, and this Redis
        is shared with the Celery broker.
        """
        deleted = 0
        batch: List[str] = []
        async for key in client.scan_iter(match=pattern, count=_SCAN_BATCH):
            batch.append(key)
            if len(batch) >= _SCAN_BATCH:
                deleted += await client.delete(*batch)
                batch = []
        if batch:
            deleted += await client.delete(*batch)
        return deleted

    def _get_fallback_key(self, key: str) -> str:
        """Get fallback cache key"""
        return f"fallback:{key}"

    def _is_fallback_expired(self, key: str) -> bool:
        """Check if fallback cache entry is expired"""
        expiry = self.fallback_cache_expiry.get(key)
        if expiry is None:
            return True

        return datetime.utcnow() > expiry

    def _cleanup_fallback_cache(self):
        """Clean up expired fallback cache entries"""
        now = datetime.utcnow()
        expired_keys = [
            key for key, expiry in self.fallback_cache_expiry.items() if now > expiry
        ]

        for key in expired_keys:
            self.fallback_cache.pop(key, None)
            self.fallback_cache_expiry.pop(key, None)

    async def get(self, key: str, use_fallback: bool = True) -> Any:
        """Get value from cache.

        While Redis answers it is the only source: a Redis miss is a miss,
        because this pod's local copy may be older than a delete or expiry
        another pod saw. The local copy serves only while Redis is down.
        """
        _require_namespaced(key)
        try:
            # Try Redis first
            self._ensure_connecting()
            if self.redis_client and self._connected:
                try:
                    value = await self.redis_client.get(key)
                except Exception as e:
                    self._trip("get", e)
                else:
                    if value is None:
                        return None
                    # Deserialize using JSON (secure, no arbitrary code execution)
                    try:
                        return json.loads(value)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(
                            f"Failed to deserialize cached value for key {key}: {e}"
                        )
                        return None

            # Redis unavailable: fall back to the in-memory cache
            if use_fallback:
                fallback_key = self._get_fallback_key(key)
                if not self._is_fallback_expired(fallback_key):
                    return self.fallback_cache.get(fallback_key)

            return None

        except Exception as e:
            logger.error(f"Cache get error for key {key}: {e}")
            return None

    async def set(
        self, key: str, value: Any, ttl: int = 300, use_fallback: bool = True
    ) -> bool:
        """Set value in cache"""
        _require_namespaced(key)
        try:
            # Try Redis first
            self._ensure_connecting()
            if self.redis_client and self._connected:
                try:
                    # Serialize value using JSON (secure, prevents arbitrary code execution)
                    try:
                        serialized = json.dumps(value, default=str)
                    except (TypeError, ValueError) as e:
                        logger.warning(f"Failed to serialize value for key {key}: {e}")
                        return False

                    jitter = int(ttl * 0.15)
                    effective_ttl = ttl + random.randint(-jitter, jitter)
                    await self.redis_client.setex(key, effective_ttl, serialized)

                    if use_fallback:
                        # Also set in fallback cache
                        fallback_key = self._get_fallback_key(key)
                        self.fallback_cache[fallback_key] = value
                        self.fallback_cache_expiry[fallback_key] = (
                            datetime.utcnow() + timedelta(seconds=ttl)
                        )

                    return True

                except Exception as e:
                    self._trip("set", e)

            # Redis did not take the write: drop its older value on reconnect.
            self._remember_invalidation(key=key)

            # Fallback to in-memory cache
            if use_fallback:
                fallback_key = self._get_fallback_key(key)
                self.fallback_cache[fallback_key] = value
                self.fallback_cache_expiry[fallback_key] = (
                    datetime.utcnow() + timedelta(seconds=ttl)
                )

                # Clean up old entries
                self._cleanup_fallback_cache()

                return True

            return False

        except Exception as e:
            logger.error(f"Cache set error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from cache; replayed on reconnect if Redis missed it."""
        _require_namespaced(key)
        try:
            success = True
            redis_done = False

            # Delete from Redis
            self._ensure_connecting()
            if self.redis_client and self._connected:
                try:
                    await self.redis_client.delete(key)
                    redis_done = True
                except Exception as e:
                    self._trip("delete", e)
                    success = False
            if not redis_done:
                self._remember_invalidation(key=key)

            # Delete from fallback cache
            fallback_key = self._get_fallback_key(key)
            self.fallback_cache.pop(fallback_key, None)
            self.fallback_cache_expiry.pop(fallback_key, None)

            return success

        except Exception as e:
            logger.error(f"Cache delete error for key {key}: {e}")
            return False

    async def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all cache keys matching a Redis glob ``pattern``.

        ``pattern`` must lie inside the ``analytics:`` namespace. Uses SCAN,
        never KEYS (see ``_delete_matching``). If Redis misses it, the
        pattern is replayed on reconnect.
        """
        _require_namespaced(pattern)
        try:
            deleted_count = 0
            redis_done = False

            self._ensure_connecting()
            if self.redis_client and self._connected:
                try:
                    deleted_count += await self._delete_matching(
                        self.redis_client, pattern
                    )
                    redis_done = True
                except Exception as e:
                    self._trip("invalidate", e)
            if not redis_done:
                self._remember_invalidation(pattern=pattern)

            # The in-memory fallback honours the same glob.
            matcher = _glob_regex(self._get_fallback_key(pattern))
            keys_to_delete = [key for key in self.fallback_cache if matcher.match(key)]
            for key in keys_to_delete:
                self.fallback_cache.pop(key, None)
                self.fallback_cache_expiry.pop(key, None)
                deleted_count += 1

            logger.info(
                f"Invalidated {deleted_count} cache entries matching pattern: {pattern}"
            )
            return deleted_count

        except Exception as e:
            logger.error(f"Cache pattern invalidation error: {e}")
            return 0

    async def get_analytics_data(
        self,
        organization_id: str,
        data_type: str,
        filters: Dict[str, Any],
        time_range: Optional[tuple] = None,
        ttl: Optional[int] = None,
    ) -> Optional[Any]:
        """Get analytics data from cache"""
        cache_key = CacheKey(
            prefix="analytics",
            organization_id=organization_id,
            data_type=data_type,
            filters=filters,
            time_range=time_range,
        )

        key = cache_key.generate_key()

        if ttl is None:
            ttl = self._get_default_ttl(data_type)

        return await self.get(key)

    async def set_analytics_data(
        self,
        organization_id: str,
        data_type: str,
        data: Any,
        filters: Dict[str, Any],
        time_range: Optional[tuple] = None,
        ttl: Optional[int] = None,
    ) -> bool:
        """Set analytics data in cache"""
        cache_key = CacheKey(
            prefix="analytics",
            organization_id=organization_id,
            data_type=data_type,
            filters=filters,
            time_range=time_range,
        )

        key = cache_key.generate_key()

        if ttl is None:
            ttl = self._get_default_ttl(data_type)

        return await self.set(key, data, ttl)

    def _get_default_ttl(self, data_type: str) -> int:
        """Get default TTL for data type"""
        ttl_mapping = {
            "realtime_metrics": CacheTTL.REALTIME_METRICS,
            "dashboard_data": CacheTTL.DASHBOARD_DATA,
            "aggregated_metrics": CacheTTL.AGGREGATED_METRICS,
            "user_behavior": CacheTTL.USER_BEHAVIOR,
            "quality_scores": CacheTTL.QUALITY_SCORES,
            "performance_summary": CacheTTL.PERFORMANCE_SUMMARY,
            "reports": CacheTTL.REPORTS,
            "exports": CacheTTL.EXPORTS,
        }

        return ttl_mapping.get(data_type, CacheTTL.AGGREGATED_METRICS)

    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        await self.connect()
        stats = {
            "redis_connected": self._connected,
            "fallback_cache_size": len(self.fallback_cache),
            "fallback_cache_keys": list(self.fallback_cache.keys()),
        }

        if self.redis_client and self._connected:
            try:
                info = await self.redis_client.info()
                stats.update(
                    {
                        "redis_used_memory": info.get("used_memory_human"),
                        "redis_connected_clients": info.get("connected_clients"),
                        "redis_total_commands": info.get("total_commands_processed"),
                        "redis_keyspace_hits": info.get("keyspace_hits", 0),
                        "redis_keyspace_misses": info.get("keyspace_misses", 0),
                    }
                )

                # Calculate hit rate
                hits = stats.get("redis_keyspace_hits", 0)
                misses = stats.get("redis_keyspace_misses", 0)
                total = hits + misses
                stats["redis_hit_rate"] = (hits / total * 100) if total > 0 else 0

            except Exception as e:
                logger.warning(f"Failed to get Redis stats: {e}")

        return stats

    async def health_check(self) -> Dict[str, Any]:
        """Perform cache health check"""
        await self.connect()
        health = {
            "status": "healthy",
            "redis_connected": self._connected,
            "fallback_available": True,
        }

        # Check Redis health
        if self.redis_client and self._connected:
            try:
                start_time = datetime.utcnow()
                await self.redis_client.ping()
                ping_time = (datetime.utcnow() - start_time).total_seconds() * 1000

                health["redis_ping_ms"] = ping_time
                health["redis_status"] = "healthy"

                if ping_time > 100:
                    health["redis_status"] = "degraded"

            except Exception as e:
                health["redis_status"] = "unhealthy"
                health["redis_error"] = str(e)
                health["status"] = "degraded"

        # Check fallback cache health
        try:
            # Per call: pods share Redis, so a fixed key races another pod's probe.
            test_key = f"{KEY_PREFIX}:health_check:{uuid.uuid4().hex}"
            test_value = {"timestamp": datetime.utcnow().isoformat()}

            await self.set(test_key, test_value, ttl=10)
            retrieved = await self.get(test_key)

            if retrieved == test_value:
                health["fallback_status"] = "healthy"
            else:
                health["fallback_status"] = "unhealthy"
                health["status"] = "degraded"

            await self.delete(test_key)

        except Exception as e:
            health["fallback_status"] = "unhealthy"
            health["fallback_error"] = str(e)
            health["status"] = "unhealthy"

        return health


# Global cache instance
_analytics_cache = AnalyticsCache()


def get_analytics_cache() -> AnalyticsCache:
    """Get the global analytics cache instance"""
    return _analytics_cache


def cached_analytics(data_type: str, ttl: Optional[int] = None):
    """Decorator for caching analytics function results"""

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract organization_id from kwargs or args
            organization_id = kwargs.get("organization_id")
            if not organization_id and args:
                organization_id = args[0]  # Assume first arg is organization_id

            if not organization_id:
                # Skip caching if no organization_id
                return await func(*args, **kwargs)

            # Create cache key from function args
            cache_kwargs = {k: v for k, v in kwargs.items() if k != "organization_id"}

            # Try to get from cache
            cache = get_analytics_cache()
            cached_result = await cache.get_analytics_data(
                organization_id=organization_id,
                data_type=data_type,
                filters=cache_kwargs,
                time_range=kwargs.get("time_range"),
                ttl=ttl,
            )

            if cached_result is not None:
                return cached_result

            # Execute function and cache result
            result = await func(*args, **kwargs)

            await cache.set_analytics_data(
                organization_id=organization_id,
                data_type=data_type,
                data=result,
                filters=cache_kwargs,
                time_range=kwargs.get("time_range"),
                ttl=ttl,
            )

            return result

        return wrapper

    return decorator


# Cache warming functions
async def warm_organization_cache(organization_id: str, days: int = 7):
    """Warm cache for an organization with common queries"""
    from datetime import datetime, timedelta

    cache = get_analytics_cache()

    # Common time ranges
    end_date = datetime.utcnow()
    start_dates = [
        end_date - timedelta(days=1),  # Last 24 hours
        end_date - timedelta(days=7),  # Last 7 days
        end_date - timedelta(days=30),  # Last 30 days
    ]

    # Common filters for different data types
    common_data_types = [
        "quality_metrics",
        "user_behavior",
        "performance_metrics",
        "dashboard_data",
    ]

    warmed_count = 0

    for data_type in common_data_types:
        for start_date in start_dates:
            filters = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }

            # Pre-populate cache (this would typically trigger the actual data loading)
            key = CacheKey(
                prefix="analytics",
                organization_id=organization_id,
                data_type=data_type,
                filters=filters,
                time_range=(start_date.isoformat(), end_date.isoformat()),
            ).generate_key()

            # Set empty placeholder to ensure cache key exists
            await cache.set(
                key,
                {"cached": True, "warmed_at": datetime.utcnow().isoformat()},
                ttl=300,
            )
            warmed_count += 1

    logger.info(
        f"Warmed {warmed_count} cache entries for organization {organization_id}"
    )
    return warmed_count

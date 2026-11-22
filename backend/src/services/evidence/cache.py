"""
Redis caching layer for Evidence Agreement Meter

Caches stance classifications and evidence meters for fast retrieval
"""

import hashlib
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ...core.config import settings

logger = logging.getLogger(__name__)


# After a connection failure, skip Redis for this long instead of paying a
# connect timeout on every request while it is down.
_UNAVAILABLE_BACKOFF_SECONDS = 30.0


class EvidenceCacheService:
    """Redis caching service for evidence meter data.

    Uses the asyncio Redis client (GOO-643/RS-1). The old sync client blocked
    the event loop on every call, PINGed before every operation and connected
    (5 s timeout) at import time, since the router builds this at import.
    Now nothing connects until the first operation, there is no per-call
    PING, and a connection failure backs off for
    ``_UNAVAILABLE_BACKOFF_SECONDS``. Every operation degrades to a cache miss.
    """

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self._unavailable_until = 0.0

    def _client(self) -> Optional[redis.Redis]:
        """The client, created lazily (no I/O), or None while backing off."""
        if time.monotonic() < self._unavailable_until:
            return None
        if self.redis_client is None:
            try:
                self.redis_client = redis.Redis.from_url(
                    settings.REDIS_URL or "redis://localhost:6379/0",
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                    health_check_interval=30,
                )
            except Exception as e:
                logger.error(f"Failed to create Redis client: {e}")
                self._mark_unavailable()
                return None
        return self.redis_client

    def _mark_unavailable(self) -> None:
        self._unavailable_until = time.monotonic() + _UNAVAILABLE_BACKOFF_SECONDS

    def _note_failure(self, error: Exception) -> None:
        if isinstance(error, (RedisConnectionError, RedisTimeoutError, OSError)):
            self._mark_unavailable()

    async def _ensure_connected(self) -> bool:
        """Health probe: one PING. Not called on the data path."""
        client = self._client()
        if client is None:
            return False
        try:
            await client.ping()
            return True
        except Exception as e:
            logger.warning(f"Redis ping failed for evidence cache: {e}")
            self._note_failure(e)
            return False

    async def _scan_keys(self, client: redis.Redis, pattern: str) -> List[Any]:
        """SCAN instead of KEYS, which blocks the Redis server on a big DB."""
        return [key async for key in client.scan_iter(match=pattern, count=500)]

    def _serialize_data(self, data: dict) -> str:
        """Serialize data for Redis storage"""
        return json.dumps(data, default=str, ensure_ascii=False)

    def _deserialize_data(self, data: str) -> dict:
        """Deserialize data from Redis storage"""
        return json.loads(data)

    # Stance classification caching

    async def get_stance_classification(self, cache_key: str) -> Optional[Dict]:
        """Get cached stance classification"""
        client = self._client()
        if client is None:
            return None

        try:
            data = await client.get(cache_key)
            if data:
                result = self._deserialize_data(data)
                logger.debug(f"Cache hit for stance classification: {cache_key}")
                return result
            return None
        except Exception as e:
            logger.error(f"Failed to get stance classification from cache: {e}")
            self._note_failure(e)
            return None

    async def set_stance_classification(
        self, cache_key: str, classification: Dict, ttl: int = 86400
    ) -> bool:
        """Set stance classification in cache"""
        client = self._client()
        if client is None:
            return False

        try:
            data = self._serialize_data(classification)
            jitter = int(ttl * 0.15)
            effective_ttl = ttl + random.randint(-jitter, jitter)
            result = await client.setex(cache_key, effective_ttl, data)
            logger.debug(f"Cached stance classification: {cache_key} (TTL: {ttl}s)")
            return bool(result)
        except Exception as e:
            logger.error(f"Failed to cache stance classification: {e}")
            self._note_failure(e)
            return False

    # Evidence meter caching

    def _generate_meter_cache_key(
        self,
        claim_hash: str,
        source_revisions: List[str],
        model_version: str,
        organization_id,
    ) -> str:
        """Generate cache key for evidence meter

        ``organization_id`` is part of the key so meter results are never shared across
        tenants — a cache hit for one org must not surface another org's analysis.
        """
        # Sort source revisions for consistent caching
        sorted_sources = sorted(source_revisions)
        sources_hash = hashlib.sha256(
            "|".join(sorted_sources).encode("utf-8")
        ).hexdigest()[:16]
        return f"meter:{organization_id}:{claim_hash}:{sources_hash}:{model_version}"

    async def get_evidence_meter(
        self,
        claim_hash: str,
        source_revisions: List[str],
        model_version: str,
        organization_id,
    ) -> Optional[Dict]:
        """Get cached evidence meter"""
        client = self._client()
        if client is None:
            return None

        cache_key = self._generate_meter_cache_key(
            claim_hash, source_revisions, model_version, organization_id
        )

        try:
            data = await client.get(cache_key)
            if data:
                result = self._deserialize_data(data)
                logger.debug(f"Cache hit for evidence meter: {cache_key}")
                return result
            return None
        except Exception as e:
            logger.error(f"Failed to get evidence meter from cache: {e}")
            self._note_failure(e)
            return None

    async def set_evidence_meter(
        self,
        claim_hash: str,
        source_revisions: List[str],
        model_version: str,
        organization_id,
        meter_data: Dict,
        ttl: int = 86400,
    ) -> bool:
        """Set evidence meter in cache"""
        client = self._client()
        if client is None:
            return False

        cache_key = self._generate_meter_cache_key(
            claim_hash, source_revisions, model_version, organization_id
        )

        try:
            data = self._serialize_data(meter_data)
            result = await client.setex(cache_key, ttl, data)
            logger.debug(f"Cached evidence meter: {cache_key} (TTL: {ttl}s)")
            return bool(result)
        except Exception as e:
            logger.error(f"Failed to cache evidence meter: {e}")
            self._note_failure(e)
            return False

    # Batch operations

    async def get_stance_classifications_batch(
        self, cache_keys: List[str]
    ) -> Dict[str, Optional[Dict]]:
        """Get multiple stance classifications from cache"""
        client = self._client()
        if client is None or not cache_keys:
            return {key: None for key in cache_keys}

        try:
            # Use mget for efficient batch retrieval
            results = await client.mget(cache_keys)

            cache_results = {}
            for i, key in enumerate(cache_keys):
                if results[i]:
                    try:
                        cache_results[key] = self._deserialize_data(results[i])
                    except Exception as e:
                        logger.warning(
                            f"Failed to deserialize cached data for {key}: {e}"
                        )
                        cache_results[key] = None
                else:
                    cache_results[key] = None

            hit_count = sum(1 for v in cache_results.values() if v is not None)
            logger.debug(f"Batch cache lookup: {hit_count}/{len(cache_keys)} hits")

            return cache_results

        except Exception as e:
            logger.error(f"Failed to get stance classifications batch: {e}")
            self._note_failure(e)
            return {key: None for key in cache_keys}

    async def set_stance_classifications_batch(
        self, classifications: Dict[str, Dict], ttl: int = 86400
    ) -> bool:
        """Set multiple stance classifications in cache"""
        client = self._client()
        if client is None or not classifications:
            return False

        try:
            pipe = client.pipeline()

            for cache_key, classification in classifications.items():
                data = self._serialize_data(classification)
                pipe.setex(cache_key, ttl, data)

            results = await pipe.execute()
            success_count = sum(1 for r in results if r)

            logger.debug(
                f"Batch cache set: {success_count}/{len(classifications)} successful"
            )
            return success_count == len(classifications)

        except Exception as e:
            logger.error(f"Failed to set stance classifications batch: {e}")
            self._note_failure(e)
            return False

    # Cache management

    async def invalidate_claim_cache(self, claim_hash: str) -> int:
        """Invalidate all cache entries for a claim"""
        client = self._client()
        if client is None:
            return 0

        try:
            # Find all keys for this claim
            patterns = [
                f"stance:{claim_hash}:*",
                f"meter:*:{claim_hash}:*",
            ]

            deleted_count = 0
            for pattern in patterns:
                keys = await self._scan_keys(client, pattern)
                if keys:
                    deleted = await client.delete(*keys)
                    deleted_count += deleted

            logger.info(
                f"Invalidated {deleted_count} cache entries for claim {claim_hash}"
            )
            return deleted_count

        except Exception as e:
            logger.error(f"Failed to invalidate claim cache: {e}")
            self._note_failure(e)
            return 0

    async def invalidate_source_cache(self, source_id: str) -> int:
        """Invalidate all cache entries for a source"""
        client = self._client()
        if client is None:
            return 0

        try:
            # Find stance classifications for this source
            pattern = f"stance:*:{source_id}:*"
            keys = await self._scan_keys(client, pattern)

            if keys:
                deleted = await client.delete(*keys)
                logger.info(
                    f"Invalidated {deleted} cache entries for source {source_id}"
                )
                return deleted

            return 0

        except Exception as e:
            logger.error(f"Failed to invalidate source cache: {e}")
            self._note_failure(e)
            return 0

    async def cleanup_old_model_versions(self, current_model_version: str) -> int:
        """Clean up cache entries from old model versions"""
        client = self._client()
        if client is None:
            return 0

        try:
            # Find all stance and meter keys
            all_keys = []
            for pattern in ["stance:*", "meter:*"]:
                keys = await self._scan_keys(client, pattern)
                all_keys.extend(keys)

            # Filter for old model versions
            old_keys = []
            for key in all_keys:
                if key.endswith(f":{current_model_version}"):
                    continue  # Keep current version
                if any(
                    key.endswith(f":{v}") for v in ["gpt-4o-mini", "gpt-4o", "gpt-3.5"]
                ):
                    old_keys.append(key)  # Mark old versions for deletion

            if old_keys:
                deleted = await client.delete(*old_keys)
                logger.info(
                    f"Cleaned up {deleted} cache entries from old model versions"
                )
                return deleted

            return 0

        except Exception as e:
            logger.error(f"Failed to cleanup old model versions: {e}")
            self._note_failure(e)
            return 0

    # Cache statistics

    async def get_cache_stats(self) -> Dict:
        """Get cache usage statistics"""
        client = self._client()
        if client is None:
            return {"error": "Redis not connected"}

        try:
            info = await client.info()

            # Count different key types
            stance_keys = len(await self._scan_keys(client, "stance:*"))
            meter_keys = len(await self._scan_keys(client, "meter:*"))

            return {
                "connected": True,
                "total_keys": (
                    info.get("db0", {}).get("keys", 0) if "db0" in info else 0
                ),
                "stance_classifications": stance_keys,
                "evidence_meters": meter_keys,
                "memory_usage": info.get("used_memory_human", "unknown"),
                "hit_rate": info.get("keyspace_hits", 0)
                / max(info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0), 1),
            }

        except Exception as e:
            logger.error(f"Failed to get cache stats: {e}")
            self._note_failure(e)
            return {"error": str(e)}

import os
from pathlib import Path
from typing import Optional
import asyncpg
import redis.asyncio as redis

# Global connection pools
_pg_pool: Optional[asyncpg.Pool] = None
_redis_client: Optional[redis.Redis] = None


async def init_db():
    """Initialize database connections."""
    global _pg_pool, _redis_client

    # Postgres
    database_url = os.getenv("DATABASE_URL", "postgresql://u1slicer:u1slicer@localhost:5432/u1slicer")
    _pg_pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)

    # Run schema migration
    # Split into individual statements to avoid asyncpg multi-statement issues
    async with _pg_pool.acquire() as conn:
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            schema_sql = schema_path.read_text()
            # Split on semicolons and execute each statement individually
            # Strip leading comment lines from each block before checking if it's empty
            raw_parts = [s.strip() for s in schema_sql.split(';') if s.strip()]
            statements = []
            for part in raw_parts:
                # Remove leading comment-only lines to get to actual SQL
                lines = part.split('\n')
                sql_lines = [l for l in lines if l.strip() and not l.strip().startswith('--')]
                if sql_lines:
                    statements.append(part)  # Execute the full block (Postgres handles comments)
            for statement in statements:
                try:
                    await conn.execute(statement)
                except Exception as e:
                    # Log but continue - some statements may fail if already applied
                    print(f"Schema statement failed (may be OK): {str(e)[:100]}")
                    print(f"Statement: {statement[:200]}")

    # Redis
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = redis.from_url(redis_url, decode_responses=True)


async def close_db():
    """Close database connections."""
    global _pg_pool, _redis_client

    if _pg_pool:
        await _pg_pool.close()
    if _redis_client:
        await _redis_client.close()


def get_pg_pool() -> asyncpg.Pool:
    """Get Postgres connection pool."""
    if _pg_pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _pg_pool


def get_redis() -> redis.Redis:
    """Get Redis client."""
    if _redis_client is None:
        raise RuntimeError("Redis not initialized. Call init_db() first.")
    return _redis_client


async def cache_gcode_layers(job_id: str, start: int, count: int, data: dict) -> None:
    """
    Cache extracted G-code layer data in Redis.

    Args:
        job_id: Slicing job ID
        start: Starting layer number
        count: Number of layers
        data: Layer data dictionary to cache
    """
    import json
    redis_client = get_redis()
    key = f"gcode_layers:{job_id}:{start}:{count}"
    # Cache for 24 hours (86400 seconds)
    await redis_client.setex(key, 86400, json.dumps(data))


async def get_cached_gcode_layers(job_id: str, start: int, count: int) -> Optional[dict]:
    """
    Get cached G-code layer data from Redis.

    Args:
        job_id: Slicing job ID
        start: Starting layer number
        count: Number of layers

    Returns:
        Cached layer data dict, or None if not cached
    """
    import json
    redis_client = get_redis()
    key = f"gcode_layers:{job_id}:{start}:{count}"
    cached = await redis_client.get(key)

    if cached:
        return json.loads(cached)
    return None

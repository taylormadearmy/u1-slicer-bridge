import os
from pathlib import Path
from typing import Optional
import asyncpg

# Global connection pool
_pg_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """Initialize database connection."""
    global _pg_pool

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


async def close_db():
    """Close database connection."""
    global _pg_pool

    if _pg_pool:
        await _pg_pool.close()


def get_pg_pool() -> asyncpg.Pool:
    """Get Postgres connection pool."""
    if _pg_pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _pg_pool

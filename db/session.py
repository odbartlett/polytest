"""Async SQLAlchemy session factory and database initialisation."""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config.settings import get_settings
from db.models import Base

logger = structlog.get_logger(__name__)

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Run SQLAlchemy DDL to create all tables (idempotent via checkfirst).

    For production migrations use the SQL files in db/migrations/ instead.
    This function is kept as a convenience for tests and fresh environments.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    # Idempotent column migrations — run on every startup, safe to re-run.
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE bot_positions ADD COLUMN IF NOT EXISTS "
            "strategy VARCHAR(16) NOT NULL DEFAULT 'COPY'"
        ))
        await conn.execute(text(
            "ALTER TABLE bot_orders ADD COLUMN IF NOT EXISTS "
            "strategy VARCHAR(16) NOT NULL DEFAULT 'COPY'"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_bot_positions_strategy "
            "ON bot_positions (strategy, status, is_simulated)"
        ))

    logger.info("database.initialized", tables=list(Base.metadata.tables.keys()))


async def close_db() -> None:
    """Dispose of the engine connection pool on shutdown."""
    await engine.dispose()
    logger.info("database.connection_pool_disposed")

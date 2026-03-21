"""SQLAlchemy 2.0 async ORM models for the Polymarket whale bot."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SideEnum(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class StrategyType(str, enum.Enum):
    COPY = "COPY"        # Standard copy-trade following a whale
    MICRO = "MICRO"      # Micro-position on thin-book markets ($10 fixed)
    NO_FLIP = "NO_FLIP"  # Contrarian NO-token buy when whale pushes YES >0.90


# Reusable SQLAlchemy Enum instances with names that match the SQL migrations.
# Using shared instances ensures consistent type names across all columns.
_SideEnumType = Enum(SideEnum, name="side_enum")
_PositionStatusType = Enum(PositionStatus, name="position_status")
_OrderStatusType = Enum(OrderStatus, name="order_status")


# ---------------------------------------------------------------------------
# WalletScore
# ---------------------------------------------------------------------------


class WalletScore(Base):
    """Computed whale scores for tracked wallets."""

    __tablename__ = "wallet_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    whale_score: Mapped[float] = mapped_column(Float, nullable=False)
    roi_score: Mapped[float] = mapped_column(Float, nullable=False)
    consistency_score: Mapped[float] = mapped_column(Float, nullable=False)
    sizing_score: Mapped[float] = mapped_column(Float, nullable=False)
    specialization_score: Mapped[float] = mapped_column(Float, nullable=False)
    recency_score: Mapped[float] = mapped_column(Float, nullable=False)
    total_volume_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    resolved_markets_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    best_category_win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="wallet_score_ref", lazy="noload")


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------


class Trade(Base):
    """All observed whale trades, streamed from the CLOB WebSocket."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[SideEnum] = mapped_column(_SideEnumType, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signal_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signal_reason_skipped: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # FK back to WalletScore (nullable — not all wallets are scored yet)
    wallet_score_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("wallet_scores.id", ondelete="SET NULL"), nullable=True
    )
    wallet_score_ref: Mapped[Optional[WalletScore]] = relationship(
        "WalletScore", back_populates="trades", lazy="noload"
    )


# ---------------------------------------------------------------------------
# BotPosition
# ---------------------------------------------------------------------------


class BotPosition(Base):
    """The bot's own copy-trade positions (live or simulated)."""

    __tablename__ = "bot_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    market_question: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    market_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[SideEnum] = mapped_column(_SideEnumType, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    shares_held: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    copied_from_wallet: Mapped[str] = mapped_column(String(42), nullable=False)
    whale_score_at_entry: Mapped[float] = mapped_column(Float, nullable=False)
    score_tier: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # "55-65","65-75","75-85","85+"
    status: Mapped[PositionStatus] = mapped_column(
        _PositionStatusType, nullable=False, default=PositionStatus.OPEN, index=True
    )
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="COPY", index=True)
    # Mark-to-market (updated periodically for open positions)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pnl_usdc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_marked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl_usdc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # Signal metadata for analysis
    signal_roi_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signal_consistency_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Relationships
    orders: Mapped[list["BotOrder"]] = relationship(
        "BotOrder", back_populates="position", lazy="noload"
    )


# ---------------------------------------------------------------------------
# BotOrder
# ---------------------------------------------------------------------------


class BotOrder(Base):
    """Individual CLOB limit orders placed by the bot."""

    __tablename__ = "bot_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bot_position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bot_positions.id", ondelete="CASCADE"), nullable=False
    )
    clob_order_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[SideEnum] = mapped_column(_SideEnumType, nullable=False)
    limit_price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        _OrderStatusType, nullable=False, default=OrderStatus.PENDING, index=True
    )
    strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="COPY")
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Relationship
    position: Mapped[BotPosition] = relationship("BotPosition", back_populates="orders", lazy="noload")


# ---------------------------------------------------------------------------
# DailyPnL
# ---------------------------------------------------------------------------


class DailyPnL(Base):
    """Daily profit-and-loss snapshots."""

    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # YYYY-MM-DD
    starting_bankroll: Mapped[float] = mapped_column(Float, nullable=False)
    ending_bankroll: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# SignalEvent (simulation funnel analytics)
# ---------------------------------------------------------------------------


class SignalEvent(Base):
    """Audit log of every signal evaluation — used for funnel analysis."""

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_question: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    whale_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_tier: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    trade_size_usdc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signal_result: Mapped[str] = mapped_column(String(32), nullable=False)  # EXECUTED/SKIPPED
    gate_failed: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    copy_size_usdc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bot_position_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("bot_positions.id", ondelete="SET NULL"), nullable=True
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# SimDailySnapshot (daily simulation performance)
# ---------------------------------------------------------------------------


class SimDailySnapshot(Base):
    """Daily simulation performance snapshots."""

    __tablename__ = "sim_daily_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # YYYY-MM-DD
    virtual_bankroll: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_pnl_per_trade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signals_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signals_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signals_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

"""Application settings — loaded from environment / .env file.

All secrets are validated at startup via Pydantic v2 BaseSettings.
Private keys are masked in all log output via the __repr__ override.

SIMULATION_MODE (default: True)
  When True the bot runs as a paper-trading / backtesting harness:
    - Connects to live Polymarket WebSocket + REST for real signal data
    - Never submits orders to the CLOB
    - Records virtual positions in Postgres and marks them to market
    - POLYMARKET_PRIVATE_KEY and wallet credentials are not required
    - Virtual bankroll starts at SIM_BANKROLL_USDC

  To enable live trading set SIMULATION_MODE=False and supply all
  POLYMARKET_* credentials. Live trading may be geo-restricted.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Simulation / paper-trading mode
    # -------------------------------------------------------------------------
    SIMULATION_MODE: bool = True

    # Virtual bankroll for paper trading
    SIM_BANKROLL_USDC: float = 10_000.0

    # Slippage applied to simulated fills (fraction added to best ask)
    SIM_FILL_SLIPPAGE: float = 0.001

    # How often (minutes) to mark open positions to market
    SIM_MARK_INTERVAL_MINUTES: int = 15

    # How often (hours) to send a performance report to Telegram
    SIM_REPORT_INTERVAL_HOURS: int = 6

    # Auto-close paper positions when the underlying market resolves
    SIM_AUTO_CLOSE_ON_RESOLUTION: bool = True

    # -------------------------------------------------------------------------
    # Polymarket CLOB credentials
    # Required for live mode; API keys are also used for WebSocket subscriptions
    # in sim mode to receive real whale trade events.
    # -------------------------------------------------------------------------
    POLYMARKET_API_KEY: Optional[str] = None
    POLYMARKET_API_SECRET: Optional[str] = None
    POLYMARKET_API_PASSPHRASE: Optional[str] = None
    POLYMARKET_PRIVATE_KEY: Optional[str] = None   # Only needed for live order signing
    POLYMARKET_WALLET_ADDRESS: Optional[str] = None
    CLOB_BASE_URL: str = "https://clob.polymarket.com"
    CLOB_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    # -------------------------------------------------------------------------
    # Bitquery
    # -------------------------------------------------------------------------
    BITQUERY_API_KEY: Optional[str] = None

    # -------------------------------------------------------------------------
    # Polygon RPC
    # -------------------------------------------------------------------------
    POLYGON_RPC_URL: Optional[str] = None

    # -------------------------------------------------------------------------
    # Database / Cache
    # -------------------------------------------------------------------------
    DATABASE_URL: str = "postgresql+asyncpg://botuser:botpassword@localhost:5432/polymarket_bot"
    REDIS_URL: str = "redis://localhost:6379"

    # -------------------------------------------------------------------------
    # Telegram (optional — if absent, alerts are logged only)
    # -------------------------------------------------------------------------
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # -------------------------------------------------------------------------
    # Risk parameters
    # -------------------------------------------------------------------------
    BANKROLL_USDC: float = 1000.0
    MAX_PER_MARKET_EXPOSURE_PCT: float = 0.05
    MAX_DRAWDOWN_PCT: float = 0.15
    SLIPPAGE_TOLERANCE_LIQUID: float = 0.02
    SLIPPAGE_TOLERANCE_THIN: float = 0.01
    ORDER_FILL_TIMEOUT_SECONDS: int = 90
    MIN_MARKET_OPEN_INTEREST: float = 150_000.0
    MIN_WHALE_TRADE_SIZE: float = 500.0
    MIN_COPY_SIZE: float = 50.0
    MIN_HOURS_TO_RESOLUTION: int = 6
    MAX_LIQUIDITY_CONSUMPTION_PCT: float = 0.20
    # Only copy trades where the token price implies real upside.
    # A token at $0.95 has $0.05 upside vs $0.95 downside — terrible odds.
    # A token at $0.10 has $0.90 upside vs $0.10 downside — worthwhile.
    MIN_ENTRY_PRICE: float = 0.05   # below this = near-zero probability, skip
    MAX_ENTRY_PRICE: float = 0.70   # above this = near-certain, no upside left
    MAX_HOURS_TO_RESOLUTION: int = 336    # 14 days — ignore markets resolving > 14 days out
    SIM_STOP_LOSS_PCT: float = 0.30       # auto-close position if unrealized loss > 30%
    SIM_TAKE_PROFIT_PCT: float = 0.50     # auto-close position if unrealized gain > 50%

    # -------------------------------------------------------------------------
    # Scoring thresholds
    # -------------------------------------------------------------------------
    WHALE_SCORE_FLOOR: float = 65.0
    WHALE_SCORE_REMOVAL: float = 45.0
    WHITELIST_MAX_SIZE: int = 75
    MIN_RESOLVED_MARKETS: int = 30
    MIN_TOTAL_VOLUME_USDC: float = 5_000.0
    MIN_TRADE_COUNT: int = 20
    LOOKBACK_DAYS: int = 90
    RECENCY_DECAY_LAMBDA: float = 0.02

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------
    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_db_url(cls, v: str) -> str:
        """Convert postgres:// or postgresql:// → postgresql+asyncpg:// for asyncpg.

        Railway (and many PaaS providers) supply the URL without the asyncpg
        dialect prefix. This validator normalises both common variants so the
        engine can always be created correctly regardless of how the URL is set.
        """
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @field_validator("POLYMARKET_PRIVATE_KEY", mode="before")
    @classmethod
    def strip_hex_prefix(cls, v: Optional[str]) -> Optional[str]:
        """Accept both '0x...' and raw hex strings."""
        if v is None:
            return None
        return v.removeprefix("0x").removeprefix("0X")

    @field_validator("POLYMARKET_WALLET_ADDRESS", mode="before")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError(f"Invalid Ethereum address: {v}")
        return v

    @model_validator(mode="after")
    def validate_mode_credentials(self) -> "Settings":
        if not self.SIMULATION_MODE:
            missing = [
                f for f in (
                    "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
                    "POLYMARKET_API_PASSPHRASE", "POLYMARKET_PRIVATE_KEY",
                    "POLYMARKET_WALLET_ADDRESS",
                )
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(
                    f"SIMULATION_MODE=False but missing required credentials: {', '.join(missing)}"
                )
        if self.TELEGRAM_BOT_TOKEN and not self.TELEGRAM_CHAT_ID:
            raise ValueError("TELEGRAM_CHAT_ID required when TELEGRAM_BOT_TOKEN is set")
        return self

    @model_validator(mode="after")
    def validate_risk_parameters(self) -> "Settings":
        if not 0 < self.MAX_PER_MARKET_EXPOSURE_PCT <= 1:
            raise ValueError("MAX_PER_MARKET_EXPOSURE_PCT must be between 0 and 1")
        if not 0 < self.MAX_DRAWDOWN_PCT <= 1:
            raise ValueError("MAX_DRAWDOWN_PCT must be between 0 and 1")
        return self

    @property
    def effective_bankroll(self) -> float:
        """SIM_BANKROLL_USDC in sim mode, BANKROLL_USDC in live mode."""
        return self.SIM_BANKROLL_USDC if self.SIMULATION_MODE else self.BANKROLL_USDC

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    # -------------------------------------------------------------------------
    # Security — mask secrets in repr / logs
    # -------------------------------------------------------------------------
    def __repr__(self) -> str:
        masked = self.model_dump()
        for secret_field in (
            "POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_SECRET",
            "POLYMARKET_API_PASSPHRASE", "TELEGRAM_BOT_TOKEN",
        ):
            if masked.get(secret_field):
                masked[secret_field] = "***REDACTED***"
        return f"Settings({masked})"

    def masked_dict(self) -> dict[str, Any]:
        """Return settings dict with secrets masked — safe to log."""
        d = self.model_dump()
        for field in (
            "POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_SECRET",
            "POLYMARKET_API_PASSPHRASE", "TELEGRAM_BOT_TOKEN",
            "BITQUERY_API_KEY", "POLYMARKET_API_KEY",
        ):
            if d.get(field):
                d[field] = "***REDACTED***"
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton."""
    return Settings()

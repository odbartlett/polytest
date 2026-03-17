-- =============================================================================
-- Migration 002: Simulation / paper-trading columns
-- Run after 001_initial.sql.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Extend bot_positions for paper trading
-- ---------------------------------------------------------------------------

ALTER TABLE bot_positions
    ADD COLUMN IF NOT EXISTS market_question       VARCHAR(512),
    ADD COLUMN IF NOT EXISTS market_category       VARCHAR(64),
    ADD COLUMN IF NOT EXISTS score_tier            VARCHAR(16),     -- '55-65','65-75','75-85','85+'
    ADD COLUMN IF NOT EXISTS is_simulated          BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS current_price         DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS unrealized_pnl_usdc   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS last_marked_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS exit_price            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS signal_roi_score      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS signal_consistency_score DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_bot_positions_simulated
    ON bot_positions (is_simulated, status);

CREATE INDEX IF NOT EXISTS idx_bot_positions_opened_at
    ON bot_positions (opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_bot_positions_score_tier
    ON bot_positions (score_tier) WHERE is_simulated = TRUE;

CREATE INDEX IF NOT EXISTS idx_bot_positions_copied_wallet
    ON bot_positions (copied_from_wallet, is_simulated);

-- ---------------------------------------------------------------------------
-- Signal funnel audit table — tracks every signal evaluation result
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signal_events (
    id               BIGSERIAL PRIMARY KEY,
    wallet_address   VARCHAR(42) NOT NULL,
    market_id        VARCHAR(128) NOT NULL,
    market_question  VARCHAR(512),
    whale_score      DOUBLE PRECISION,
    score_tier       VARCHAR(16),
    trade_size_usdc  DOUBLE PRECISION,
    signal_result    VARCHAR(32) NOT NULL,   -- 'EXECUTED','SKIPPED','RISK_REJECTED'
    gate_failed      VARCHAR(64),            -- NULL when executed
    skip_reason      TEXT,
    copy_size_usdc   DOUBLE PRECISION,       -- NULL when skipped
    bot_position_id  BIGINT REFERENCES bot_positions(id) ON DELETE SET NULL,
    evaluated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_events_wallet
    ON signal_events (wallet_address, evaluated_at DESC);

CREATE INDEX IF NOT EXISTS idx_signal_events_gate
    ON signal_events (gate_failed) WHERE gate_failed IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signal_events_result
    ON signal_events (signal_result, evaluated_at DESC);

-- ---------------------------------------------------------------------------
-- Daily sim performance snapshots
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sim_daily_snapshots (
    id                   BIGSERIAL PRIMARY KEY,
    date                 VARCHAR(10) NOT NULL UNIQUE,  -- YYYY-MM-DD
    virtual_bankroll     DOUBLE PRECISION NOT NULL,
    realized_pnl         DOUBLE PRECISION NOT NULL DEFAULT 0,
    unrealized_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_pnl            DOUBLE PRECISION NOT NULL DEFAULT 0,
    open_positions       INTEGER NOT NULL DEFAULT 0,
    closed_positions     INTEGER NOT NULL DEFAULT 0,
    win_count            INTEGER NOT NULL DEFAULT 0,
    loss_count           INTEGER NOT NULL DEFAULT 0,
    win_rate             DOUBLE PRECISION,
    avg_pnl_per_trade    DOUBLE PRECISION,
    signals_evaluated    INTEGER NOT NULL DEFAULT 0,
    signals_executed     INTEGER NOT NULL DEFAULT 0,
    signals_skipped      INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;

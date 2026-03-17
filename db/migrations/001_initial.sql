-- =============================================================================
-- Migration 001: Initial schema
-- Run once against a fresh Postgres 15 database.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE side_enum AS ENUM ('BUY', 'SELL');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE position_status AS ENUM ('OPEN', 'CLOSED', 'CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE order_status AS ENUM ('PENDING', 'FILLED', 'CANCELLED', 'EXPIRED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- wallet_scores
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wallet_scores (
    id                     BIGSERIAL PRIMARY KEY,
    wallet_address         VARCHAR(42)  NOT NULL,
    whale_score            DOUBLE PRECISION NOT NULL,
    roi_score              DOUBLE PRECISION NOT NULL,
    consistency_score      DOUBLE PRECISION NOT NULL,
    sizing_score           DOUBLE PRECISION NOT NULL,
    specialization_score   DOUBLE PRECISION NOT NULL,
    recency_score          DOUBLE PRECISION NOT NULL,
    total_volume_usdc      DOUBLE PRECISION NOT NULL DEFAULT 0,
    resolved_markets_count INTEGER NOT NULL DEFAULT 0,
    win_count              INTEGER NOT NULL DEFAULT 0,
    best_category          VARCHAR(64),
    best_category_win_rate DOUBLE PRECISION,
    last_scored_at         TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_scores_address
    ON wallet_scores (wallet_address);

CREATE INDEX IF NOT EXISTS idx_wallet_scores_score_desc
    ON wallet_scores (whale_score DESC);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_wallet_scores_updated_at ON wallet_scores;
CREATE TRIGGER trg_wallet_scores_updated_at
    BEFORE UPDATE ON wallet_scores
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- trades
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trades (
    id                    BIGSERIAL PRIMARY KEY,
    wallet_address        VARCHAR(42) NOT NULL,
    market_id             VARCHAR(128) NOT NULL,
    token_id              VARCHAR(128) NOT NULL,
    side                  side_enum NOT NULL,
    price                 DOUBLE PRECISION NOT NULL,
    size_usdc             DOUBLE PRECISION NOT NULL,
    timestamp             TIMESTAMPTZ NOT NULL,
    signal_generated      BOOLEAN NOT NULL DEFAULT FALSE,
    signal_reason_skipped TEXT,
    wallet_score_id       BIGINT REFERENCES wallet_scores(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_wallet_timestamp
    ON trades (wallet_address, timestamp);

CREATE INDEX IF NOT EXISTS idx_trades_market_id
    ON trades (market_id);

CREATE INDEX IF NOT EXISTS idx_trades_signal_generated
    ON trades (signal_generated) WHERE signal_generated = TRUE;

-- ---------------------------------------------------------------------------
-- bot_positions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bot_positions (
    id                   BIGSERIAL PRIMARY KEY,
    market_id            VARCHAR(128) NOT NULL,
    token_id             VARCHAR(128) NOT NULL,
    side                 side_enum NOT NULL,
    entry_price          DOUBLE PRECISION NOT NULL,
    size_usdc            DOUBLE PRECISION NOT NULL,
    shares_held          DOUBLE PRECISION NOT NULL DEFAULT 0,
    copied_from_wallet   VARCHAR(42) NOT NULL,
    whale_score_at_entry DOUBLE PRECISION NOT NULL,
    status               position_status NOT NULL DEFAULT 'OPEN',
    opened_at            TIMESTAMPTZ NOT NULL,
    closed_at            TIMESTAMPTZ,
    realized_pnl_usdc    DOUBLE PRECISION,
    exit_reason          VARCHAR(256)
);

CREATE INDEX IF NOT EXISTS idx_bot_positions_status_market
    ON bot_positions (status, market_id);

CREATE INDEX IF NOT EXISTS idx_bot_positions_wallet
    ON bot_positions (copied_from_wallet);

-- ---------------------------------------------------------------------------
-- bot_orders
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bot_orders (
    id               BIGSERIAL PRIMARY KEY,
    bot_position_id  BIGINT NOT NULL REFERENCES bot_positions(id) ON DELETE CASCADE,
    clob_order_id    VARCHAR(256) NOT NULL,
    market_id        VARCHAR(128) NOT NULL,
    token_id         VARCHAR(128) NOT NULL,
    side             side_enum NOT NULL,
    limit_price      DOUBLE PRECISION NOT NULL,
    size_usdc        DOUBLE PRECISION NOT NULL,
    status           order_status NOT NULL DEFAULT 'PENDING',
    placed_at        TIMESTAMPTZ NOT NULL,
    filled_at        TIMESTAMPTZ,
    fill_price       DOUBLE PRECISION
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_orders_clob_order_id
    ON bot_orders (clob_order_id);

CREATE INDEX IF NOT EXISTS idx_bot_orders_status_placed
    ON bot_orders (status, placed_at);

-- ---------------------------------------------------------------------------
-- daily_pnl
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS daily_pnl (
    id                BIGSERIAL PRIMARY KEY,
    date              VARCHAR(10) NOT NULL,          -- YYYY-MM-DD
    starting_bankroll DOUBLE PRECISION NOT NULL,
    ending_bankroll   DOUBLE PRECISION NOT NULL,
    realized_pnl      DOUBLE PRECISION NOT NULL DEFAULT 0,
    unrealized_pnl    DOUBLE PRECISION NOT NULL DEFAULT 0,
    trade_count       INTEGER NOT NULL DEFAULT 0,
    win_count         INTEGER NOT NULL DEFAULT 0,
    loss_count        INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_pnl_date
    ON daily_pnl (date);

COMMIT;

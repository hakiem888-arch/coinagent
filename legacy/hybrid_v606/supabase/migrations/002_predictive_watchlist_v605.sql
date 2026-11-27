-- =============================================================================
-- CoinAgentX v6.0.5 — Predictive Watchlist v1.0 Database Migration
-- Idempotent PostgreSQL & Supabase Schema Migration
-- =============================================================================
-- Run in Supabase SQL Editor (Role: postgres) or via:
--   psql "$DATABASE_URL" -f supabase/migrations/002_predictive_watchlist_v605.sql
--
-- Security & Architecture Guarantees:
-- 1. Idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).
-- 2. Strict `service_role`-only grants; REVOKES all privileges from PUBLIC,
--    anon, and authenticated, and ENABLES + FORCES Row Level Security (RLS).
-- 3. Unique constraints prevent duplicate active signals and duplicate Telegram
--    outbox alerts across worker restarts.
-- =============================================================================

-- 0. Schema Migrations Tracking Table
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at_ms BIGINT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- 1. Predictive Signals Table (Active + Historical Signals)
CREATE TABLE IF NOT EXISTS public.predictive_signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    stage TEXT NOT NULL,
    signal_state TEXT NOT NULL DEFAULT 'ACTIVE',
    early_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    setup_quality_score DOUBLE PRECISION,
    confirmation_score DOUBLE PRECISION,
    detected_price DOUBLE PRECISION NOT NULL,
    trigger_price DOUBLE PRECISION NOT NULL,
    invalidation_price DOUBLE PRECISION NOT NULL,
    current_price DOUBLE PRECISION NOT NULL,
    distance_to_trigger_pct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    provider TEXT NOT NULL,
    instrument_kind TEXT NOT NULL DEFAULT 'USD_M_PERPETUAL',
    m15_regime TEXT NOT NULL DEFAULT 'UNKNOWN',
    provisional_context TEXT NOT NULL DEFAULT '',
    strategy_id TEXT NOT NULL DEFAULT '',
    confidence_label TEXT NOT NULL DEFAULT 'PROVISIONAL_SETUP_QUALITY',
    reason_codes TEXT NOT NULL DEFAULT '[]',
    detected_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    expires_at_ms BIGINT NOT NULL,
    alert_state TEXT NOT NULL DEFAULT 'PENDING',
    dedup_key TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_predictive_signals_dedup_key
    ON public.predictive_signals (dedup_key);

CREATE INDEX IF NOT EXISTS idx_predictive_signals_symbol_state
    ON public.predictive_signals (symbol, signal_state, updated_at_ms DESC);

CREATE INDEX IF NOT EXISTS idx_predictive_signals_stage_updated
    ON public.predictive_signals (stage, updated_at_ms DESC);

-- 2. Predictive Signal Lifecycle Events Table (Immutable Audit Trail)
CREATE TABLE IF NOT EXISTS public.predictive_signal_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    from_stage TEXT NOT NULL DEFAULT '',
    to_stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    early_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    setup_quality_score DOUBLE PRECISION,
    price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    provider TEXT NOT NULL DEFAULT '',
    at_ms BIGINT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT uq_predictive_signal_events_event_id UNIQUE (event_id)
);

CREATE INDEX IF NOT EXISTS idx_predictive_signal_events_signal_id
    ON public.predictive_signal_events (signal_id, at_ms DESC);

-- 3. Predictive Alert Outbox Table (Transactional Deduplicated Telegram Delivery)
CREATE TABLE IF NOT EXISTS public.predictive_alert_outbox (
    outbox_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    delivery_state TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at_ms BIGINT NOT NULL DEFAULT 0,
    created_at_ms BIGINT NOT NULL,
    sent_at_ms BIGINT,
    last_error TEXT NOT NULL DEFAULT '',
    message_text TEXT NOT NULL,
    CONSTRAINT uq_predictive_alert_outbox_dedup_key UNIQUE (dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_predictive_alert_outbox_state_retry
    ON public.predictive_alert_outbox (delivery_state, next_retry_at_ms ASC);

-- 4. Revoke PUBLIC / anon / authenticated & Grant service_role ONLY
REVOKE ALL ON TABLE
    public.schema_migrations,
    public.predictive_signals,
    public.predictive_signal_events,
    public.predictive_alert_outbox
FROM PUBLIC, anon, authenticated;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.schema_migrations,
    public.predictive_signals,
    public.predictive_signal_events,
    public.predictive_alert_outbox
TO service_role;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- 5. Enable & FORCE Row Level Security (RLS)
DO $$
DECLARE
    tbl TEXT;
    pol_name TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'schema_migrations',
        'predictive_signals',
        'predictive_signal_events',
        'predictive_alert_outbox'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY;', tbl);
        pol_name := 'service_role_only_' || tbl;
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = tbl
              AND policyname = pol_name
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL TO service_role USING (true) WITH CHECK (true);',
                pol_name,
                tbl
            );
        END IF;
    END LOOP;
END
$$;

INSERT INTO public.schema_migrations (version, applied_at_ms, description)
VALUES (
    '002_predictive_watchlist_v605',
    (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
    'CoinAgentX v6.0.5 Predictive Watchlist v1.0 tables (predictive_signals, predictive_signal_events, predictive_alert_outbox)'
)
ON CONFLICT (version) DO NOTHING;

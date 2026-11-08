-- =============================================================================
-- ForkMark Banking Enterprise: PostgreSQL Audit Log Sequence Partitioning
-- =============================================================================
-- High-throughput banks processing thousands of daily LLM inference evaluations
-- benefit from RANGE partitioning on supervisory audit records.
--
-- CRITICAL FOR TAMPER-EVIDENCE:
-- 1. `ts` MUST remain `TEXT NOT NULL` (ISO-8601 string) because it is an input
--    to the SHA-256 entry_hash calculation. Casting to TIMESTAMPTZ alters the
--    string representation on retrieval, which invalidates the audit hash chain.
-- 2. Partitioning is performed by `RANGE (seq)` so that PostgreSQL allows the
--    mandatory UNIQUE constraint on `seq` (idx_audit_log_seq), preventing
--    sequence duplication under concurrent transactions.
-- =============================================================================

-- Step 1: Create the sequence-partitioned audit_log table
CREATE TABLE IF NOT EXISTS audit_log_partitioned (
    id            TEXT NOT NULL,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'system',
    actor_role    TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL,
    resource_type TEXT NOT NULL DEFAULT '',
    resource_id   TEXT NOT NULL DEFAULT '',
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip            TEXT NOT NULL DEFAULT '',
    seq           BIGINT NOT NULL,
    prev_hash     TEXT NOT NULL DEFAULT '',
    entry_hash    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (seq, id)
) PARTITION BY RANGE (seq);

-- Step 2: Mandatory UNIQUE index on seq across partitions
-- Because seq is the partition key, PostgreSQL enforces global uniqueness across all partitions.
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_log_seq_part ON audit_log_partitioned(seq);

-- Step 3: Create sequence partitions (1M records per partition)
CREATE TABLE IF NOT EXISTS audit_log_p1 PARTITION OF audit_log_partitioned
    FOR VALUES FROM (1) TO (1000000);

CREATE TABLE IF NOT EXISTS audit_log_p2 PARTITION OF audit_log_partitioned
    FOR VALUES FROM (1000000) TO (2000000);

CREATE TABLE IF NOT EXISTS audit_log_p3 PARTITION OF audit_log_partitioned
    FOR VALUES FROM (2000000) TO (3000000);

CREATE TABLE IF NOT EXISTS audit_log_p4 PARTITION OF audit_log_partitioned
    FOR VALUES FROM (3000000) TO (4000000);

CREATE TABLE IF NOT EXISTS audit_log_p5 PARTITION OF audit_log_partitioned
    FOR VALUES FROM (4000000) TO (5000000);

-- Default partition catches sequences beyond initial pre-allocated ranges
CREATE TABLE IF NOT EXISTS audit_log_default PARTITION OF audit_log_partitioned DEFAULT;

-- Step 4: Supporting supervisory query indexes
CREATE INDEX IF NOT EXISTS idx_audit_part_ts ON audit_log_partitioned(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_part_resource ON audit_log_partitioned(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_part_action ON audit_log_partitioned(action);
CREATE INDEX IF NOT EXISTS idx_audit_part_actor ON audit_log_partitioned(actor);

-- Step 5: Data migration from legacy unpartitioned audit_log to partitioned table
-- (Executed during planned maintenance window. Exact string preservation of ts preserves SHA-256 chain)
--
-- INSERT INTO audit_log_partitioned (id, ts, actor, actor_role, action, resource_type, resource_id, detail, ip, seq, prev_hash, entry_hash)
-- SELECT id, ts, actor, actor_role, action, resource_type, resource_id, detail, ip, seq, prev_hash, entry_hash
-- FROM audit_log;
--
-- Step 6: Atomic table and index swap
-- Renaming the legacy index first prevents relation name collisions under ON_ERROR_STOP=1
--
-- BEGIN;
-- ALTER TABLE IF EXISTS audit_log RENAME TO audit_log_legacy;
-- ALTER INDEX IF EXISTS idx_audit_log_seq RENAME TO idx_audit_log_seq_legacy;
-- ALTER TABLE audit_log_partitioned RENAME TO audit_log;
-- ALTER INDEX idx_audit_log_seq_part RENAME TO idx_audit_log_seq;
-- COMMIT;

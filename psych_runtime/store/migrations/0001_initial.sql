-- 0001_initial.sql
--
-- The three tables PostgresStore needs. Migrations are sequential and
-- forward-only (DESIGN.md §22): this file is never edited once a
-- later one exists, and applying it is always an explicit call the consumer
-- makes (PostgresStore.migrate()), never something that happens on connect.
--
-- No transactions, no joins, no `SELECT ... FOR UPDATE` (DESIGN.md §7). Every
-- operation the adapter performs against these tables is one conditional
-- INSERT or UPDATE, which is what keeps this schema semantically
-- interchangeable with the MySQL and DynamoDB adapters.

-- The append-only log. `(run_id, seq)` as the primary key is the whole of the
-- conditional-append mechanism: a duplicate append fails the constraint
-- instead of racing a read-then-write check.
CREATE TABLE psych_records (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (run_id, seq)
);

-- Published Versions. The hash is the identity, so a second `put_version` for
-- a hash already present is a no-op rather than an overwrite.
CREATE TABLE psych_versions (
    hash TEXT PRIMARY KEY,
    version JSONB NOT NULL
);

-- Run headers: the mutable row beside each Run's immutable log. Columns
-- mirror `psych.store.port.RunHeader` field for field rather than folding it
-- into one JSONB blob, because `claim()`, `expired_leases()` and
-- `overdue_deadlines()` all filter and order on these fields and a JSONB path
-- expression cannot be indexed as cheaply or as legibly as a real column.
CREATE TABLE psych_runs (
    run_id TEXT PRIMARY KEY,
    scope JSONB NOT NULL,
    version_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('runnable', 'running', 'suspended', 'settled')),
    created_at TIMESTAMPTZ NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    lease_holder TEXT,
    lease_expires_at TIMESTAMPTZ,
    idempotency_key TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    runnable_at TIMESTAMPTZ,
    parent_run_id TEXT,
    delegation_depth INTEGER NOT NULL DEFAULT 0
);

-- Idempotent admission (DESIGN.md §8.3): a second `create_run` under a key
-- already used must find the first Run rather than create a second one. Only
-- one row may hold a given non-null key; NULL keys are unconstrained since
-- most Runs are admitted without one.
CREATE UNIQUE INDEX psych_runs_idempotency_key_idx
    ON psych_runs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- What `claim()` and `expired_leases()` scan: every runnable or running Run,
-- ordered so the supervisor and the claimer see the same set without a
-- sequential scan over settled and suspended history.
CREATE INDEX psych_runs_claimable_idx
    ON psych_runs (state, lease_expires_at)
    WHERE state IN ('runnable', 'running');

-- What `overdue_deadlines()` scans: live Runs past their deadline. Settled
-- Runs never age out of this index because they are excluded from it, not
-- because anything prunes them.
CREATE INDEX psych_runs_deadline_idx
    ON psych_runs (deadline_at)
    WHERE state <> 'settled';

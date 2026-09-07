-- 0001_initial.sql
--
-- Schema for the MySQL/MariaDB Store adapter (psych.store.mysql.MySQLStore).
-- Kept separate from the PostgreSQL migrations: the dialects differ enough
-- (JSON handling, no RETURNING, DATETIME precision) that sharing one file
-- would leave neither readable.
--
-- DATETIME(6) everywhere a lease or a deadline is compared. Plain DATETIME
-- truncates to whole seconds, and the store contract suite claims and renews
-- leases at sub-second resolution, so a bare DATETIME column would silently
-- round two distinct instants to the same second and break every ordering
-- assumption claim() and renew() depend on.
--
-- IF NOT EXISTS makes this file safe to apply repeatedly against a
-- long-lived test database, since nothing here ever runs against a
-- consumer's database automatically (psych.store never provisions one).

CREATE TABLE IF NOT EXISTS records (
    run_id VARCHAR(64) NOT NULL,
    seq INT NOT NULL,
    record JSON NOT NULL,
    PRIMARY KEY (run_id, seq)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS versions (
    version_hash VARCHAR(128) NOT NULL,
    version JSON NOT NULL,
    published_at DATETIME(6) NOT NULL,
    PRIMARY KEY (version_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(64) NOT NULL,
    scope JSON NOT NULL,
    version_hash VARCHAR(128) NOT NULL,
    state VARCHAR(16) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    deadline_at DATETIME(6) NOT NULL,
    lease_holder VARCHAR(64) NULL,
    lease_expires_at DATETIME(6) NULL,
    idempotency_key VARCHAR(256) NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    runnable_at DATETIME(6) NULL,
    parent_run_id VARCHAR(64) NULL,
    delegation_depth INT NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id),
    -- Multiple NULLs are allowed under a UNIQUE key in MySQL, so runs with no
    -- idempotency key never collide with each other here.
    UNIQUE KEY uq_runs_idempotency_key (idempotency_key),
    -- claim()'s candidate scan: runnable, or running with an expired lease,
    -- gated by runnable_at. state leads so the index stays selective once
    -- most rows in a long-lived table are settled.
    KEY idx_runs_claimable (state, runnable_at, lease_expires_at),
    -- expired_leases(): running rows ordered by how overdue the lease is.
    KEY idx_runs_lease_expiry (state, lease_expires_at),
    -- overdue_deadlines(): live rows ordered by how overdue the deadline is.
    KEY idx_runs_deadline (state, deadline_at)
) ENGINE=InnoDB;

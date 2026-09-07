-- 0003_tenant_scoped_idempotency.sql
--
-- An idempotency key is unique per tenant, not globally. See the PostgreSQL
-- migration of the same number for why.
--
-- MySQL cannot index a JSON path directly, so the tenant is materialised as a
-- generated column and the unique key covers that plus the key. Generated
-- STORED rather than VIRTUAL because a UNIQUE index over a virtual column is
-- not supported on every supported server version.

ALTER TABLE runs
    ADD COLUMN tenant VARCHAR(191)
        GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(scope, '$.tenant'))) STORED;

ALTER TABLE runs DROP INDEX uq_runs_idempotency_key;

ALTER TABLE runs
    ADD UNIQUE KEY uq_runs_tenant_idempotency_key (tenant, idempotency_key);

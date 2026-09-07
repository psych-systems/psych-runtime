-- 0003_tenant_scoped_idempotency.sql
--
-- An idempotency key is unique per tenant, not globally.
--
-- 0001 made `idempotency_key` unique across the whole table. Keys are chosen
-- by the consumer and collide naturally across tenants ("order-1234", a
-- webhook delivery id), and a collision handed the second tenant the first
-- tenant's run_id back from create_run, from which every read entry point
-- (stream, records, state, report) and resume/interrupt would have worked.
-- That is a cross-tenant read reachable by guessing a key.
--
-- `scope->>'tenant'` rather than a new column: the tenant is already in the
-- scope JSONB every row carries, and duplicating it into a column would give
-- two places for it to disagree.

DROP INDEX IF EXISTS psych_runs_idempotency_key_idx;

CREATE UNIQUE INDEX psych_runs_tenant_idempotency_key_idx
    ON psych_runs ((scope->>'tenant'), idempotency_key)
    WHERE idempotency_key IS NOT NULL;

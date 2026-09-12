-- 0004_nested_run_state.sql
--
-- `nested` is a run state, and this table would not accept one.
--
-- 0001 wrote the check constraint when there were four states. `nested` came
-- later, for a Run executed inline by the Attempt that dispatched it -- a
-- subagent -- so that no Worker could claim a log its parent was already
-- writing. The constraint was never widened, so `create_run` on a nested Run
-- raised a check violation here and every inline delegation failed against
-- PostgreSQL while passing against the in-memory adapter, which enforces no
-- such list.
--
-- Rewritten rather than dropped: the point of the constraint is that a state
-- outside the enum is a bug in an adapter, and that is still worth catching.

ALTER TABLE psych_runs DROP CONSTRAINT IF EXISTS psych_runs_state_check;

ALTER TABLE psych_runs
    ADD CONSTRAINT psych_runs_state_check
    CHECK (state IN ('runnable', 'running', 'suspended', 'nested', 'settled'));

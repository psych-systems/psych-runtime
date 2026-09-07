-- 0002_thread_continuation.sql
--
-- A chat thread is a chain of Runs, each continuing the last. This
-- adds the one column `RunHeader.continues_run_id` needs so a continuation's
-- Scope can be checked with one row read instead of a full log replay -- the
-- log itself (`RunAdmitted.continues_run_id`) stays the record, and this
-- column mirrors it, same as `parent_run_id` already is for delegation.
--
-- Forward-only (DESIGN.md §22): 0001 is never edited once a later
-- migration exists, this file is applied on top of it.

ALTER TABLE psych_runs ADD COLUMN continues_run_id TEXT;

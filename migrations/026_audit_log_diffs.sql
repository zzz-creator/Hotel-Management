-- Migration: 026_audit_log_diffs.sql
-- AuditLog records old/new values, not just a free-text sentence.
--
-- The problem:
--   AuditLog stored the free-text the caller passed to log_audit() and nothing
--   else. A rate edit or a setting change could say "something about the rate
--   happened on Tuesday" but never "the rate was 120.00 before and 135.00 after"
--   -- the before-image was lost. Forensically weak: the log cannot answer
--   "what was this value before Tuesday?" without re-reading a database that
--   has since been overwritten.
--
-- The fix:
--   Two nullable columns on AuditLog. log_audit() accepts old_value / new_value
--   and stores them; the caller owns the before-image, because only it knows what
--   changed (re-reading the row inside log_audit would be wrong for a re-let,
--   where the old row has already been archived by the time the audit call runs).
--   Both columns are nullable so the ~60 existing call sites keep working; each
--   can opt in by passing the diff where the old value is already in scope.
--
-- Safe to re-run (idempotent): COL_LENGTH guards skip columns that exist.
-- Apply from SSMS after migration 025.
USE hotelSystem
GO

IF COL_LENGTH('dbo.AuditLog', 'OldValue') IS NULL
    ALTER TABLE dbo.AuditLog ADD OldValue NVARCHAR(500) NULL;
GO

IF COL_LENGTH('dbo.AuditLog', 'NewValue') IS NULL
    ALTER TABLE dbo.AuditLog ADD NewValue NVARCHAR(500) NULL;
GO

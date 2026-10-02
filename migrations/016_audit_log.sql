-- Migration: 016_audit_log.sql
-- Records who changed what, and when. Until this table existed every delete was
-- silent: removing a reservation, a user, an item or a discount left no trace.
--
--   AuditID    : identity, PK
--   CreatedAt  : when the change happened
--   Username   : the signed-in operator ('system' where there is no user session,
--                e.g. a guest-facing action)
--   Action     : CREATE / UPDATE / DELETE / LOGIN / LOGIN_FAILED / LOCKOUT /
--                ISSUE / REVOKE / ADJUST / POST / ACK / RESOLVE
--   EntityType : Reservation / User / Item / Discount / Room / RoomType / Order /
--                Feedback / ConciergeRequest / Notification / StaffAlert /
--                KeyCard / LoyaltyAccount / Invoice
--   EntityID   : the natural key of the row (room number, username, code, ...)
--   Details    : short human-readable description of the change
--
-- log_audit() in maincopycopy.py writes these rows from an independent connection
-- so a failure in the surrounding business transaction can never be blamed on the
-- audit write, and vice versa. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-013.
USE hotelSystem
GO

IF OBJECT_ID('dbo.AuditLog', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.AuditLog (
        AuditID    INT IDENTITY(1,1) NOT NULL,
        CreatedAt  DATETIME      NOT NULL CONSTRAINT DF_AuditLog_CreatedAt DEFAULT (GETDATE()),
        Username   NVARCHAR(50)  NOT NULL CONSTRAINT DF_AuditLog_Username DEFAULT (N'system'),
        Action     NVARCHAR(20)  NOT NULL,
        EntityType NVARCHAR(50)  NOT NULL,
        EntityID   NVARCHAR(100) NULL,
        Details    NVARCHAR(1000) NULL,
        CONSTRAINT PK_AuditLog PRIMARY KEY CLUSTERED (AuditID)
    );
END
GO

-- Admin reporting filters by entity then recency ("show me this room's history").
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AuditLog_Entity' AND object_id = OBJECT_ID('dbo.AuditLog'))
    CREATE INDEX IX_AuditLog_Entity ON dbo.AuditLog (EntityType, EntityID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_AuditLog_CreatedAt' AND object_id = OBJECT_ID('dbo.AuditLog'))
    CREATE INDEX IX_AuditLog_CreatedAt ON dbo.AuditLog (CreatedAt DESC);
GO

-- 033_keycard_active_unique.sql
-- Phase 2D: at most one Active card per room.
-- Re-runnable.

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID('dbo.KeyCards')
                 AND name = 'UX_KeyCards_ActiveRoom')
BEGIN
    CREATE UNIQUE INDEX UX_KeyCards_ActiveRoom
        ON dbo.KeyCards (RoomNumber)
        WHERE Status = 'Active';
END
GO

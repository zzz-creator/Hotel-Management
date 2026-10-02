-- Migration: 017_door_access.sql
-- Key-card door access: a real feature instead of the empty "Door Access Control"
-- section header that sat in maincopycopy.py.
--
--   KeyCards   : one card per issued stay. Issued at check-in, revoked at check-out,
--                moved with the guest by edit_reservation(), and killed with the
--                reservation by delete_reservation(). A card is valid only while its
--                Status = 'Active' AND today is within [IssuedAt, ExpiresAt].
--   DoorEvents : an append-only access log (Granted / Denied) so a card dispute can
--                be settled from history. Every door read writes one.
--
-- No FK to Reservations/Rooms: room numbers are free-text `<floor><3-digit-code>` and
-- the existing schema deliberately avoids constraining them (same reasoning as
-- Transactions and Invoices).
--
-- Requires migrations 001-013. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-013.
USE hotelSystem
GO

IF OBJECT_ID('dbo.KeyCards', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.KeyCards (
        CardID      INT IDENTITY(1,1) NOT NULL,
        CardNumber  NVARCHAR(30)  NOT NULL,
        RoomNumber  NVARCHAR(50)  NOT NULL,
        LastName    NVARCHAR(50)  NULL,
        FirstName   NVARCHAR(50)  NULL,
        Status      NVARCHAR(20)  NOT NULL CONSTRAINT DF_KeyCards_Status DEFAULT ('Active'),
        IssuedAt    DATETIME      NOT NULL CONSTRAINT DF_KeyCards_IssuedAt DEFAULT (GETDATE()),
        ExpiresAt   DATETIME      NULL,
        RevokedAt   DATETIME      NULL,
        RevokeReason NVARCHAR(200) NULL,
        IssuedBy    NVARCHAR(50)  NULL,
        CONSTRAINT PK_KeyCards PRIMARY KEY CLUSTERED (CardID),
        CONSTRAINT UQ_KeyCards_CardNumber UNIQUE (CardNumber)
    );
END
GO

-- Status is constrained so a typo cannot invent a new card state.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_KeyCards_Status' AND parent_object_id = OBJECT_ID('dbo.KeyCards'))
    ALTER TABLE dbo.KeyCards
        ADD CONSTRAINT CK_KeyCards_Status CHECK (Status IN ('Active', 'Revoked', 'Lost', 'Expired'));
GO

-- The card number is looked up on every door read; the room on housekeeping queries.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_KeyCards_CardNumber' AND object_id = OBJECT_ID('dbo.KeyCards'))
    CREATE INDEX IX_KeyCards_CardNumber ON dbo.KeyCards (CardNumber);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_KeyCards_RoomNumber' AND object_id = OBJECT_ID('dbo.KeyCards'))
    CREATE INDEX IX_KeyCards_RoomNumber ON dbo.KeyCards (RoomNumber, Status);
GO

IF OBJECT_ID('dbo.DoorEvents', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.DoorEvents (
        EventID    INT IDENTITY(1,1) NOT NULL,
        CardNumber NVARCHAR(30)  NULL,
        RoomNumber NVARCHAR(50)  NULL,
        EventTime  DATETIME      NOT NULL CONSTRAINT DF_DoorEvents_EventTime DEFAULT (GETDATE()),
        Result     NVARCHAR(10)  NOT NULL,
        Detail     NVARCHAR(200) NULL,
        CONSTRAINT PK_DoorEvents PRIMARY KEY CLUSTERED (EventID)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_DoorEvents_Time' AND object_id = OBJECT_ID('dbo.DoorEvents'))
    CREATE INDEX IX_DoorEvents_Time ON dbo.DoorEvents (EventTime DESC);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_DoorEvents_CardNumber' AND object_id = OBJECT_ID('dbo.DoorEvents'))
    CREATE INDEX IX_DoorEvents_CardNumber ON dbo.DoorEvents (CardNumber);
GO

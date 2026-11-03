-- Migration 034: Conference room system -- Phase A (room catalog + setup types).
--
-- Delivers the first half of the conference room subsystem: the room catalog
-- (ConferenceRooms) and the per-room setup types (ConferenceRoomSetups). Booking,
-- billing, catering and equipment land in later migrations (035+).
--
-- Conference rooms are deliberately a separate subsystem from the guest Rooms /
-- Reservations tables: a conference room can hold many bookings per day, needs real
-- overlap checks, and has no housekeeping state machine. See plans for the design.
--
-- Re-runnable.
USE hotelSystem
GO

/* ============================ ConferenceRooms ============================ */
IF OBJECT_ID('dbo.ConferenceRooms', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConferenceRooms (
        RoomID       INT IDENTITY(1,1) NOT NULL,
        RoomNumber   NVARCHAR(50)      NOT NULL,
        Name         NVARCHAR(100)     NOT NULL,
        Capacity     INT               NOT NULL CONSTRAINT DF_ConferenceRooms_Capacity DEFAULT (0),
        HourlyRate   DECIMAL(10,2)     NOT NULL CONSTRAINT DF_ConferenceRooms_HourlyRate DEFAULT (0),
        Description  NVARCHAR(255)     NULL,
        Active       BIT               NOT NULL CONSTRAINT DF_ConferenceRooms_Active DEFAULT (1),
        CreatedAt    DATETIME          NOT NULL CONSTRAINT DF_ConferenceRooms_CreatedAt DEFAULT (GETDATE()),
        CONSTRAINT PK_ConferenceRooms PRIMARY KEY CLUSTERED (RoomID),
        CONSTRAINT UQ_ConferenceRooms_RoomNumber UNIQUE (RoomNumber)
    );
END
GO

/* ============================ ConferenceRoomSetups ============================ */
IF OBJECT_ID('dbo.ConferenceRoomSetups', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConferenceRoomSetups (
        SetupID      INT IDENTITY(1,1) NOT NULL,
        RoomID       INT               NOT NULL,
        SetupType    NVARCHAR(50)      NOT NULL,
        Capacity     INT               NOT NULL CONSTRAINT DF_ConferenceRoomSetups_Capacity DEFAULT (0),
        Active       BIT               NOT NULL CONSTRAINT DF_ConferenceRoomSetups_Active DEFAULT (1),
        CONSTRAINT PK_ConferenceRoomSetups PRIMARY KEY CLUSTERED (SetupID),
        CONSTRAINT FK_ConferenceRoomSetups_Room FOREIGN KEY (RoomID)
            REFERENCES dbo.ConferenceRooms (RoomID) ON DELETE CASCADE
    );
END
GO

-- A room can only list a given setup type once.
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID('dbo.ConferenceRoomSetups')
                 AND name = 'UX_ConferenceRoomSetups_Room_Setup')
BEGIN
    CREATE UNIQUE INDEX UX_ConferenceRoomSetups_Room_Setup
        ON dbo.ConferenceRoomSetups (RoomID, SetupType);
END
GO

-- Sanity CHECK constraints: capacity and rate must not be negative.
IF OBJECT_ID('dbo.CK_ConferenceRooms_Capacity', 'C') IS NULL
    ALTER TABLE dbo.ConferenceRooms WITH CHECK ADD CONSTRAINT CK_ConferenceRooms_Capacity
        CHECK (Capacity >= 0);
GO
IF OBJECT_ID('dbo.CK_ConferenceRooms_HourlyRate', 'C') IS NULL
    ALTER TABLE dbo.ConferenceRooms WITH CHECK ADD CONSTRAINT CK_ConferenceRooms_HourlyRate
        CHECK (HourlyRate >= 0);
GO
IF OBJECT_ID('dbo.CK_ConferenceRoomSetups_Capacity', 'C') IS NULL
    ALTER TABLE dbo.ConferenceRoomSetups WITH CHECK ADD CONSTRAINT CK_ConferenceRoomSetups_Capacity
        CHECK (Capacity >= 0);
GO
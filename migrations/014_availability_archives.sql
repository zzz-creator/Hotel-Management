-- Migration: 014_availability_archives.sql
-- Preserves booking history so availability search works over a real stay calendar.
--
-- `Reservations.RoomNumber` is the primary key, so a room has at most ONE live booking
-- and re-booking a room silently overwrote the previous guest. The proper fix is a
-- ReservationID identity plus real overlap checks, but that would rewrite
-- add/edit/delete_reservation, check_in/check_out, loyalty and invoice keying and every
-- `WHERE RoomNumber = ?` site.
--
-- Instead: when add_reservation() overwrites a COMPLETED past stay, the outgoing row is
-- copied here first. The PK and every existing query stay as they are; availability
-- search reads live + archived.
--
--   Nights  : (CheckOutDate - CheckInDate).days, snapshotted so a later date edit to the
--             live row cannot rewrite history.
--   ArchivedAt : when the stay was displaced by a re-booking.
--
-- Requires migrations 001-013. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-013.
USE hotelSystem
GO

IF OBJECT_ID('dbo.ReservationArchive', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ReservationArchive (
        ArchiveID   INT IDENTITY(1,1) NOT NULL,
        RoomNumber  NVARCHAR(50)  NOT NULL,
        Floor       INT           NULL,
        LastName    NVARCHAR(50)  NULL,
        FirstName   VARCHAR(50)   NULL,
        CheckInDate DATE          NOT NULL,
        CheckOutDate DATE         NOT NULL,
        Nights      INT           NOT NULL,
        RoomType    VARCHAR(50)   NULL,
        NightlyRate DECIMAL(10,2) NULL,
        ArchivedAt  DATETIME      NOT NULL CONSTRAINT DF_ReservationArchive_ArchivedAt DEFAULT (GETDATE()),
        CONSTRAINT PK_ReservationArchive PRIMARY KEY CLUSTERED (ArchiveID)
    );
END
GO

-- Availability search scans by room then by date window.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ReservationArchive_RoomDate' AND object_id = OBJECT_ID('dbo.ReservationArchive'))
    CREATE INDEX IX_ReservationArchive_RoomDate ON dbo.ReservationArchive (RoomNumber, CheckInDate, CheckOutDate);
GO

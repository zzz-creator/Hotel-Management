-- Migration: 004_align_transactions.sql
-- Reconciles the live Transactions table with the shape the application code expects:
--   ID (identity, PK), RoomNumber, ItemID, Quantity, UnitPrice, Amount, CreatedAt, IsBilled
-- The pre-existing table used the legacy column names (TransactionID, ReservationRoomNumber,
-- Description). Run AFTER migrations 001-003. Safe to re-run / a no-op on a fresh schema.
USE hotelSystem
GO

-- 1. Guard: a drop/recreate would destroy data, so refuse to run if rows exist.
IF (SELECT COUNT(*) FROM dbo.Transactions) > 0
BEGIN
    RAISERROR('Transactions is not empty (found existing rows). Back up or migrate the data before applying 004_align_transactions.sql.', 16, 1);
    RETURN;
END
GO

-- 2. Recreate Transactions in the shape code + reports expect.
IF OBJECT_ID('dbo.Transactions', 'U') IS NOT NULL
    DROP TABLE dbo.Transactions;
GO

CREATE TABLE dbo.Transactions (
    ID INT IDENTITY(1,1) PRIMARY KEY,
    RoomNumber NVARCHAR(50) NULL,
    ItemID INT NULL,
    Quantity INT NOT NULL DEFAULT (1),
    UnitPrice DECIMAL(10,2) NULL,
    Amount DECIMAL(12,2) NOT NULL DEFAULT (0),
    CreatedAt DATETIME NOT NULL DEFAULT (GETDATE()),
    IsBilled BIT NOT NULL DEFAULT (0)
);
GO

-- 3. Widen Reservations.RoomNumber (nvarchar(10) -> nvarchar(50)) so it can host the FK.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('dbo.Reservations')
             AND name = 'RoomNumber' AND max_length < 100)
    ALTER TABLE dbo.Reservations ALTER COLUMN RoomNumber NVARCHAR(50) NOT NULL;
GO

-- 4. Referential integrity + lookup index.
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys
               WHERE name = 'FK_Transactions_Reservations'
                 AND parent_object_id = OBJECT_ID('dbo.Transactions'))
    ALTER TABLE dbo.Transactions ADD CONSTRAINT FK_Transactions_Reservations
        FOREIGN KEY (RoomNumber) REFERENCES dbo.Reservations(RoomNumber);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Transactions_RoomNumber'
                 AND object_id = OBJECT_ID('dbo.Transactions'))
    CREATE INDEX IX_Transactions_RoomNumber ON dbo.Transactions(RoomNumber);
GO
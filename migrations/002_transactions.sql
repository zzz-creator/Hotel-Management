-- Migration: 002_transactions.sql
USE hotelSystem
IF OBJECT_ID('dbo.Transactions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Transactions (
        ID INT IDENTITY(1,1) PRIMARY KEY,
        RoomNumber NVARCHAR(50) NULL,
        ItemID INT NULL,
        Quantity INT NOT NULL DEFAULT 1,
        UnitPrice DECIMAL(10,2) NULL,
        Amount DECIMAL(12,2) NOT NULL,
        CreatedAt DATETIME NOT NULL DEFAULT(GETDATE()),
        IsBilled BIT NOT NULL DEFAULT(0)
    );
    -- Optional FK if Reservations table exists
    IF OBJECT_ID('dbo.Reservations', 'U') IS NOT NULL
    BEGIN
        ALTER TABLE dbo.Transactions
        ADD CONSTRAINT FK_Transactions_Reservations
        FOREIGN KEY (RoomNumber) REFERENCES dbo.Reservations(RoomNumber);
    END
END
ELSE
BEGIN
    -- If table exists, ensure columns exist (add missing columns)
    IF COL_LENGTH('dbo.Transactions','RoomNumber') IS NULL
        EXEC('ALTER TABLE dbo.Transactions ADD RoomNumber NVARCHAR(50) NULL');
    IF COL_LENGTH('dbo.Transactions','UnitPrice') IS NULL
        EXEC('ALTER TABLE dbo.Transactions ADD UnitPrice DECIMAL(10,2) NULL');
    IF COL_LENGTH('dbo.Transactions','Amount') IS NULL
        EXEC('ALTER TABLE dbo.Transactions ADD Amount DECIMAL(12,2) NOT NULL DEFAULT(0)');
    IF COL_LENGTH('dbo.Transactions','CreatedAt') IS NULL
        EXEC('ALTER TABLE dbo.Transactions ADD CreatedAt DATETIME NOT NULL DEFAULT(GETDATE())');
    IF COL_LENGTH('dbo.Transactions','IsBilled') IS NULL
        EXEC('ALTER TABLE dbo.Transactions ADD IsBilled BIT NOT NULL DEFAULT(0)');
END

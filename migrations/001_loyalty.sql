-- Migration: 001_loyalty.sql
-- Creates LoyaltyAccounts and LoyaltyTransactions tables for the loyalty program
USE hotelSystem
IF OBJECT_ID('dbo.LoyaltyAccounts', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LoyaltyAccounts (
        RoomNumber NVARCHAR(50) NOT NULL PRIMARY KEY,
        Points INT NOT NULL DEFAULT(0),
        Tier NVARCHAR(50) NULL,
        LastUpdated DATETIME NULL
    );
END;

IF OBJECT_ID('dbo.LoyaltyTransactions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LoyaltyTransactions (
        ID INT IDENTITY(1,1) PRIMARY KEY,
        RoomNumber NVARCHAR(50) NULL,
        Delta INT NOT NULL,
        Reason NVARCHAR(255) NULL,
        CreatedAt DATETIME NOT NULL DEFAULT(GETDATE()),
        SourceID NVARCHAR(100) NULL
    );
END;

-- Indexes
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_LoyaltyTransactions_RoomNumber')
BEGIN
    CREATE INDEX IX_LoyaltyTransactions_RoomNumber ON dbo.LoyaltyTransactions(RoomNumber);
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_LoyaltyAccounts_Points')
BEGIN
    CREATE INDEX IX_LoyaltyAccounts_Points ON dbo.LoyaltyAccounts(Points);
END;

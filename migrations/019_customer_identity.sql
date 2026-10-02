-- Migration: 019_customer_identity.sql
-- Re-keys loyalty from ROOM NUMBER to CUSTOMER, and adds the identity the public
-- booking desk needs.
--
-- WHY THIS EXISTS
--   `LoyaltyAccounts.RoomNumber` was the PRIMARY KEY, so a loyalty "account" WAS a
--   physical room. That is wrong in three ways that all bite in production:
--     1. The next guest of a room inherits the previous guest's balance and tier, so
--        they get a discount they never earned.
--     2. A returning guest's points fragment across whichever rooms they happen to
--        stay in, so Gold and Platinum are effectively unreachable.
--     3. Moving a guest to another room either stranded their points (when the target
--        room already had an account) or handed them the previous occupant's balance.
--   Loyalty is a relationship with a PERSON, so the key becomes `CustomerProfiles.CustomerID`.
--   `LoyaltyAccounts.RoomNumber` survives as a nullable "last room seen" column, purely
--   for display -- it is no longer a key and no longer unique.
--
-- IDENTITY
--   `CustomerProfiles` gains a `Password` column and a UNIQUE filtered index on `Email`.
--   The index is FILTERED (WHERE Email IS NOT NULL) on purpose: `check_in()` upserts a
--   profile from the name alone and leaves Email NULL, and SQL Server allows only one
--   NULL in a plain unique index, which would make the second walk-in profile fail.
--   Email is therefore unique *when present* and the booking login refuses a blank one.
--
--   Plaintext passwords are intentional in this project (see AGENTS.md), so Password is
--   stored as entered, matching the `Users.Password` convention.
--
-- LEGACY DATA
--   `LoyaltyAccounts` rows are re-pointed at a customer where one can be found, and any row
--   still without a customer is DELETED: a balance attributed to a room cannot be attributed
--   to a person, and guessing an owner would credit the wrong guest with somebody else's
--   history. The count of discarded rows is SELECTed at the end so the operator can see it.
--
--   "Where one can be found" is deliberately narrow. A room's CURRENT occupant is not
--   automatically the guest the balance belongs to -- rooms are re-let, and adopting a
--   stale balance onto whoever is in the room now is the very defect this migration exists
--   to fix. A row is therefore only adopted when its own timestamp falls inside the stay
--   that currently occupies the room (LastUpdated for the balance, CreatedAt for each
--   ledger row), which is the only evidence available that they are the same person. A
--   balance last touched months ago by a guest who has since left is deleted instead.
--
-- Requires migrations 001-018. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-018.
USE hotelSystem
GO

-- ---------------------------------------------------------------- CustomerProfiles
IF COL_LENGTH('dbo.CustomerProfiles', 'Password') IS NULL
    ALTER TABLE dbo.CustomerProfiles ADD Password NVARCHAR(100) NULL
GO

IF COL_LENGTH('dbo.CustomerProfiles', 'CreatedAt') IS NULL
    ALTER TABLE dbo.CustomerProfiles
        ADD CreatedAt DATETIME NOT NULL CONSTRAINT DF_CustomerProfiles_CreatedAt DEFAULT (GETDATE())
GO

-- A login identity needs a unique handle. Filtered so the many Email-less walk-in
-- profiles created by check_in() can coexist.
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'UX_CustomerProfiles_Email'
                 AND object_id = OBJECT_ID('dbo.CustomerProfiles'))
    CREATE UNIQUE INDEX UX_CustomerProfiles_Email
        ON dbo.CustomerProfiles (Email) WHERE Email IS NOT NULL
GO

-- ------------------------------------------------------------------ Reservations
IF COL_LENGTH('dbo.Reservations', 'CustomerID') IS NULL
    ALTER TABLE dbo.Reservations ADD CustomerID INT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys
               WHERE name = 'FK_Reservations_CustomerProfiles')
    ALTER TABLE dbo.Reservations
        ADD CONSTRAINT FK_Reservations_CustomerProfiles
        FOREIGN KEY (CustomerID) REFERENCES dbo.CustomerProfiles (CustomerID)
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Reservations_CustomerID'
                 AND object_id = OBJECT_ID('dbo.Reservations'))
    CREATE INDEX IX_Reservations_CustomerID ON dbo.Reservations (CustomerID)
GO

-- ----------------------------------------------------------- LoyaltyTransactions
-- The ledger keeps RoomNumber as the stay CONTEXT (which room the points were earned
-- in) but gains CustomerID as the account the points belong to.
IF COL_LENGTH('dbo.LoyaltyTransactions', 'CustomerID') IS NULL
    ALTER TABLE dbo.LoyaltyTransactions ADD CustomerID INT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys
               WHERE name = 'FK_LoyaltyTransactions_CustomerProfiles')
    ALTER TABLE dbo.LoyaltyTransactions
        ADD CONSTRAINT FK_LoyaltyTransactions_CustomerProfiles
        FOREIGN KEY (CustomerID) REFERENCES dbo.CustomerProfiles (CustomerID)
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_LoyaltyTransactions_CustomerID'
                 AND object_id = OBJECT_ID('dbo.LoyaltyTransactions'))
    CREATE INDEX IX_LoyaltyTransactions_CustomerID ON dbo.LoyaltyTransactions (CustomerID)
GO

-- Backfill the ledger from the reservation that occupied the room, where the timing says
-- they are the same guest. Joining on RoomNumber alone would attach an old guest's history
-- to whoever holds the room today.
UPDATE lt
SET lt.CustomerID = r.CustomerID
FROM dbo.LoyaltyTransactions lt
JOIN dbo.Reservations r
  ON r.RoomNumber = lt.RoomNumber
 AND r.CustomerID IS NOT NULL
 AND lt.CreatedAt >= r.CheckInDate
 AND lt.CreatedAt <  DATEADD(DAY, 1, CAST(r.CheckOutDate AS DATETIME))
WHERE lt.CustomerID IS NULL
GO

-- --------------------------------------------------------------- LoyaltyAccounts
-- Re-key from RoomNumber to CustomerID.
IF COL_LENGTH('dbo.LoyaltyAccounts', 'CustomerID') IS NULL
    ALTER TABLE dbo.LoyaltyAccounts ADD CustomerID INT NULL
GO

-- Adopt a room-keyed balance onto the occupant who was actually in the room when the
-- balance was last touched. See the LEGACY DATA note above for why this is windowed.
UPDATE la
SET la.CustomerID = r.CustomerID
FROM dbo.LoyaltyAccounts la
JOIN dbo.Reservations r
  ON r.RoomNumber = la.RoomNumber
 AND r.CustomerID IS NOT NULL
 AND la.LastUpdated IS NOT NULL
 AND la.LastUpdated >= r.CheckInDate
 AND la.LastUpdated <  DATEADD(DAY, 1, CAST(r.CheckOutDate AS DATETIME))
WHERE la.CustomerID IS NULL
GO

-- A balance with no attributable person is not history, it is a misattribution waiting
-- to happen. Remove it rather than guess an owner.
--
-- Counted before and reported straight after, in the SAME batch: variables do not survive a
-- GO, and an irreversible DELETE of somebody's points must never be silent. The header note
-- promises the operator can see what was dropped, so this has to actually emit it.
DECLARE @adopted      int = (SELECT COUNT(*) FROM dbo.LoyaltyAccounts WHERE CustomerID IS NOT NULL);
DECLARE @dropped      int = (SELECT COUNT(*) FROM dbo.LoyaltyAccounts WHERE CustomerID IS NULL);
DECLARE @dropped_pts  int = (SELECT ISNULL(SUM(Points), 0) FROM dbo.LoyaltyAccounts WHERE CustomerID IS NULL);
DELETE FROM dbo.LoyaltyAccounts WHERE CustomerID IS NULL;
SELECT @adopted AS LoyaltyBalancesAdoptedOntoAGuest,
       @dropped AS LoyaltyBalancesDiscardedNoAttributableGuest,
       @dropped_pts AS PointsDiscardedWithThem;
GO

-- A guest who stayed in two rooms had an account per room, so the adoption above can leave
-- two rows sharing one CustomerID -- which the new primary key forbids. Fold the points
-- together rather than discarding them: keeping the highest row alone would silently lose
-- whatever the other room had accrued.
IF EXISTS (SELECT 1 FROM dbo.LoyaltyAccounts
           WHERE CustomerID IS NOT NULL
           GROUP BY CustomerID HAVING COUNT(*) > 1)
BEGIN
    ;WITH ranked AS (
        SELECT CustomerID, Points,
               ROW_NUMBER() OVER (PARTITION BY CustomerID
                                  ORDER BY Points DESC, LastUpdated DESC) AS rn
        FROM dbo.LoyaltyAccounts
        WHERE CustomerID IS NOT NULL
    ),
    totals AS (
        SELECT CustomerID, SUM(Points) AS TotalPoints
        FROM ranked
        GROUP BY CustomerID
    )
    UPDATE la
    SET la.Points = t.TotalPoints
    FROM dbo.LoyaltyAccounts la
    JOIN ranked r ON r.CustomerID = la.CustomerID AND r.rn = 1
    JOIN totals t ON t.CustomerID = la.CustomerID
    WHERE EXISTS (SELECT 1 FROM ranked r2
                  WHERE r2.CustomerID = la.CustomerID AND r2.rn > 1)
END
GO

IF EXISTS (SELECT 1 FROM dbo.LoyaltyAccounts
           WHERE CustomerID IS NOT NULL
           GROUP BY CustomerID HAVING COUNT(*) > 1)
BEGIN
    ;WITH ranked AS (
        SELECT CustomerID,
               ROW_NUMBER() OVER (PARTITION BY CustomerID
                                  ORDER BY Points DESC, LastUpdated DESC) AS rn
        FROM dbo.LoyaltyAccounts
        WHERE CustomerID IS NOT NULL
    )
    DELETE la
    FROM dbo.LoyaltyAccounts la
    JOIN ranked r ON r.CustomerID = la.CustomerID
    WHERE r.rn > 1
END
GO

-- CustomerID is now mandatory: an account with no owner has no meaning.
DELETE FROM dbo.LoyaltyAccounts WHERE CustomerID IS NULL
GO

-- NB: folding two balances together leaves the surviving row's Tier describing the OLD
-- smaller total. It is left as-is rather than recomputed here because the thresholds are
-- admin-editable in HotelSettings and SQL has no single source of truth for them; start the
-- app once and run "Tier Management" -> recompute, which is idempotent.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('dbo.LoyaltyAccounts')
             AND name = 'CustomerID' AND is_nullable = 1)
    ALTER TABLE dbo.LoyaltyAccounts ALTER COLUMN CustomerID INT NOT NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys
               WHERE name = 'FK_LoyaltyAccounts_CustomerProfiles')
    ALTER TABLE dbo.LoyaltyAccounts
        ADD CONSTRAINT FK_LoyaltyAccounts_CustomerProfiles
        FOREIGN KEY (CustomerID) REFERENCES dbo.CustomerProfiles (CustomerID)
GO

-- Drop the old room primary key and make CustomerID the key.
--
-- Located by is_primary_key rather than BY NAME. Migration 001 declares the key inline
-- (`RoomNumber ... NOT NULL PRIMARY KEY`), so SQL Server auto-names it
-- PK__LoyaltyAccounts__<hash>; a name-based drop silently matches nothing and the CREATE
-- then fails with "Table 'LoyaltyAccounts' already has a primary key defined on it".
-- Skipping the drop when the key we want is already in place also makes this re-runnable.
--
-- The statement is run through sp_executesql, which takes a statement and is never
-- ambiguous. Both shorter spellings were tried against this database and both fail:
--   EXEC('... ' + QUOTENAME(@old_pk))  -> Msg 102 Incorrect syntax near 'QUOTENAME'
--   EXEC @drop_pk                       -> Msg 203 The name 'ALTER TABLE ...' is not a
--                                          valid identifier
-- because EXECUTE's parenthesised form takes a module name or a string, and a BARE
-- variable is read as a module name. Either way the drop silently does not happen, and
-- the step after it then fails with Msg 4922 (the old key still depends on the column).
IF EXISTS (SELECT 1 FROM sys.indexes
           WHERE object_id = OBJECT_ID('dbo.LoyaltyAccounts')
             AND is_primary_key = 1
             AND name <> 'PK_LoyaltyAccounts')
BEGIN
    DECLARE @old_pk sysname = (
        SELECT name FROM sys.indexes
        WHERE object_id = OBJECT_ID('dbo.LoyaltyAccounts') AND is_primary_key = 1);
    DECLARE @drop_pk nvarchar(max) =
        N'ALTER TABLE dbo.LoyaltyAccounts DROP CONSTRAINT ' + QUOTENAME(@old_pk);
    EXEC sp_executesql @drop_pk;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID('dbo.LoyaltyAccounts')
                 AND is_primary_key = 1)
    ALTER TABLE dbo.LoyaltyAccounts
        ADD CONSTRAINT PK_LoyaltyAccounts PRIMARY KEY CLUSTERED (CustomerID)
GO

-- RoomNumber is now just the last room a guest stayed in: display only. It has to be
-- nullable because most customers have several stays and only the latest is kept, and the
-- old inline primary key was NOT NULL.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('dbo.LoyaltyAccounts')
             AND name = 'RoomNumber' AND is_nullable = 0)
    ALTER TABLE dbo.LoyaltyAccounts ALTER COLUMN RoomNumber NVARCHAR(50) NULL
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_LoyaltyAccounts_RoomNumber'
                 AND object_id = OBJECT_ID('dbo.LoyaltyAccounts'))
    CREATE INDEX IX_LoyaltyAccounts_RoomNumber ON dbo.LoyaltyAccounts (RoomNumber)
GO

-- ---------------------------------------------------------------------- Summary
SELECT COUNT(*) AS LoyaltyAccountsNowKeyedByCustomer FROM dbo.LoyaltyAccounts
GO
SELECT COUNT(*) AS ProfilesWithLoginEmail
    FROM dbo.CustomerProfiles WHERE Email IS NOT NULL
GO

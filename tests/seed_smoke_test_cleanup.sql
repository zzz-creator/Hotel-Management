-- Removes exactly what tests/seed_smoke_test.sql created, and nothing else.
--
-- Safe to run more than once. Scoped to the two seed email addresses, the two seed rooms
-- and the one seed booking reference -- it never touches a blanket room range, so it is
-- safe against a database that also holds real data.
--
-- NOTE: the loyalty sweep by room removes ANY ledger row for rooms 12045 and 12700, not
-- just the seeded ones. That is deliberate for a re-seed (it resets the scenario to a known
-- state), but it does mean running this after a real check-out discards that guest's newly
-- earned points.
USE hotelSystem
GO

SET NOCOUNT ON;

DECLARE @InHouseEmail varchar(100) = 'maya.rivera@seed.test';
DECLARE @BookedEmail  varchar(100) = 'ben.okafor@seed.test';
DECLARE @InHouseRoom  varchar(10) = '12045';
DECLARE @BookedRoom   varchar(10) = '12700';
DECLARE @BookingRef   varchar(20) = 'BK-A1B2C3';

BEGIN TRANSACTION;

DELETE FROM dbo.OrderItems
 WHERE OrderID IN (SELECT OrderID FROM dbo.Orders
                    WHERE RoomNumber IN (@InHouseRoom, @BookedRoom));
DELETE FROM dbo.Orders             WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.Transactions       WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.DoorEvents         WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.Notifications      WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.KeyCards           WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.ReservationPayments
 WHERE BookingRef = @BookingRef OR RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.LoyaltyTransactions
 WHERE CustomerID IN (SELECT CustomerID FROM dbo.CustomerProfiles
                       WHERE Email IN (@InHouseEmail, @BookedEmail));
DELETE FROM dbo.LoyaltyAccounts
 WHERE CustomerID IN (SELECT CustomerID FROM dbo.CustomerProfiles
                       WHERE Email IN (@InHouseEmail, @BookedEmail));
DELETE FROM dbo.Reservations       WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.CustomerProfiles   WHERE Email IN (@InHouseEmail, @BookedEmail);
DELETE FROM dbo.LoyaltyTransactions WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);

UPDATE dbo.Rooms SET Status = 'Available'
 WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);

COMMIT TRANSACTION;

SELECT 'CustomerProfiles' AS tbl, COUNT(*) AS remaining FROM dbo.CustomerProfiles
 WHERE Email IN (@InHouseEmail, @BookedEmail)
UNION ALL SELECT 'Reservations', COUNT(*) FROM dbo.Reservations
 WHERE RoomNumber IN (@InHouseRoom, @BookedRoom)
UNION ALL SELECT 'ReservationPayments', COUNT(*) FROM dbo.ReservationPayments
 WHERE BookingRef = @BookingRef
UNION ALL SELECT 'Transactions', COUNT(*) FROM dbo.Transactions
 WHERE RoomNumber IN (@InHouseRoom, @BookedRoom)
UNION ALL SELECT 'KeyCards', COUNT(*) FROM dbo.KeyCards
 WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
GO

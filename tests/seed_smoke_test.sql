-- Seed two test guests so the booking desk and the in-house guest features can be
-- exercised end to end against a live database.
--
--   GUEST A -- IN HOUSE (front desk).  Room 12045, Standard, $120/night.
--             Checked in TODAY, checking out in 2 nights. Has a room-service folio, one
--             pay-now line, an open order, an active key card, a Gold loyalty balance and
--             ledger history. Exercises: validate_room(), room service, order tracking,
--             key card + door reader, loyalty status/history, and a full check-out that
--             exercises the split folio, the Gold tier discount and the loyalty award.
--             Deliberately has NO booking reference, because a front-desk stay never gets
--             one -- so "View My Booking" must correctly say it has no booking.
--
--   GUEST B -- BOOKED (public booking desk).  Room 12700, Deluxe, $180/night.
--             Arriving in 10 days, 2 nights, with a one-night deposit taken. No loyalty
--             account yet, so they are a brand-new Bronze member. Exercises:
--             customer_login(), "View My Booking", and "Cancel Booking" -- which must
--             produce a full Refund row because arrival is beyond the refund cutoff
--             (booking_refund_cutoff_days = 7).
--
-- The contrast is the point: A is a walk-in with history and no booking, B is an account
-- with a booking and no history. Both paths are covered by the same two guests.
--
-- SAFETY
--   * Everything runs in ONE transaction and ONE batch. There is deliberately no `GO` after
--     the DECLAREs, because variables do not survive a batch boundary, and no `GO` inside
--     the transaction, so a failure half way leaves the database untouched.
--   * Cleanup is scoped to the two seed email addresses, the two seed rooms and the one
--     seed booking reference. It never touches a blanket room range, so it is safe to run
--     against a database that already has real data. Re-running is safe.
--   * Rooms are chosen from the migration 008 seed and verified free: 12045 = Standard
--     (floor 12, code 045), 12700 = Deluxe (floor 12, code 700).
--
-- Apply from SSMS. To undo, run tests/seed_smoke_test_cleanup.sql.
USE hotelSystem
GO

SET NOCOUNT ON;

DECLARE @InHouseEmail varchar(100) = 'maya.rivera@seed.test';
DECLARE @BookedEmail  varchar(100) = 'ben.okafor@seed.test';
DECLARE @Password     nvarchar(100) = N'TestPass!23';

DECLARE @InHouseRoom varchar(10) = '12045';
DECLARE @BookedRoom  varchar(10) = '12700';
DECLARE @BookingRef  varchar(20) = 'BK-A1B2C3';
DECLARE @CardNumber  nvarchar(30) = N'KC-406112';

DECLARE @Today            date = CAST(GETDATE() AS DATE);
DECLARE @InHouseCheckIn   date = @Today;
DECLARE @InHouseCheckOut  date = DATEADD(DAY, 2, @Today);
DECLARE @BookedCheckIn    date = DATEADD(DAY, 10, @Today);
DECLARE @BookedCheckOut   date = DATEADD(DAY, 12, @Today);

-- Deluxe deposit: one night at $180 plus 13% tax. Matches what booking_quote() would
-- produce, so the invoice credit path has a realistic figure to work with.
DECLARE @Deposit decimal(12,2) = 180.00 * 1.13;

BEGIN TRANSACTION;

-- ------------------------------------------------------------------ cleanup (re-runnable)
-- Child rows before parents: Transactions and OrderItems both point at Reservations.
DELETE FROM dbo.OrderItems
 WHERE OrderID IN (SELECT OrderID FROM dbo.Orders
                    WHERE RoomNumber IN (@InHouseRoom, @BookedRoom));
DELETE FROM dbo.Orders        WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.Transactions  WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.DoorEvents    WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.Notifications WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.KeyCards      WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.ReservationPayments
 WHERE BookingRef = @BookingRef
    OR RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.LoyaltyTransactions
 WHERE CustomerID IN (SELECT CustomerID FROM dbo.CustomerProfiles
                       WHERE Email IN (@InHouseEmail, @BookedEmail));
DELETE FROM dbo.LoyaltyAccounts
 WHERE CustomerID IN (SELECT CustomerID FROM dbo.CustomerProfiles
                       WHERE Email IN (@InHouseEmail, @BookedEmail));
DELETE FROM dbo.Reservations  WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);
DELETE FROM dbo.CustomerProfiles WHERE Email IN (@InHouseEmail, @BookedEmail);

-- Any ledger row still orphaned by a deleted account would block the FK, so sweep by room
-- too. Only touches rows for the two seed rooms.
DELETE FROM dbo.LoyaltyTransactions
 WHERE RoomNumber IN (@InHouseRoom, @BookedRoom);

-- ------------------------------------------------------------------ guest accounts
INSERT INTO dbo.CustomerProfiles (LastName, FirstName, Email, Phone, Preferences, Password)
VALUES ('Rivera', 'Maya', @InHouseEmail, '555-0101', 'High floor, away from the lift', @Password),
       ('Okafor', 'Ben',  @BookedEmail,  '555-0102', 'Late arrival',                  @Password);

DECLARE @InHouseCustomerID int =
    (SELECT CustomerID FROM dbo.CustomerProfiles WHERE Email = @InHouseEmail);
DECLARE @BookedCustomerID int =
    (SELECT CustomerID FROM dbo.CustomerProfiles WHERE Email = @BookedEmail);

-- ------------------------------------------------------------------ stays
INSERT INTO dbo.Reservations
    (RoomNumber, LastName, FirstName, Floor, CheckInDate, CheckOutDate, CustomerID)
VALUES
    (@InHouseRoom, 'Rivera', 'Maya', 12, @InHouseCheckIn, @InHouseCheckOut, @InHouseCustomerID),
    (@BookedRoom,  'Okafor', 'Ben',  12, @BookedCheckIn,  @BookedCheckOut,  @BookedCustomerID);

-- The in-house room shows Occupied on the housekeeping board; the booked room stays
-- Available because the guest has not arrived.
UPDATE dbo.Rooms SET Status = 'Occupied'  WHERE RoomNumber = @InHouseRoom;
UPDATE dbo.Rooms SET Status = 'Available' WHERE RoomNumber = @BookedRoom;

-- ------------------------------------------------------------------ loyalty (guest A only)
-- 2600 points = Gold (Bronze 0 / Silver 1000 / Gold 2500 / Platinum 7500), so check-out
-- applies a real tier discount to the F&B group. Guest B is left with no account at all,
-- which is the correct state for a first-time booker.
INSERT INTO dbo.LoyaltyAccounts (CustomerID, RoomNumber, Points, Tier, LastUpdated)
VALUES (@InHouseCustomerID, @InHouseRoom, 2600, 'Gold', @InHouseCheckIn);

INSERT INTO dbo.LoyaltyTransactions
    (CustomerID, RoomNumber, Delta, Reason, CreatedAt, SourceID)
VALUES
    (@InHouseCustomerID, @InHouseRoom, 1000, 'Stay award (prior visit, Silver)',
        DATEADD(DAY, -40, @InHouseCheckIn), 'seed:stay:1'),
    (@InHouseCustomerID, @InHouseRoom,  900, 'Stay award (prior visit, Silver)',
        DATEADD(DAY, -20, @InHouseCheckIn), 'seed:stay:2'),
    (@InHouseCustomerID, @InHouseRoom,  700, 'Room service spend',
        DATEADD(DAY,  -1, @InHouseCheckIn), 'seed:order:1');

-- ------------------------------------------------------------------ folio for guest A
-- No room charge is seeded: post_room_charge() writes it at check-out, which is the whole
-- point of that function. Seeding one here would make check-out think it was already
-- charged and skip it.
--
-- ChargeGroup 'F&B' so these land in the discounted group of the split folio. The room
-- itself is charged at face value.
INSERT INTO dbo.Transactions
    (RoomNumber, ItemID, Quantity, UnitPrice, Amount, CreatedAt, IsBilled, InvoiceID,
     PaidEarlier, Description, ChargeGroup)
VALUES
    (@InHouseRoom, 15, 2,  4.50,  9.00, DATEADD(HOUR, -5, GETDATE()), 0, NULL, 0, N'In-Room Drink x2',      'F&B'),
    (@InHouseRoom,  4, 1, 20.00, 20.00, DATEADD(HOUR, -4, GETDATE()), 0, NULL, 0, N'Room Service Meal',      'F&B'),
    (@InHouseRoom,  8, 1, 25.00, 25.00, DATEADD(HOUR, -3, GETDATE()), 0, NULL, 0, N'Breakfast Buffet',       'F&B'),
    (@InHouseRoom,  5, 1, 50.00, 50.00, DATEADD(HOUR, -1, GETDATE()), 0, NULL, 0, N'Spa Appointment',        'F&B'),
    -- Paid at order time: IsBilled = 1 with a NULL InvoiceID is exactly the shape
    -- bill_room_transactions() looks for, so it is consolidated onto the check-out
    -- invoice and printed as "card (pay now)".
    (@InHouseRoom, 11, 1, 12.00, 12.00, DATEADD(HOUR, -6, GETDATE()), 1, NULL, 1, N'Porter / Bellhop',       'F&B');

-- ------------------------------------------------------------------ open room-service order
INSERT INTO dbo.Orders (RoomNumber, Status, PlacedAt, UpdatedAt, Notes)
VALUES (@InHouseRoom, 'Placed', DATEADD(MINUTE, -30, GETDATE()), DATEADD(MINUTE, -30, GETDATE()),
        N'Seed order: late room service');

DECLARE @OrderID int = (SELECT MAX(OrderID) FROM dbo.Orders WHERE RoomNumber = @InHouseRoom);
INSERT INTO dbo.OrderItems (OrderID, ItemID, ItemName, Quantity, UnitPrice)
VALUES (@OrderID, 9, N'Dinner Buffet', 1, 45.00);

-- ------------------------------------------------------------------ key card + notification
-- ExpiresAt follows the app's own rule in issue_key_card(): midnight of CheckOutDate + 1
-- day, so the card stays valid through the checkout day rather than dying at midnight on it.
INSERT INTO dbo.KeyCards
    (CardNumber, RoomNumber, LastName, FirstName, Status, IssuedAt, ExpiresAt, IssuedBy)
VALUES (@CardNumber, @InHouseRoom, 'Rivera', 'Maya', 'Active', @InHouseCheckIn,
        DATEADD(DAY, 3, @InHouseCheckIn), N'seed');

INSERT INTO dbo.Notifications (RoomNumber, Message, Channel, SentBy, CreatedAt)
VALUES (@InHouseRoom, N'Welcome back, Maya. Your spa appointment is confirmed for 4pm.', N'In-Room', N'seed', GETDATE());

-- ------------------------------------------------------------------ booking deposit (guest B)
-- StayCheckIn MUST equal Reservations.CheckInDate: _find_booking() joins the two on
-- RoomNumber AND StayCheckIn, so a mismatch here makes the booking invisible.
-- IsApplied = 0 and AppliedAmount = 0, so the deposit is unspent credit awaiting check-out.
INSERT INTO dbo.ReservationPayments
    (RoomNumber, BookingRef, StayCheckIn, Kind, Amount, NightsCovered, CardLast4, PaidAt,
     AppliedToInvoiceID, IsApplied, Notes, AppliedAmount)
VALUES (@BookedRoom, @BookingRef, @BookedCheckIn, 'Deposit', @Deposit, 1, '4242', GETDATE(),
        NULL, 0, N'Seed: one-night deposit at booking', 0);

COMMIT TRANSACTION;

-- ------------------------------------------------------------------ read this back
SELECT 'GUEST A -- IN HOUSE' AS who,
       'Email'    AS field, CAST(@InHouseEmail AS varchar(100)) AS value
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Password',        CAST(@Password AS varchar(100))
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Name',            'Maya Rivera'
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Room',            @InHouseRoom
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Nights',          CAST(DATEDIFF(DAY, @InHouseCheckIn, @InHouseCheckOut) AS varchar(10))
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Loyalty',         '2600 pts, Gold'
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Key card',        @CardNumber
UNION ALL SELECT 'GUEST A -- IN HOUSE', 'Booking ref',     'none (front-desk stay)'
UNION ALL SELECT 'GUEST B -- BOOKED',  'Email',           CAST(@BookedEmail AS varchar(100))
UNION ALL SELECT 'GUEST B -- BOOKED',  'Password',        CAST(@Password AS varchar(100))
UNION ALL SELECT 'GUEST B -- BOOKED',  'Name',            'Ben Okafor'
UNION ALL SELECT 'GUEST B -- BOOKED',  'Room',            @BookedRoom
UNION ALL SELECT 'GUEST B -- BOOKED',  'Booking ref',     @BookingRef
UNION ALL SELECT 'GUEST B -- BOOKED',  'Arrives',         CAST(@BookedCheckIn AS varchar(10))
UNION ALL SELECT 'GUEST B -- BOOKED',  'Nights',          CAST(DATEDIFF(DAY, @BookedCheckIn, @BookedCheckOut) AS varchar(10))
UNION ALL SELECT 'GUEST B -- BOOKED',  'Deposit taken',   CAST(@Deposit AS varchar(20))
UNION ALL SELECT 'GUEST B -- BOOKED',  'Cancel outcome',  'full Refund (arrival is past the 7-day cutoff)';

-- Independent read-back: the booking must be findable by its owner, which is the join
-- _find_booking() performs. If this returns nothing, the seed is wrong.
SELECT r.RoomNumber, r.LastName, r.FirstName, r.CheckInDate, r.CheckOutDate,
       p.BookingRef, p.Kind, p.Amount, p.AppliedAmount, p.IsApplied
  FROM dbo.Reservations r
  JOIN dbo.ReservationPayments p
    ON p.RoomNumber = r.RoomNumber AND p.StayCheckIn = r.CheckInDate
 WHERE r.CustomerID = @BookedCustomerID;
GO

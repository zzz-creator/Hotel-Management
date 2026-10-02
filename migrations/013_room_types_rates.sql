-- Migration: 013_room_types_rates.sql
-- Introduces nightly room rates and splits the guest folio into two charge groups.
--
-- Before this migration the app only ever billed room service: `Items` held
-- sellable items/services, `Transactions` only ever contained `order_item()` rows,
-- and a multi-night stay with no room service produced a $0.00 invoice. This
-- migration adds a per-room-type nightly rate and a 'Room' folio line posted at
-- check-out.
--
--   RoomTypes            : canonical list of room categories + editable nightly rate
--   Transactions.Description : human label for lines with no Items row (the room charge)
--   Transactions.ChargeGroup : 'Room' or 'F&B' -- tags every folio line
--   Invoices.* (group columns) : per-group subtotal/discount/tax breakdown. The existing
--                               Subtotal/TotalAmount/AmountPaid remain GRAND TOTALS so
--                               current reports and print_invoice() totals keep working.
--
-- Business rules enforced in code (see PLAN-room-rates-and-folios.md):
--   * Discount codes and loyalty tier discounts apply to the F&B group ONLY.
--   * The room charge is never discounted.
--   * Both groups are taxed, each at the existing single `tax_rate` setting.
--   * `award_billed_order_points()` filters ChargeGroup = 'F&B' so a guest cannot
--     earn points twice for one stay (once for the nights, once for the room spend).
--
-- Requires migrations 001-012. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-012.
USE hotelSystem
GO

/* ============================ RoomTypes ============================ */
IF OBJECT_ID('dbo.RoomTypes', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.RoomTypes (
        RoomType     VARCHAR(50)   NOT NULL,
        NightlyRate  DECIMAL(10,2) NOT NULL CONSTRAINT DF_RoomTypes_NightlyRate DEFAULT (0),
        Description  VARCHAR(255)  NULL,
        Active       BIT           NOT NULL CONSTRAINT DF_RoomTypes_Active DEFAULT (1),
        CONSTRAINT PK_RoomTypes PRIMARY KEY CLUSTERED (RoomType)
    );
END
GO

-- Seed the canonical room categories, matching the seven categories in
-- migrations/008_rooms_seed.sql and DEFAULT_ROOM_TYPE_MULTIPLIERS in the app, so
-- nightly rates and loyalty multipliers line up with the seeded Rooms.RoomType values.
-- Admins can change every rate from the "Pricing & Settings" menu.
IF NOT EXISTS (SELECT 1 FROM dbo.RoomTypes)
BEGIN
    INSERT INTO dbo.RoomTypes (RoomType, NightlyRate, Description, Active) VALUES
        ('Standard',           120.00, 'Standard guest room.',                     1),
        ('Deluxe',             180.00, 'Deluxe room with upgraded amenities.',    1),
        ('Junior Suite',       260.00, 'Junior suite with separate sitting area.',1),
        ('Suite',              400.00, 'Full suite with lounge.',                 1),
        ('Grand Suite',        650.00, 'Grand suite with dining area.',          1),
        ('Penthouse',         1200.00, 'Penthouse with private terrace.',         1),
        ('Presidential Suite',2500.00, 'Presidential suite, top floor.',           1);
END
GO

/* ================== Transactions: description + group ================== */
IF COL_LENGTH('dbo.Transactions', 'Description') IS NULL
    ALTER TABLE dbo.Transactions ADD Description NVARCHAR(100) NULL;
GO

IF COL_LENGTH('dbo.Transactions', 'ChargeGroup') IS NULL
    ALTER TABLE dbo.Transactions
        ADD ChargeGroup VARCHAR(10) NOT NULL
        CONSTRAINT DF_Transactions_ChargeGroup DEFAULT ('F&B');
GO

-- Index folio reads by group; billing filters on (RoomNumber, IsBilled, ChargeGroup).
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Transactions_ChargeGroup' AND object_id = OBJECT_ID('dbo.Transactions'))
    CREATE INDEX IX_Transactions_ChargeGroup ON dbo.Transactions (ChargeGroup);
GO

/* ============== Invoices: per-group breakdown (grand totals kept) ============== */
IF COL_LENGTH('dbo.Invoices', 'RoomSubtotal') IS NULL
    ALTER TABLE dbo.Invoices ADD RoomSubtotal DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_RoomSubtotal DEFAULT (0);
GO
IF COL_LENGTH('dbo.Invoices', 'RoomTaxAmount') IS NULL
    ALTER TABLE dbo.Invoices ADD RoomTaxAmount DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_RoomTaxAmount DEFAULT (0);
GO
IF COL_LENGTH('dbo.Invoices', 'RoomTotal') IS NULL
    ALTER TABLE dbo.Invoices ADD RoomTotal DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_RoomTotal DEFAULT (0);
GO
IF COL_LENGTH('dbo.Invoices', 'FnbSubtotal') IS NULL
    ALTER TABLE dbo.Invoices ADD FnbSubtotal DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_FnbSubtotal DEFAULT (0);
GO
IF COL_LENGTH('dbo.Invoices', 'FnbDiscountCodeAmount') IS NULL
    ALTER TABLE dbo.Invoices ADD FnbDiscountCodeAmount DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_FnbDiscountCodeAmount DEFAULT (0);
GO
IF COL_LENGTH('dbo.Invoices', 'FnbTierDiscountAmount') IS NULL
    ALTER TABLE dbo.Invoices ADD FnbTierDiscountAmount DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_FnbTierDiscountAmount DEFAULT (0);
GO
IF COL_LENGTH('dbo.Invoices', 'FnbTaxAmount') IS NULL
    ALTER TABLE dbo.Invoices ADD FnbTaxAmount DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_FnbTaxAmount DEFAULT (0);
GO

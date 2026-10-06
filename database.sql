-- ===========================================================================
-- Fresh install. Authoritative schema for a NEW database.
--
-- This script assumes the server does not have the database yet: it creates
-- hotelSystem if absent, then builds the full schema and the seed rows. It is
-- self-contained -- every table, constraint and index is declared here, and
-- nothing in it depends on a file in migrations/. If you are upgrading a
-- database that already has data, apply the numbered files in migrations/ in
-- order INSTEAD of re-running this; running it against a populated database is
-- not an upgrade path.
--
-- CREATE DATABASE cannot run inside a transaction or a multi-statement batch
-- with other statements, so the IF and the CREATE sit in one batch and the USE
-- follows in the next.
--
-- After this completes, the hotel still has no rooms and no items. That is
-- deliberate -- see docs/ONBOARDING.md for the staff first-run order.
-- ===========================================================================
IF DB_ID(N'hotelSystem') IS NULL
    CREATE DATABASE hotelSystem
GO
USE [hotelSystem]
GO
/****** Object:  Table [dbo].[CustomerProfiles]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[CustomerProfiles](
	[CustomerID] [int] IDENTITY(1,1) NOT NULL,
	[LastName] [varchar](50) NOT NULL,
	[FirstName] [varchar](50) NOT NULL,
	[Email] [varchar](100) NULL,
	[Phone] [varchar](20) NULL,
	[Preferences] [varchar](255) NULL,
	[Password] [nvarchar](100) NULL,
	[CreatedAt] [datetime] NOT NULL CONSTRAINT [DF_CustomerProfiles_CreatedAt] DEFAULT (GETDATE()),
PRIMARY KEY CLUSTERED 
(
	[CustomerID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
-- Login identity. FILTERED, not a plain unique index: check_in() upserts a profile
-- from the name alone and leaves Email NULL, and SQL Server permits only one NULL in a
-- unique index, so the second walk-in profile would otherwise fail to insert.
-- Email is unique whenever present; the booking login refuses a blank one.
CREATE UNIQUE NONCLUSTERED INDEX [UX_CustomerProfiles_Email] ON [dbo].[CustomerProfiles]
(
	[Email] ASC
)WHERE [Email] IS NOT NULL
WITH (PAD_INDEX = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Discounts]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Discounts](
	[Code] [varchar](50) NOT NULL,
	[DiscountPercentage] [decimal](5, 2) NOT NULL,
	[CreatedAt] [datetime] NULL,
PRIMARY KEY CLUSTERED 
(
	[Code] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Items]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Items](
	[ItemID] [int] NOT NULL,
	[Name] [nvarchar](100) NULL,
	[Price] [decimal](10, 2) NULL,
	[PricingRule] [nvarchar](50) NULL,
PRIMARY KEY CLUSTERED 
(
	[ItemID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[LoyaltyAccounts]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
-- Loyalty belongs to a PERSON, not a room (migration 019). RoomNumber is only the last
-- room the guest stayed in: it is display context, not a key, and is deliberately not
-- unique. Two guests may have stayed in 9012 without ever sharing a balance.
CREATE TABLE [dbo].[LoyaltyAccounts](
	[CustomerID] [int] NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[Points] [int] NOT NULL,
	[Tier] [nvarchar](50) NULL,
	[LastUpdated] [datetime] NULL,
PRIMARY KEY CLUSTERED 
(
	[CustomerID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[LoyaltyAccounts]  WITH CHECK ADD  CONSTRAINT [FK_LoyaltyAccounts_CustomerProfiles] FOREIGN KEY([CustomerID])
REFERENCES [dbo].[CustomerProfiles] ([CustomerID])
GO
CREATE NONCLUSTERED INDEX [IX_LoyaltyAccounts_RoomNumber] ON [dbo].[LoyaltyAccounts]
(
	[RoomNumber] ASC
)WITH (PAD_INDEX = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[LoyaltyTransactions]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[LoyaltyTransactions](
	[ID] [int] IDENTITY(1,1) NOT NULL,
	[CustomerID] [int] NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[Delta] [int] NULL,
	[Reason] [nvarchar](255) NULL,
	[CreatedAt] [datetime] NULL,
	[SourceID] [nvarchar](100) NULL,
PRIMARY KEY CLUSTERED 
(
	[ID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
-- CustomerID is the account the points belong to; RoomNumber is the stay CONTEXT, i.e.
-- where the points were earned. It is never rewritten when a guest moves rooms, because
-- that would falsify their earning history.
ALTER TABLE [dbo].[LoyaltyTransactions]  WITH CHECK ADD  CONSTRAINT [FK_LoyaltyTransactions_CustomerProfiles] FOREIGN KEY([CustomerID])
REFERENCES [dbo].[CustomerProfiles] ([CustomerID])
GO
CREATE NONCLUSTERED INDEX [IX_LoyaltyTransactions_CustomerID] ON [dbo].[LoyaltyTransactions]
(
	[CustomerID] ASC
)WITH (PAD_INDEX = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Reservations]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Reservations](
	[RoomNumber] [nvarchar](50) NOT NULL,
	[LastName] [nvarchar](50) NULL,
	[FirstName] [varchar](50) NULL,
	[Floor] [int] NULL,
	-- These two defaults are NAMED on purpose, unlike most of the defaults in this file.
	-- migrations/003_reservations.sql adds them under these names and guards on those
	-- names, so an unnamed declaration here would make 003's guard miss an existing
	-- constraint and then collide with it (Msg 1781, "Column already has a DEFAULT bound
	-- to it") the moment that file ran against a fresh install. The live database carries
	-- exactly these names for the same reason.
	[CheckInDate] [date] NOT NULL CONSTRAINT [DF_Reservations_CheckInDate] DEFAULT (CAST(GETDATE() AS DATE)),
	[CheckOutDate] [date] NOT NULL CONSTRAINT [DF_Reservations_CheckOutDate] DEFAULT (CAST(DATEADD(DAY, 1, GETDATE()) AS DATE)),
	-- The guest who owns this stay (migration 019), which is what the loyalty award and
	-- the booking-ownership check resolve. NULL is legal and expected: front-desk
	-- bookings have no online account, and pre-019 reservations predate the column.
	-- It is CLEARED when a room is re-let, so the next guest never inherits the link.
	[CustomerID] [int] NULL,
	-- The per-night rate in force when the stay was MADE (migration 022), which is what
	-- check-out bills. A rate card is a price list; a confirmed booking is a contract, so
	-- an admin rate edit between booking and arrival must not re-price the stay.
	-- NULL means "this stay predates rate capture" and makes check-out fall back to the
	-- current category rate and say so; it is never 0, because 0 would be
	-- indistinguishable from a genuinely free room.
	[NightlyRate] [decimal](10, 2) NULL,
PRIMARY KEY CLUSTERED 
(
	[RoomNumber] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[Reservations]  WITH CHECK ADD  CONSTRAINT [FK_Reservations_CustomerProfiles] FOREIGN KEY([CustomerID])
REFERENCES [dbo].[CustomerProfiles] ([CustomerID])
GO
CREATE NONCLUSTERED INDEX [IX_Reservations_CustomerID] ON [dbo].[Reservations]
(
	[CustomerID] ASC
)WITH (PAD_INDEX = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Rooms]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Rooms](
	[RoomNumber] [varchar](10) NOT NULL,
	[RoomType] [varchar](50) NOT NULL,
	[Description] [varchar](255) NULL,
	[Status] [varchar](20) NULL,
PRIMARY KEY CLUSTERED 
(
	[RoomNumber] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Transactions]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Transactions](
	[ID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[ItemID] [int] NULL,
	[Quantity] [int] NOT NULL DEFAULT ((1)),
	[UnitPrice] [decimal](10, 2) NULL,
	[Amount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[IsBilled] [bit] NOT NULL DEFAULT ((0)),
	[InvoiceID] [int] NULL,
	[PaidEarlier] [bit] NOT NULL DEFAULT ((0)),
PRIMARY KEY CLUSTERED 
(
	[ID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Users]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Users](
	[Username] [nvarchar](50) NOT NULL,
	[Password] [nvarchar](50) NULL,
	[FailedAttempts] [int] NULL,
	[LockoutTime] [datetime] NULL,
	[Role] [nvarchar](50) NULL,
PRIMARY KEY CLUSTERED 
(
	[Username] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[ValetVehicles]    Script Date: 6/17/2026 6:18:01 AM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[ValetVehicles](
	[VehicleID] [int] IDENTITY(1,1) NOT NULL,
	[LicensePlate] [nvarchar](20) NOT NULL,
	[OwnerName] [nvarchar](100) NOT NULL,
	[ParkingSpot] [nvarchar](20) NULL,
	[Status] [nvarchar](20) NULL,
	[CheckInTime] [datetime] NULL,
	[CheckOutTime] [datetime] NULL,
PRIMARY KEY CLUSTERED 
(
	[VehicleID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[Discounts] ADD  DEFAULT (getdate()) FOR [CreatedAt]
GO
ALTER TABLE [dbo].[LoyaltyAccounts] ADD  DEFAULT ((0)) FOR [Points]
GO
ALTER TABLE [dbo].[LoyaltyTransactions] ADD  DEFAULT (getdate()) FOR [CreatedAt]
GO
ALTER TABLE [dbo].[Rooms] ADD  DEFAULT ('Available') FOR [Status]
GO
ALTER TABLE [dbo].[Users] ADD  DEFAULT ((0)) FOR [FailedAttempts]
GO
ALTER TABLE [dbo].[ValetVehicles] ADD  DEFAULT (getdate()) FOR [CheckInTime]
GO
ALTER TABLE [dbo].[ValetVehicles]  WITH CHECK ADD CHECK  (([Status]='Checked-Out' OR [Status]='Checked-In'))
GO
ALTER TABLE [dbo].[Reservations]  WITH CHECK ADD CONSTRAINT [CH_Reservations_DateRange] CHECK  (([CheckOutDate] > [CheckInDate]))
GO
ALTER TABLE [dbo].[Transactions]  WITH CHECK ADD CONSTRAINT [FK_Transactions_Reservations] FOREIGN KEY([RoomNumber])
REFERENCES [dbo].[Reservations] ([RoomNumber])
GO
ALTER TABLE [dbo].[Transactions] CHECK CONSTRAINT [FK_Transactions_Reservations]
GO
CREATE INDEX [IX_Transactions_RoomNumber] ON [dbo].[Transactions] ([RoomNumber])
GO
CREATE INDEX [IX_Transactions_InvoiceID] ON [dbo].[Transactions] ([InvoiceID])
GO
/****** Object:  Table [dbo].[HotelSettings]    Script Date: 9/14/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[HotelSettings](
	[SettingKey] [nvarchar](50) NOT NULL,
	[SettingValue] [nvarchar](100) NULL,
PRIMARY KEY CLUSTERED 
(
	[SettingKey] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
-- Seed default values (mirror config.ini). Editable from the admin "Pricing & Settings" menu.
-- 'business_date' (migration 023) is the hotel's single clock for "which day is it": every
-- "today" in the app reads it, and the admin "Business Date" menu advances it one day at
-- a time. It is seeded to the day this script is run, which is what preserves the meaning
-- of every date already on record -- before it existed, "today" was the wall clock.
-- The value is a CONVERT, not GETDATE() itself, so the string is always ISO YYYY-MM-DD and
-- the two readers in the app can parse it without guessing a locale.
-- There is deliberately no 'loyalty_expiration_days' row: it used to be seeded here and
-- shown on the settings screen, but nothing ever read it to expire anything. Migration 025
-- removes it. Points do not expire.
INSERT INTO [dbo].[HotelSettings] ([SettingKey], [SettingValue]) VALUES
    (N'business_date', CONVERT(NVARCHAR(10), CAST(GETDATE() AS DATE), 23)),
    (N'peak_factor', N'1.20'),
    (N'offpeak_factor', N'0.90'),
    (N'tax_rate', N'0.13'),
    -- A FRACTION, and deliberately below what a night's stay earns. A Standard night's
    -- 100 points on a $120 room is 0.83 pts/$, so 0.5 keeps the room the better prize:
    -- at the old rate of 3 the most profitable thing a guest could do was order a drink
    -- (migration 024). It is read as a float, never an int -- int(0.5) is 0, which would
    -- have silently paid nothing for every order.
    (N'loyalty_accrual_points_per_unit', N'0.5'),
    (N'loyalty_redemption_points_per_currency_unit', N'100'),
    (N'loyalty_points_per_night', N'100'),
    (N'booking_refund_cutoff_days', N'7');
GO
-- Per-room-category points multipliers (editable from "Pricing & Settings").
INSERT INTO [dbo].[HotelSettings] ([SettingKey], [SettingValue]) VALUES
    (N'loyalty_mult_standard', N'1.0'),
    (N'loyalty_mult_deluxe', N'1.5'),
    (N'loyalty_mult_junior_suite', N'2.0'),
    (N'loyalty_mult_suite', N'3.0'),
    (N'loyalty_mult_grand_suite', N'4.0'),
    (N'loyalty_mult_penthouse', N'6.0'),
    (N'loyalty_mult_presidential_suite', N'8.0');
GO
/****** Object:  Table [dbo].[LoyaltyTiers]    Script Date: 9/14/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[LoyaltyTiers](
	[TierName] [nvarchar](50) NOT NULL,
	[MinLifetimePoints] [int] NOT NULL DEFAULT ((0)),
	[PointsMultiplier] [decimal](5, 2) NOT NULL DEFAULT ((1.00)),
	[DiscountPercent] [decimal](5, 2) NOT NULL DEFAULT ((0)),
	[Perks] [nvarchar](500) NOT NULL DEFAULT (N''),
PRIMARY KEY CLUSTERED
(
	[TierName] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
-- Seed default loyalty tiers. Tiers are based on lifetime points earned (editable from admin "Loyalty Management").
INSERT INTO [dbo].[LoyaltyTiers] ([TierName], [MinLifetimePoints], [PointsMultiplier], [DiscountPercent], [Perks]) VALUES
    (N'Bronze', 0, 1.00, 0, N'Standard points accrual. No extra perks.'),
    (N'Silver', 1000, 1.25, 5, N'+25% points on orders; 5% discount on room service; priority concierge.'),
    (N'Gold', 2500, 1.50, 10, N'+50% points on orders; 10% discount on room service; complimentary late check-out; free fitness class.'),
    (N'Platinum', 7500, 2.00, 15, N'2x points on orders; 15% discount on room service; complimentary breakfast; spa credit; dedicated concierge line.');
GO
/****** Object:  Table [dbo].[Invoices]    Script Date: 9/15/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Invoices](
	[InvoiceID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NOT NULL,
	[InvoiceDate] [datetime] NOT NULL DEFAULT (getdate()),
	[Subtotal] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[DiscountCodeAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[TierDiscountAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[TaxAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[TotalAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[PointsRedeemed] [int] NOT NULL DEFAULT ((0)),
	[RedemptionValue] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[AmountPaid] [decimal](12, 2) NOT NULL DEFAULT ((0)),
PRIMARY KEY CLUSTERED 
(
	[InvoiceID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_Invoices_RoomNumber] ON [dbo].[Invoices] ([RoomNumber])
GO
ALTER TABLE [dbo].[Transactions] WITH CHECK ADD CONSTRAINT [FK_Transactions_Invoices] FOREIGN KEY ([InvoiceID])
REFERENCES [dbo].[Invoices] ([InvoiceID])
GO
/****** Object:  Table [dbo].[RoomTypes]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[RoomTypes](
	[RoomType] [varchar](50) NOT NULL,
	[NightlyRate] [decimal](10, 2) NOT NULL DEFAULT ((0)),
	[Description] [varchar](255) NULL,
	[Active] [bit] NOT NULL DEFAULT ((1)),
PRIMARY KEY CLUSTERED
(
	[RoomType] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
-- Default nightly rates. Editable from the admin "Pricing & Settings" menu.
INSERT INTO [dbo].[RoomTypes] ([RoomType], [NightlyRate], [Description], [Active]) VALUES
	(N'Standard', 129.00, N'Standard room.', 1),
	(N'Deluxe', 189.00, N'Deluxe room.', 1),
	(N'Junior Suite', 289.00, N'Junior suite.', 1),
	(N'Suite', 429.00, N'Suite.', 1),
	(N'Grand Suite', 749.00, N'Grand suite.', 1),
	(N'Penthouse', 1899.00, N'Penthouse.', 1),
	(N'Presidential Suite', 3499.00, N'Presidential suite.', 1)
GO
/****** Object:  Table [dbo].[ReservationArchive]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
-- Reservations.RoomNumber is the primary key, so a room can only hold one live booking.
-- Re-booking a room archives the completed stay here so its history is never lost.
-- Nights is snapshotted so a later edit to the live row cannot rewrite history.
CREATE TABLE [dbo].[ReservationArchive](
	[ArchiveID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NOT NULL,
	[Floor] [int] NULL,
	[LastName] [nvarchar](50) NULL,
	[FirstName] [varchar](50) NULL,
	[CheckInDate] [date] NOT NULL,
	[CheckOutDate] [date] NOT NULL,
	[Nights] [int] NOT NULL,
	[RoomType] [varchar](50) NULL,
	[NightlyRate] [decimal](10, 2) NULL,
	[ArchivedAt] [datetime] NOT NULL CONSTRAINT [DF_ReservationArchive_ArchivedAt] DEFAULT (getdate()),
PRIMARY KEY CLUSTERED
(
	[ArchiveID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
-- Availability search scans by room then by date window.
CREATE INDEX [IX_ReservationArchive_RoomDate] ON [dbo].[ReservationArchive] ([RoomNumber], [CheckInDate], [CheckOutDate])
GO
/****** Object:  Table [dbo].[GuestRequests]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[GuestRequests](
	[RequestID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[Category] [nvarchar](50) NOT NULL,
	[Message] [nvarchar](1000) NOT NULL,
	[Status] [nvarchar](20) NOT NULL DEFAULT ('Open'),
	[Priority] [nvarchar](20) NOT NULL DEFAULT ('Normal'),
	[CreatedBy] [nvarchar](100) NULL,
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[ResolvedBy] [nvarchar](100) NULL,
	[ResolvedAt] [datetime] NULL,
	[Notes] [nvarchar](1000) NULL,
PRIMARY KEY CLUSTERED
(
	[RequestID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_GuestRequests_Status] ON [dbo].[GuestRequests] ([Status])
GO
/****** Object:  Table [dbo].[ConciergeRequests]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[ConciergeRequests](
	[RequestID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[LastName] [nvarchar](50) NULL,
	[FirstName] [nvarchar](50) NULL,
	[Message] [nvarchar](1000) NOT NULL,
	[Status] [nvarchar](20) NOT NULL DEFAULT ('Open'),
	[Response] [nvarchar](1000) NULL,
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[ResolvedAt] [datetime] NULL,
	[ResolvedBy] [nvarchar](50) NULL,
PRIMARY KEY CLUSTERED
(
	[RequestID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_ConciergeRequests_RoomNumber] ON [dbo].[ConciergeRequests] ([RoomNumber])
GO
/****** Object:  Table [dbo].[Feedback]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Feedback](
	[FeedbackID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[LastName] [nvarchar](50) NULL,
	[FirstName] [nvarchar](50) NULL,
	[Rating] [int] NOT NULL,
	[Comments] [nvarchar](1000) NULL,
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
PRIMARY KEY CLUSTERED
(
	[FeedbackID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Orders]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Orders](
	[OrderID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[Status] [nvarchar](20) NOT NULL DEFAULT ('Placed'),
	[PlacedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[UpdatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[CompletedAt] [datetime] NULL,
	[Notes] [nvarchar](500) NULL,
PRIMARY KEY CLUSTERED
(
	[OrderID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_Orders_RoomNumber] ON [dbo].[Orders] ([RoomNumber])
GO
CREATE INDEX [IX_Orders_Status] ON [dbo].[Orders] ([Status])
GO
/****** Object:  Table [dbo].[OrderItems]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[OrderItems](
	[OrderItemID] [int] IDENTITY(1,1) NOT NULL,
	[OrderID] [int] NOT NULL,
	[ItemID] [int] NULL,
	[ItemName] [nvarchar](200) NOT NULL,
	[Quantity] [int] NOT NULL,
	[UnitPrice] [decimal](10, 2) NOT NULL,
PRIMARY KEY CLUSTERED
(
	[OrderItemID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[OrderItems] WITH CHECK ADD CONSTRAINT [FK_OrderItems_Orders] FOREIGN KEY ([OrderID])
REFERENCES [dbo].[Orders] ([OrderID]) ON DELETE CASCADE
GO
/****** Object:  Table [dbo].[Amenities]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Amenities](
	[AmenityID] [int] IDENTITY(1,1) NOT NULL,
	[Name] [nvarchar](200) NOT NULL,
	[Description] [nvarchar](500) NULL,
	[DisplayOrder] [int] NOT NULL DEFAULT ((0)),
	[Active] [bit] NOT NULL DEFAULT ((1)),
PRIMARY KEY CLUSTERED
(
	[AmenityID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Promotions]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Promotions](
	[PromotionID] [int] IDENTITY(1,1) NOT NULL,
	[Title] [nvarchar](200) NOT NULL,
	[Details] [nvarchar](1000) NULL,
	[DiscountCode] [varchar](50) NULL,
	[StartsOn] [date] NULL,
	[EndsOn] [date] NULL,
	[Active] [bit] NOT NULL DEFAULT ((1)),
PRIMARY KEY CLUSTERED
(
	[PromotionID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[Notifications]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[Notifications](
	[NotificationID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[Message] [nvarchar](1000) NOT NULL,
	[Channel] [nvarchar](20) NOT NULL DEFAULT ('In-Room'),
	[SentBy] [nvarchar](50) NULL,
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[ReadAt] [datetime] NULL,
PRIMARY KEY CLUSTERED
(
	[NotificationID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_Notifications_RoomNumber] ON [dbo].[Notifications] ([RoomNumber], [CreatedAt])
GO
/****** Object:  Table [dbo].[StaffAlerts]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[StaffAlerts](
	[AlertID] [int] IDENTITY(1,1) NOT NULL,
	[TargetRole] [nvarchar](50) NOT NULL,
	[Message] [nvarchar](1000) NOT NULL,
	[Severity] [nvarchar](20) NOT NULL DEFAULT ('Info'),
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[AcknowledgedAt] [datetime] NULL,
	[AcknowledgedBy] [nvarchar](50) NULL,
PRIMARY KEY CLUSTERED
(
	[AlertID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
/****** Object:  Table [dbo].[AuditLog]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[AuditLog](
	[AuditID] [int] IDENTITY(1,1) NOT NULL,
	[CreatedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[Username] [nvarchar](50) NOT NULL DEFAULT (N'system'),
	[Action] [nvarchar](20) NOT NULL,
	[EntityType] [nvarchar](50) NOT NULL,
	[EntityID] [nvarchar](100) NULL,
	[Details] [nvarchar](1000) NULL,
	[OldValue] [nvarchar](500) NULL,
	[NewValue] [nvarchar](500) NULL,
PRIMARY KEY CLUSTERED
(
	[AuditID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_AuditLog_CreatedAt] ON [dbo].[AuditLog] ([CreatedAt])
GO
/****** Object:  Table [dbo].[KeyCards]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[KeyCards](
	[CardID] [int] IDENTITY(1,1) NOT NULL,
	[CardNumber] [nvarchar](30) NOT NULL,
	[RoomNumber] [nvarchar](50) NOT NULL,
	[LastName] [nvarchar](50) NULL,
	[FirstName] [nvarchar](50) NULL,
	[Status] [nvarchar](20) NOT NULL DEFAULT ('Active'),
	[IssuedAt] [datetime] NOT NULL DEFAULT (getdate()),
	[ExpiresAt] [datetime] NULL,
	[IssuedBy] [nvarchar](50) NULL,
	[RevokedAt] [datetime] NULL,
	[RevokeReason] [nvarchar](200) NULL,
PRIMARY KEY CLUSTERED
(
	[CardID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[KeyCards] ADD CONSTRAINT [UQ_KeyCards_CardNumber] UNIQUE ([CardNumber])
GO
CREATE INDEX [IX_KeyCards_RoomNumber] ON [dbo].[KeyCards] ([RoomNumber], [Status])
GO
/****** Object:  Table [dbo].[DoorEvents]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[DoorEvents](
	[EventID] [int] IDENTITY(1,1) NOT NULL,
	[CardNumber] [nvarchar](30) NULL,
	[RoomNumber] [nvarchar](50) NULL,
	[Result] [nvarchar](10) NOT NULL,
	[Detail] [nvarchar](200) NULL,
	[EventTime] [datetime] NOT NULL DEFAULT (getdate()),
PRIMARY KEY CLUSTERED
(
	[EventID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
CREATE INDEX [IX_DoorEvents_Time] ON [dbo].[DoorEvents] ([EventTime])
GO
/****** Migration 013: folio split + invoice breakdown ******/
-- Both columns match migrations/013_room_types_rates.sql exactly, which a live database
-- built by the migrations proves: ChargeGroup is VARCHAR (not NVARCHAR) and NOT NULL with
-- the 'F&B' default, because pre-013 folio rows are tagged F&B rather than left NULL --
-- award_billed_order_points() treats NULL and 'F&B' the same, but the constraint is what
-- stops a NULL slipping in. Description is the free-text label printed on the folio line.
ALTER TABLE [dbo].[Transactions] ADD [ChargeGroup] [varchar](10) NOT NULL CONSTRAINT [DF_Transactions_ChargeGroup] DEFAULT ('F&B'),
	[Description] [nvarchar](100) NULL
GO
ALTER TABLE [dbo].[Invoices] ADD [RoomSubtotal] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[RoomTaxAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[RoomTotal] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[FnbSubtotal] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[FnbDiscountCodeAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[FnbTierDiscountAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
	[FnbTaxAmount] [decimal](12, 2) NOT NULL DEFAULT ((0))
GO
/****** Object:  Table [dbo].[ReservationPayments]    Script Date: 9/25/2026 ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[ReservationPayments](
	[PaymentID] [int] IDENTITY(1,1) NOT NULL,
	[RoomNumber] [nvarchar](50) NOT NULL,
	[BookingRef] [varchar](20) NOT NULL,
	[StayCheckIn] [date] NOT NULL,
	[Kind] [varchar](10) NOT NULL,
	[Amount] [decimal](12, 2) NOT NULL,
	[NightsCovered] [int] NULL,
	[CardLast4] [varchar](4) NULL,
	[PaidAt] [datetime] NOT NULL DEFAULT (getdate()),
	[AppliedToInvoiceID] [int] NULL,
	[IsApplied] [bit] NOT NULL DEFAULT ((0)),
	[Notes] [nvarchar](200) NULL,
	-- Migration 020: how much of this row has been given to check-out invoices. Kept
	-- separate from IsApplied (a whole-row flag) because a booking quote is only an
	-- estimate, so a bill can legitimately be smaller than the prepayment. See
	-- migrations/020_partial_booking_credit.sql.
	[AppliedAmount] [decimal](12, 2) NOT NULL DEFAULT ((0)),
PRIMARY KEY CLUSTERED 
(
	[PaymentID] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
ALTER TABLE [dbo].[ReservationPayments]  WITH CHECK ADD  CONSTRAINT [CK_ReservationPayments_Kind] CHECK  (([Kind]='Deposit' OR [Kind]='Prepayment' OR [Kind]='Refund' OR [Kind]='Forfeit'))
GO
ALTER TABLE [dbo].[ReservationPayments]  WITH CHECK ADD  CONSTRAINT [CK_ReservationPayments_AppliedAmount] CHECK  (([AppliedAmount]>=(0) AND ([Amount]<=(0) OR [AppliedAmount]<=[Amount])))
GO
CREATE INDEX [IX_ReservationPayments_Stay] ON [dbo].[ReservationPayments] ([RoomNumber], [StayCheckIn], [IsApplied])
GO
CREATE INDEX [IX_ReservationPayments_BookingRef] ON [dbo].[ReservationPayments] ([BookingRef])
GO
-- Migration 021: one booking per reference, enforced by the database. Filtered to the two
-- kinds that appear once per booking, because a cancellation writes a second row carrying
-- the SAME reference. See migrations/021_booking_ref_uniqueness.sql.
-- The filter uses IN rather than `([Kind]='Deposit' OR [Kind]='Prepayment')`: SQL Server
-- rejects an OR between two equality predicates in a filtered index's WHERE clause with
-- Msg 156, "Incorrect syntax near the keyword 'OR'". migrations/021 writes the same index
-- in this IN form, which is why an upgraded database has it and a fresh install once did
-- not -- nothing caught the difference because the live catalog comparison in AGENTS.md
-- section 7 sees the finished index, not whether this statement parses.
CREATE UNIQUE INDEX [UX_ReservationPayments_BookingRef_Charge] ON [dbo].[ReservationPayments] ([BookingRef]) WHERE [Kind] IN ('Deposit', 'Prepayment')
GO
/****** Migration 014: historical stays for re-booked rooms ******/
CREATE INDEX [IX_Reservations_CheckOutDate] ON [dbo].[Reservations] ([CheckOutDate])
GO
/****** Migration 016: audit log is created above ******/
/****** Migration 018: booking deposits, prepayments and refunds ******/
ALTER TABLE [dbo].[Invoices] ADD [PrepaidAmount] [decimal](12, 2) NOT NULL CONSTRAINT [DF_Invoices_PrepaidAmount] DEFAULT ((0))
GO
/****** Structural parity with a migrated database ******/
-- Everything below already exists on any database built by applying migrations/001-025,
-- but was missing here, so a fresh install from this file shipped with weaker guarantees
-- than a migrated one: a typo could invent a new KeyCards/Orders status, a rating outside
-- 1..5 could be stored, a checkout before check-in could be booked, and a valet vehicle
-- could sit in a state the app never renders. Each object below is named as the migration
-- that creates it is named, with the definition read off the live catalog.
--
-- Constraint NAMES are not free: the CHECK list is the code's defence against a bad write,
-- so these are the same names migrations/015-017 use and the docs refer to.
ALTER TABLE [dbo].[ConciergeRequests]  WITH CHECK ADD  CONSTRAINT [CK_ConciergeRequests_Status] CHECK  (([Status]='Open' OR [Status]='In Progress' OR [Status]='Resolved'))
GO
ALTER TABLE [dbo].[Feedback]  WITH CHECK ADD  CONSTRAINT [CK_Feedback_Rating] CHECK  (([Rating]>=(1) AND [Rating]<=(5)))
GO
-- Status is constrained so a typo cannot invent a new card state. issue_key_card() and
-- revoke_key_card() only ever write the four values listed here; see docs/SCHEMA.md.
ALTER TABLE [dbo].[KeyCards]  WITH CHECK ADD  CONSTRAINT [CK_KeyCards_Status] CHECK  (([Status]='Active' OR [Status]='Revoked' OR [Status]='Lost' OR [Status]='Expired'))
GO
ALTER TABLE [dbo].[Orders]  WITH CHECK ADD  CONSTRAINT [CK_Orders_Status] CHECK  (([Status]='Placed' OR [Status]='Preparing' OR [Status]='Ready' OR [Status]='Delivered' OR [Status]='Completed' OR [Status]='Cancelled'))
GO
ALTER TABLE [dbo].[StaffAlerts]  WITH CHECK ADD  CONSTRAINT [CK_StaffAlerts_Severity] CHECK  (([Severity]='Info' OR [Severity]='Warning' OR [Severity]='Critical'))
GO
-- The one constraint on this list the app names by convention rather than by choice.
-- migrations/010 declared it without a name, so SQL Server auto-named it
-- CK__ValetVehi__Statu__0F624AF8, and that is the name every migrated database carries.
-- It is spelled out here verbatim on purpose: this file builds a schema indistinguishable
-- from a migrated one, and a later script that looks the constraint up by name has to find
-- the same name here that it finds there.
ALTER TABLE [dbo].[ValetVehicles]  WITH CHECK ADD  CONSTRAINT [CK__ValetVehi__Statu__0F624AF8] CHECK  (([Status]='Checked-Out' OR [Status]='Checked-In'))
GO
-- Column defaults are NOT repeated here. Rooms.Status matters most: the room dashboard and
-- housekeeping report key on 'Available', so without it a row inserted by anything other
-- than upsert_room_if_missing() has a NULL status. All six -- Discounts.CreatedAt,
-- LoyaltyAccounts.Points, LoyaltyTransactions.CreatedAt, Rooms.Status,
-- Users.FailedAttempts and ValetVehicles.CheckInTime -- are already declared by the
-- unnamed `ALTER TABLE ... ADD DEFAULT ... FOR` statements near the top of this file.
--
-- They are declared there UNNAMED on purpose. That is how the migrations created them, so
-- the live database carries SQL Server's generated names (DF__Discounts__Creat__49C3F6B7,
-- DF__Rooms__Status__75A278F5, ...) rather than readable ones. A second block here used to
-- add the same six defaults again under hand-written names; SQL Server rejected that with
-- Msg 1781, "Column already has a DEFAULT bound to it", so a fresh install died partway
-- through. Looking a constraint up by a name it was never given is the trap AGENTS.md
-- section 4 warns about for primary keys, and it applies to defaults too.
GO
-- Secondary indexes. Every one of these backs a query the app makes on a screen a user is
-- looking at, so they are correctness-adjacent rather than optional tuning.
CREATE INDEX [IX_AuditLog_Entity] ON [dbo].[AuditLog] ([EntityType], [EntityID])
GO
CREATE INDEX [IX_ConciergeRequests_Status] ON [dbo].[ConciergeRequests] ([Status], [CreatedAt])
GO
CREATE INDEX [IX_DoorEvents_CardNumber] ON [dbo].[DoorEvents] ([CardNumber])
GO
CREATE INDEX [IX_Feedback_RoomNumber] ON [dbo].[Feedback] ([RoomNumber], [CreatedAt])
GO
-- The card number is looked up on every door read.
CREATE INDEX [IX_KeyCards_CardNumber] ON [dbo].[KeyCards] ([CardNumber])
GO
CREATE INDEX [IX_LoyaltyAccounts_Points] ON [dbo].[LoyaltyAccounts] ([Points])
GO
CREATE INDEX [IX_LoyaltyTransactions_RoomNumber] ON [dbo].[LoyaltyTransactions] ([RoomNumber])
GO
CREATE INDEX [IX_OrderItems_OrderID] ON [dbo].[OrderItems] ([OrderID])
GO
CREATE INDEX [IX_Orders_RoomStatus] ON [dbo].[Orders] ([RoomNumber], [Status], [PlacedAt])
GO
CREATE INDEX [IX_Promotions_Active] ON [dbo].[Promotions] ([Active], [StartsOn], [EndsOn])
GO
CREATE INDEX [IX_StaffAlerts_Ack] ON [dbo].[StaffAlerts] ([AcknowledgedAt], [Severity])
GO
-- The folio split filter: billing reads every line of a stay by charge group.
CREATE INDEX [IX_Transactions_ChargeGroup] ON [dbo].[Transactions] ([ChargeGroup])
GO


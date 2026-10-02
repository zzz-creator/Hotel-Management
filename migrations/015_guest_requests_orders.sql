-- Migration: 015_guest_requests_orders.sql
-- Replaces the simulated guest-facing features with real, persisted records.
--
-- Before this migration these features were theatre: provide_feedback() read a rating
-- and discarded it, track_order_status() called random.choice() over a list of statuses,
-- contact_concierge() slept for two seconds and threw the message away, and
-- send_notification_to_customer() / send_alert_to_staff() only printed to the console.
--
--   Feedback         : guest stay ratings (1-5) + comments, viewable by management
--   ConciergeRequests: guest messages, with a staff response and resolution state
--   Notifications    : messages sent to a guest in a room (replaces the print-only path)
--   StaffAlerts      : operational alerts broadcast to a staff role, acknowledgeable
--   Orders / OrderItems : the real room-service order lifecycle, replacing random.choice
--   Amenities        : hotel amenities, admin-editable (was a hardcoded list in code)
--   Promotions       : current offers, admin-editable (was a hardcoded list in code)
--
-- Order status is a real progression advanced by staff:
--   Placed -> Preparing -> Ready -> Delivered -> Completed, or Cancelled at any point.
--
-- No FKs to room numbers: they are free-text `<floor><3-digit-code>` and the schema
-- deliberately avoids constraining them (same reasoning as Transactions and Invoices).
--
-- Requires migrations 001-013. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-013.
USE hotelSystem
GO

/* ============================== Feedback ============================== */
IF OBJECT_ID('dbo.Feedback', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Feedback (
        FeedbackID INT IDENTITY(1,1) NOT NULL,
        RoomNumber NVARCHAR(50)  NULL,
        LastName   NVARCHAR(50)  NULL,
        FirstName  NVARCHAR(50)  NULL,
        Rating     INT           NOT NULL,
        Comments   NVARCHAR(1000) NULL,
        CreatedAt  DATETIME      NOT NULL CONSTRAINT DF_Feedback_CreatedAt DEFAULT (GETDATE()),
        CONSTRAINT PK_Feedback PRIMARY KEY CLUSTERED (FeedbackID),
        CONSTRAINT CK_Feedback_Rating CHECK (Rating BETWEEN 1 AND 5)
    );
END
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Feedback_RoomNumber' AND object_id = OBJECT_ID('dbo.Feedback'))
    CREATE INDEX IX_Feedback_RoomNumber ON dbo.Feedback (RoomNumber, CreatedAt DESC);
GO

/* ========================= ConciergeRequests ========================= */
IF OBJECT_ID('dbo.ConciergeRequests', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConciergeRequests (
        RequestID  INT IDENTITY(1,1) NOT NULL,
        RoomNumber NVARCHAR(50)   NULL,
        LastName   NVARCHAR(50)   NULL,
        FirstName  NVARCHAR(50)   NULL,
        Message    NVARCHAR(1000) NOT NULL,
        Status     NVARCHAR(20)   NOT NULL CONSTRAINT DF_ConciergeRequests_Status DEFAULT ('Open'),
        Response   NVARCHAR(1000) NULL,
        CreatedAt  DATETIME       NOT NULL CONSTRAINT DF_ConciergeRequests_CreatedAt DEFAULT (GETDATE()),
        ResolvedAt DATETIME       NULL,
        ResolvedBy NVARCHAR(50)   NULL,
        CONSTRAINT PK_ConciergeRequests PRIMARY KEY CLUSTERED (RequestID)
    );
END
GO
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_ConciergeRequests_Status' AND parent_object_id = OBJECT_ID('dbo.ConciergeRequests'))
    ALTER TABLE dbo.ConciergeRequests
        ADD CONSTRAINT CK_ConciergeRequests_Status CHECK (Status IN ('Open', 'In Progress', 'Resolved'));
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ConciergeRequests_Status' AND object_id = OBJECT_ID('dbo.ConciergeRequests'))
    CREATE INDEX IX_ConciergeRequests_Status ON dbo.ConciergeRequests (Status, CreatedAt DESC);
GO

/* =========================== Notifications =========================== */
IF OBJECT_ID('dbo.Notifications', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Notifications (
        NotificationID INT IDENTITY(1,1) NOT NULL,
        RoomNumber     NVARCHAR(50)   NULL,
        Message        NVARCHAR(1000) NOT NULL,
        Channel        NVARCHAR(20)   NOT NULL CONSTRAINT DF_Notifications_Channel DEFAULT ('In-Room'),
        SentBy         NVARCHAR(50)   NULL,
        CreatedAt      DATETIME       NOT NULL CONSTRAINT DF_Notifications_CreatedAt DEFAULT (GETDATE()),
        ReadAt         DATETIME       NULL,
        CONSTRAINT PK_Notifications PRIMARY KEY CLUSTERED (NotificationID)
    );
END
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Notifications_RoomNumber' AND object_id = OBJECT_ID('dbo.Notifications'))
    CREATE INDEX IX_Notifications_RoomNumber ON dbo.Notifications (RoomNumber, CreatedAt DESC);
GO

/* ============================= StaffAlerts ============================= */
IF OBJECT_ID('dbo.StaffAlerts', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.StaffAlerts (
        AlertID       INT IDENTITY(1,1) NOT NULL,
        TargetRole    NVARCHAR(50)   NOT NULL,
        Message       NVARCHAR(1000) NOT NULL,
        Severity      NVARCHAR(20)   NOT NULL CONSTRAINT DF_StaffAlerts_Severity DEFAULT ('Info'),
        CreatedAt     DATETIME       NOT NULL CONSTRAINT DF_StaffAlerts_CreatedAt DEFAULT (GETDATE()),
        AcknowledgedAt DATETIME      NULL,
        AcknowledgedBy NVARCHAR(50)  NULL,
        CONSTRAINT PK_StaffAlerts PRIMARY KEY CLUSTERED (AlertID)
    );
END
GO
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_StaffAlerts_Severity' AND parent_object_id = OBJECT_ID('dbo.StaffAlerts'))
    ALTER TABLE dbo.StaffAlerts
        ADD CONSTRAINT CK_StaffAlerts_Severity CHECK (Severity IN ('Info', 'Warning', 'Critical'));
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_StaffAlerts_Ack' AND object_id = OBJECT_ID('dbo.StaffAlerts'))
    CREATE INDEX IX_StaffAlerts_Ack ON dbo.StaffAlerts (AcknowledgedAt, Severity);
GO

/* ============================== Orders ============================== */
IF OBJECT_ID('dbo.Orders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Orders (
        OrderID     INT IDENTITY(1,1) NOT NULL,
        RoomNumber  NVARCHAR(50)  NULL,
        Status      NVARCHAR(20)  NOT NULL CONSTRAINT DF_Orders_Status DEFAULT ('Placed'),
        PlacedAt    DATETIME      NOT NULL CONSTRAINT DF_Orders_PlacedAt DEFAULT (GETDATE()),
        UpdatedAt   DATETIME      NOT NULL CONSTRAINT DF_Orders_UpdatedAt DEFAULT (GETDATE()),
        CompletedAt DATETIME      NULL,
        Notes       NVARCHAR(500) NULL,
        CONSTRAINT PK_Orders PRIMARY KEY CLUSTERED (OrderID)
    );
END
GO
-- Constrains the lifecycle so a typo cannot invent a state the UI cannot render.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_Orders_Status' AND parent_object_id = OBJECT_ID('dbo.Orders'))
    ALTER TABLE dbo.Orders
        ADD CONSTRAINT CK_Orders_Status
        CHECK (Status IN ('Placed', 'Preparing', 'Ready', 'Delivered', 'Completed', 'Cancelled'));
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Orders_RoomStatus' AND object_id = OBJECT_ID('dbo.Orders'))
    CREATE INDEX IX_Orders_RoomStatus ON dbo.Orders (RoomNumber, Status, PlacedAt DESC);
GO

IF OBJECT_ID('dbo.OrderItems', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.OrderItems (
        OrderItemID INT IDENTITY(1,1) NOT NULL,
        OrderID     INT NOT NULL,
        ItemID      INT NULL,
        ItemName    NVARCHAR(200) NOT NULL,
        Quantity    INT          NOT NULL,
        UnitPrice   DECIMAL(10,2) NOT NULL,
        CONSTRAINT PK_OrderItems PRIMARY KEY CLUSTERED (OrderItemID)
    );
END
GO
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_OrderItems_Orders')
    ALTER TABLE dbo.OrderItems
        ADD CONSTRAINT FK_OrderItems_Orders FOREIGN KEY (OrderID)
        REFERENCES dbo.Orders (OrderID) ON DELETE CASCADE;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_OrderItems_OrderID' AND object_id = OBJECT_ID('dbo.OrderItems'))
    CREATE INDEX IX_OrderItems_OrderID ON dbo.OrderItems (OrderID);
GO

/* ============================= Amenities ============================= */
IF OBJECT_ID('dbo.Amenities', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Amenities (
        AmenityID   INT IDENTITY(1,1) NOT NULL,
        Name        NVARCHAR(200) NOT NULL,
        Description NVARCHAR(500) NULL,
        DisplayOrder INT          NOT NULL CONSTRAINT DF_Amenities_DisplayOrder DEFAULT (0),
        Active      BIT           NOT NULL CONSTRAINT DF_Amenities_Active DEFAULT (1),
        CONSTRAINT PK_Amenities PRIMARY KEY CLUSTERED (AmenityID)
    );
END
GO
-- Replaces the hardcoded amenity list that was printed by view_amenities().
IF NOT EXISTS (SELECT 1 FROM dbo.Amenities)
BEGIN
    INSERT INTO dbo.Amenities (Name, Description, DisplayOrder) VALUES
        (N'Complimentary high-speed Wi-Fi', N'Throughout the premises', 1),
        (N'Fitness center',                 N'Fully equipped, open 24 hours', 2),
        (N'Swimming pool',                  NULL, 3),
        (N'Room service',                   N'Available 24 hours', 4),
        (N'Gourmet restaurant',             NULL, 5),
        (N'Business center',                N'Includes meeting rooms', 6),
        (N'Front desk',                     N'Open 24 hours', 7),
        (N'Complimentary breakfast',       N'For premium tier guests', 8);
END
GO

/* ============================= Promotions ============================= */
IF OBJECT_ID('dbo.Promotions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Promotions (
        PromotionID INT IDENTITY(1,1) NOT NULL,
        Title       NVARCHAR(200)  NOT NULL,
        Details     NVARCHAR(1000) NULL,
        DiscountCode VARCHAR(50)   NULL,
        StartsOn    DATE           NULL,
        EndsOn      DATE           NULL,
        Active      BIT            NOT NULL CONSTRAINT DF_Promotions_Active DEFAULT (1),
        CONSTRAINT PK_Promotions PRIMARY KEY CLUSTERED (PromotionID)
    );
END
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Promotions_Active' AND object_id = OBJECT_ID('dbo.Promotions'))
    CREATE INDEX IX_Promotions_Active ON dbo.Promotions (Active, StartsOn, EndsOn);
GO
-- Replaces the hardcoded promotion list that was printed by view_promotions().
IF NOT EXISTS (SELECT 1 FROM dbo.Promotions)
BEGIN
    INSERT INTO dbo.Promotions (Title, Details) VALUES
        (N'15% off room service',        N'On all room service orders above $50.'),
        (N'Summer spa special',          N'Book a spa session and get a free fitness class.'),
        (N'Loyalty dining bonus',        N'Earn extra points for every dine-in meal.');
END
GO

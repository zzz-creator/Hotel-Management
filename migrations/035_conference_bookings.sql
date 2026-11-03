-- Migration 035: Conference room system -- Phase B (bookings, equipment, invoices).
-- Re-runnable.
USE hotelSystem
GO

IF OBJECT_ID('dbo.ConferenceBookings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConferenceBookings (
        BookingID      INT IDENTITY(1,1) NOT NULL,
        RoomID         INT               NOT NULL,
        StartAt        DATETIME          NOT NULL,
        EndAt          DATETIME          NOT NULL,
        CustomerID     INT               NULL,
        BookedBy       NVARCHAR(100)     NULL,
        AttendeeCount  INT               NOT NULL CONSTRAINT DF_ConfBook_Attendees DEFAULT (0),
        SetupType      NVARCHAR(50)      NULL,
        Status         NVARCHAR(20)      NOT NULL CONSTRAINT DF_ConfBook_Status DEFAULT ('Confirmed'),
        Notes          NVARCHAR(500)     NULL,
        CreatedAt      DATETIME          NOT NULL CONSTRAINT DF_ConfBook_CreatedAt DEFAULT (GETDATE()),
        CancelledAt    DATETIME          NULL,
        CancelledBy    NVARCHAR(100)     NULL,
        CONSTRAINT PK_ConferenceBookings PRIMARY KEY CLUSTERED (BookingID),
        CONSTRAINT FK_ConfBook_Room FOREIGN KEY (RoomID)
            REFERENCES dbo.ConferenceRooms (RoomID),
        CONSTRAINT FK_ConfBook_Customer FOREIGN KEY (CustomerID)
            REFERENCES dbo.CustomerProfiles (CustomerID),
        CONSTRAINT CK_ConfBook_Status CHECK (Status IN ('Confirmed','InProgress','Completed','Cancelled')),
        CONSTRAINT CK_ConfBook_Times CHECK (EndAt > StartAt)
    );
END
GO

IF OBJECT_ID('dbo.ConferenceBookingEquipment', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConferenceBookingEquipment (
        BookingID     INT            NOT NULL,
        EquipmentType NVARCHAR(50)   NOT NULL,
        Quantity      INT            NOT NULL CONSTRAINT DF_ConfEquip_Qty DEFAULT (1),
        CONSTRAINT PK_ConfEquip PRIMARY KEY CLUSTERED (BookingID, EquipmentType),
        CONSTRAINT FK_ConfEquip_Booking FOREIGN KEY (BookingID)
            REFERENCES dbo.ConferenceBookings (BookingID) ON DELETE CASCADE
    );
END
GO

IF OBJECT_ID('dbo.ConferenceInvoices', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConferenceInvoices (
        ConferenceInvoiceID INT IDENTITY(1,1) NOT NULL,
        BookingID           INT            NOT NULL,
        InvoiceDate         DATETIME       NOT NULL CONSTRAINT DF_ConfInv_Date DEFAULT (GETDATE()),
        Subtotal            DECIMAL(12,2)  NOT NULL CONSTRAINT DF_ConfInv_Sub DEFAULT (0),
        TaxAmount           DECIMAL(12,2)  NOT NULL CONSTRAINT DF_ConfInv_Tax DEFAULT (0),
        TotalAmount         DECIMAL(12,2)  NOT NULL CONSTRAINT DF_ConfInv_Total DEFAULT (0),
        AmountPaid          DECIMAL(12,2)  NOT NULL CONSTRAINT DF_ConfInv_Paid DEFAULT (0),
        VoidedAt            DATETIME       NULL,
        VoidedBy            NVARCHAR(100)  NULL,
        VoidReason          NVARCHAR(255)  NULL,
        CONSTRAINT PK_ConferenceInvoices PRIMARY KEY CLUSTERED (ConferenceInvoiceID),
        CONSTRAINT FK_ConfInv_Booking FOREIGN KEY (BookingID)
            REFERENCES dbo.ConferenceBookings (BookingID)
    );
END
GO

CREATE INDEX IX_ConfBook_Room_Times ON dbo.ConferenceBookings (RoomID, StartAt, EndAt);
GO
CREATE INDEX IX_ConfBook_Status ON dbo.ConferenceBookings (Status, StartAt);
GO
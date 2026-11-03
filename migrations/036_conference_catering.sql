-- Migration 036: Conference room system -- Phase D (catering).
-- Re-runnable.
USE hotelSystem
GO

IF OBJECT_ID('dbo.ConferenceCateringOrders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ConferenceCateringOrders (
        CateringOrderID INT IDENTITY(1,1) NOT NULL,
        BookingID       INT            NOT NULL,
        PackageType     NVARCHAR(50)   NOT NULL,
        Description     NVARCHAR(255)  NULL,
        HeadCount       INT            NOT NULL CONSTRAINT DF_ConfCat_Head DEFAULT (0),
        UnitPrice       DECIMAL(10,2)  NOT NULL CONSTRAINT DF_ConfCat_Unit DEFAULT (0),
        TotalAmount     DECIMAL(12,2)  NOT NULL CONSTRAINT DF_ConfCat_Total DEFAULT (0),
        Status          NVARCHAR(20)   NOT NULL CONSTRAINT DF_ConfCat_Status DEFAULT ('Requested'),
        PlacedAt        DATETIME       NOT NULL CONSTRAINT DF_ConfCat_Placed DEFAULT (GETDATE()),
        DeliveredAt     DATETIME       NULL,
        CONSTRAINT PK_ConfCatering PRIMARY KEY CLUSTERED (CateringOrderID),
        CONSTRAINT FK_ConfCat_Booking FOREIGN KEY (BookingID)
            REFERENCES dbo.ConferenceBookings (BookingID) ON DELETE CASCADE,
        CONSTRAINT CK_ConfCat_Status CHECK (Status IN ('Requested','Confirmed','Delivered','Cancelled'))
    );
END
GO
-- Migration: 008_rooms_seed.sql
-- Seeds the Rooms table so the app's room status board, check-in/check-out sync,
-- and reservation availability checks have real rooms to work with.
-- Pure seed data -- no schema changes.
--
-- Scale: a 150-floor tower, room numbers formatted as <floor><3-digit code>
-- (e.g. floor 9 code 012 = 9012, floor 150 code 956 = 150956).
--
-- Vertical distribution (larger footprints lower the room count up top):
--   * Floors 1-50    (Base tier)      : all codes 001-999 (999 rooms/floor)
--   * Floors 51-100  (Mid-tower tier) : all codes 001-999 (999 rooms/floor)
--   * Floors 101-145 (Executive tier) : codes 001-850 only (850 rooms/floor)
--   * Floors 146-150 (Pinnacle tier)  : codes 951-956 only (penthouse/presidential suites)
--
-- Per-floor code segments drive room type:
--   001-600 inner/standard layouts, 601-850 premium exposure,
--   851-950 corner units, 951-999 specialty structural spans.
--
-- Every room number already present in Reservations (data.json: 1111, 9012, 9015,
-- 9016, 9017, 9020, 9063, 9072, 9326, 9732, 9756) falls in the floors 1-100 range
-- and is included, so no reservation orphans a room.
--
-- Safe to re-run (idempotent). Apply from SSMS after migrations 001-007.
USE hotelSystem
GO

;WITH Floors AS (
    SELECT 1 AS Floor
    UNION ALL
    SELECT Floor + 1 FROM Floors WHERE Floor < 150
),
Codes AS (
    SELECT 1 AS Code
    UNION ALL
    SELECT Code + 1 FROM Codes WHERE Code < 999
),
Generated AS (
    SELECT
        CAST(f.Floor AS varchar(10)) + RIGHT('000' + CAST(c.Code AS varchar(3)), 3) AS RoomNumber,
        f.Floor AS Floor,
        c.Code AS Code
    FROM Floors f
    CROSS JOIN Codes c
    WHERE (f.Floor <= 100)
       OR (f.Floor BETWEEN 101 AND 145 AND c.Code <= 850)
       OR (f.Floor BETWEEN 146 AND 150 AND c.Code BETWEEN 951 AND 956)
),
Classified AS (
    SELECT
        RoomNumber,
        Floor,
        Code,
        CASE
            WHEN Floor BETWEEN 146 AND 150 THEN
                CASE WHEN Code <= 953 THEN 'Penthouse' ELSE 'Presidential Suite' END
            WHEN Floor BETWEEN 101 AND 145 THEN
                CASE WHEN Code <= 600 THEN 'Suite' ELSE 'Grand Suite' END
            WHEN Floor BETWEEN 51 AND 100 THEN
                CASE WHEN Code <= 850 THEN 'Deluxe'
                     WHEN Code <= 950 THEN 'Junior Suite'
                     ELSE 'Suite' END
            ELSE
                CASE WHEN Code <= 600 THEN 'Standard'
                     WHEN Code <= 850 THEN 'Deluxe'
                     WHEN Code <= 950 THEN 'Junior Suite'
                     ELSE 'Suite' END
        END AS RoomType
    FROM Generated
)
INSERT INTO dbo.Rooms (RoomNumber, RoomType, Description, Status)
SELECT
    g.RoomNumber,
    g.RoomType,
    g.RoomType + ' - floor ' + CAST(g.Floor AS varchar(3)) + ' - ' +
        CASE
            WHEN g.Code >= 951 THEN 'panoramic specialty span'
            WHEN g.Code >= 851 THEN 'corner unit with multi-directional views'
            WHEN g.Code >= 601 THEN 'floor-to-ceiling windows'
            ELSE 'central corridor view'
        END,
    'Available'
FROM Classified g
WHERE NOT EXISTS (SELECT 1 FROM dbo.Rooms r WHERE r.RoomNumber = g.RoomNumber)
OPTION (MAXRECURSION 0);
GO
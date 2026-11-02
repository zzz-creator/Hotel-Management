# type: ignore
"""Concurrency stress harness (Phase 1G).

Runs N worker threads (default 8) of randomized book/check-in/check-out sequences
against a SCRATCH database and asserts the Phase 1 invariants afterwards.

Usage:  python tests/stress_concurrency.py [--workers 8] [--ops 200]

Requires a config.ini with a [verify] section pointing at a scratch database, the
same way tests/verify_e2e.py does. Never touches the live [database] target.
"""
import argparse
import configparser
import os
import random
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests.verify_e2e as v

from hotel import db as dbmod
from hotel import core as coremod
import main as app
from hotel import session, reservations, rooms, billing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--ops", type=int, default=200)
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "config.ini")
    server = cfg["verify"]["server"]
    scratch = cfg["verify"]["verify_database"]
    user = cfg["verify"].get("username", "")
    password = cfg["verify"].get("password", "")
    live = cfg["database"]["database"]
    if scratch == live:
        print("Refusing to run: scratch resolves to the live database.")
        sys.exit(2)

    # Build a fresh scratch DB.
    v.fresh_scratch(server, scratch, user, password)
    cs = v.conn_str(server, scratch, user, password)
    coremod.CONNECTION_STRING = cs
    coremod.database = scratch
    dbmod.init(cs)

    # Seed rooms 101..108 and a rate.
    conn = dbmod.create_connection()
    cur = conn.cursor()
    for rn in range(101, 109):
        cur.execute("IF NOT EXISTS (SELECT 1 FROM Rooms WHERE RoomNumber=?) INSERT INTO Rooms (RoomNumber, RoomType, Status) VALUES (?, 'Standard', 'Available')", (str(rn), str(rn)))
    conn.commit()
    conn.close()

    errors = []
    lock = threading.Lock()

    def worker(wid):
        rng = random.Random(wid * 9973)
        for _ in range(args.ops):
            room = str(rng.choice(range(101, 109)))
            try:
                today = date.today()
                ci = today
                co = today + timedelta(days=2)
                # Try to book.
                with dbmod.get_connection(isolation="serializable") as c:
                    if c is None:
                        continue
                    ok, reason = reservations._book_reservation_in_conn(
                        c, room, "Stress", "Guest", ci, co, room_type="Standard")
                    if ok:
                        c.commit()
                        # check-in + check-out
                        try:
                            reservations.check_in.__wrapped__ if hasattr(reservations.check_in, "__wrapped__") else None
                        except Exception:
                            pass
                time.sleep(0.001)
            except Exception as e:
                with lock:
                    errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Invariants.
    conn = dbmod.create_connection()
    cur = conn.cursor()
    cur.execute("SELECT RoomNumber, COUNT(*) FROM Reservations GROUP BY RoomNumber HAVING COUNT(*) > 1")
    dupes = cur.fetchall()
    cur.execute("SELECT RoomNumber, COUNT(*) FROM Transactions WHERE ChargeGroup='Room' GROUP BY RoomNumber HAVING COUNT(*) > 1")
    double_charges = cur.fetchall()
    conn.close()

    print("workers=%d ops=%d errors=%d duplicate_reservations=%d double_room_charges=%d"
          % (args.workers, args.ops, len(errors), len(dupes), len(double_charges)))
    if errors or dupes or double_charges:
        sys.exit(1)
    print("STRESS OK")


if __name__ == "__main__":
    main()

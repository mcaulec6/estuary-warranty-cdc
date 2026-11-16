"""
Fake vehicle warranty data generator.

Seeds vehicles and an initial batch of claims (so Estuary has something to
backfill), then loops forever producing a realistic mix of change events:

  * INSERT  - new warranty claims arrive
  * UPDATE  - claims move through submitted -> under_review -> approved/rejected -> paid
  * DELETE  - occasionally a rejected claim is purged

All data is randomly generated. Stop with Ctrl+C.

Usage:
    python generate.py                 # one tick every 2 seconds
    python generate.py --interval 0.5  # faster
    python generate.py --burst 500     # insert 500 claims at once, then continue
"""

import argparse
import os
import random
import string
import sys
import time
from datetime import date, timedelta

import psycopg

MODELS = {
    # model: (price band multiplier, model years)
    "Aurora EV": (1.6, [2023, 2024, 2025, 2026]),
    "Summit SUV": (1.3, [2021, 2022, 2023, 2024, 2025]),
    "Harbor Sedan": (1.0, [2020, 2021, 2022, 2023, 2024]),
    "Ridge Pickup": (1.4, [2021, 2022, 2023, 2024, 2025, 2026]),
    "Metro Hatch": (0.8, [2022, 2023, 2024, 2025]),
}

# component: (typical repair cost in EUR, relative likelihood)
COMPONENTS = {
    "battery_pack": (6500, 2),
    "charging_port": (450, 5),
    "infotainment": (900, 12),
    "adas_camera": (1200, 6),
    "brake_system": (750, 8),
    "hvac": (1100, 7),
    "transmission": (3800, 3),
    "suspension": (950, 6),
    "door_module": (400, 9),
    "paint_defect": (600, 5),
}

MARKETS = ["IE", "UK", "DE", "FR", "SE", "NL", "ES"]
DEALERS = [f"{m}-{n:03d}" for m in MARKETS for n in range(1, 6)]

# Status transitions: current -> possible next statuses
NEXT_STATUS = {
    "submitted": ["under_review"],
    "under_review": ["approved", "approved", "approved", "rejected"],
    "approved": ["paid"],
}


def fake_vin() -> str:
    alphabet = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # VINs exclude I, O and Q
    return "".join(random.choices(alphabet, k=17))


def pick_component() -> tuple[str, int]:
    names = list(COMPONENTS)
    weights = [COMPONENTS[n][1] for n in names]
    name = random.choices(names, weights=weights, k=1)[0]
    return name, COMPONENTS[name][0]


def seed_vehicles(cur, count: int = 200) -> None:
    cur.execute("SELECT count(*) FROM vehicles")
    if cur.fetchone()[0] > 0:
        return
    rows = []
    for _ in range(count):
        model = random.choice(list(MODELS))
        year = random.choice(MODELS[model][1])
        sold_on = date(year, 1, 1) + timedelta(days=random.randint(0, 364))
        rows.append((fake_vin(), model, year, random.choice(MARKETS), sold_on))
    cur.executemany(
        "INSERT INTO vehicles (vin, model, model_year, market, sold_on) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        rows,
    )
    print(f"Seeded {count} vehicles")


def insert_claims(cur, n: int) -> None:
    cur.execute("SELECT vin, model FROM vehicles ORDER BY random() LIMIT %s", (n,))
    for vin, model in cur.fetchall():
        component, base_cost = pick_component()
        multiplier = MODELS[model][0]
        amount = int(base_cost * multiplier * random.uniform(0.6, 1.5))
        cur.execute(
            "INSERT INTO warranty_claims "
            "(vin, model, component, dealer_code, mileage_km, claim_amount) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (vin, model, component, random.choice(DEALERS),
             random.randint(500, 120_000), amount),
        )


def advance_claims(cur, n: int) -> None:
    cur.execute(
        "SELECT claim_id, status FROM warranty_claims "
        "WHERE status IN ('submitted', 'under_review', 'approved') "
        "ORDER BY random() LIMIT %s",
        (n,),
    )
    for claim_id, status in cur.fetchall():
        new_status = random.choice(NEXT_STATUS[status])
        cur.execute(
            "UPDATE warranty_claims SET status = %s, updated_at = now() "
            "WHERE claim_id = %s",
            (new_status, claim_id),
        )


def purge_rejected(cur) -> None:
    cur.execute(
        "DELETE FROM warranty_claims WHERE claim_id = ("
        "  SELECT claim_id FROM warranty_claims WHERE status = 'rejected' "
        "  ORDER BY random() LIMIT 1)"
    )


def tick(cur) -> str:
    inserts = random.randint(1, 3)
    updates = random.randint(1, 4)
    insert_claims(cur, inserts)
    advance_claims(cur, updates)
    deleted = 0
    if random.random() < 0.05:
        purge_rejected(cur)
        deleted = cur.rowcount
    return f"+{inserts} claims, ~{updates} status updates, -{deleted} deleted"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float,
                        default=float(os.environ.get("INTERVAL_SECONDS", 2)),
                        help="seconds between ticks (default 2)")
    parser.add_argument("--burst", type=int, default=0,
                        help="insert this many claims immediately before looping")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("Set DATABASE_URL (see .env.example)")

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            seed_vehicles(cur)
            cur.execute("SELECT count(*) FROM warranty_claims")
            if cur.fetchone()[0] == 0:
                insert_claims(cur, 50)
                print("Seeded 50 initial claims (these arrive in Estuary via backfill)")
            if args.burst:
                for done in range(0, args.burst, 100):
                    insert_claims(cur, min(100, args.burst - done))
                print(f"Burst-inserted ~{args.burst} claims")
        conn.commit()

        print(f"Generating changes every {args.interval}s. Ctrl+C to stop.")
        try:
            while True:
                with conn.cursor() as cur:
                    summary = tick(cur)
                conn.commit()
                print(time.strftime("%H:%M:%S"), summary, flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()

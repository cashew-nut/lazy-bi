"""Generate demo datasets and upload them to the S3 bucket.

Four datasets, one per supported source format:
  sales/<year>.parquet        - order lines, ~60k rows over 30 months (parquet glob)
  marketing/spend.parquet     - monthly ad spend by channel/region
  ref/products.csv            - product lookup joined into the sales model (csv)
  logistics/shipments         - courier shipments (Delta Lake table)
"""
from __future__ import annotations

import io
import random
from datetime import date, timedelta

import polars as pl
from deltalake import write_deltalake

from . import config, s3

REGIONS = ["Neo-Tokyo", "Night City", "Euro-Zone", "Pacifica", "Badlands"]
CATEGORIES = {
    "Cyberware": ["Optic Implant", "Neural Link", "Subdermal Armor", "Reflex Booster"],
    "Netrunning": ["ICE Breaker", "Deck MK-II", "RAM Upgrade", "Daemon Suite"],
    "Streetwear": ["Armored Jacket", "LED Visor", "Smart Boots", "Nano Weave Tee"],
    "Vehicles": ["Hover Bike", "Turbo Coupe", "Cargo Drone"],
}
CHANNELS = ["web", "street vendor", "fixer", "corp direct"]
SEGMENTS = ["solo", "corpo", "nomad", "netrunner"]

PRICE = {
    "Optic Implant": 1200, "Neural Link": 3500, "Subdermal Armor": 900,
    "Reflex Booster": 2100, "ICE Breaker": 640, "Deck MK-II": 1800,
    "RAM Upgrade": 260, "Daemon Suite": 480, "Armored Jacket": 320,
    "LED Visor": 95, "Smart Boots": 140, "Nano Weave Tee": 45,
    "Hover Bike": 8200, "Turbo Coupe": 21000, "Cargo Drone": 5400,
}


def _sales_frame(rng: random.Random) -> pl.DataFrame:
    start = date(2024, 1, 1)
    days = (date(2026, 6, 30) - start).days
    rows = []
    order_id = 100000
    for _ in range(60_000):
        d = start + timedelta(days=int(rng.triangular(0, days, days * 0.7)))
        category = rng.choices(list(CATEGORIES), weights=[4, 3, 5, 1])[0]
        product = rng.choice(CATEGORIES[category])
        base = PRICE[product]
        unit_price = round(base * rng.uniform(0.85, 1.25), 2)
        qty = 1 if base > 4000 else rng.randint(1, 5)
        # margin varies by category; vehicles are thin, netrunning gear is fat
        margin = {"Cyberware": 0.45, "Netrunning": 0.6, "Streetwear": 0.5, "Vehicles": 0.18}[category]
        unit_cost = round(unit_price * (1 - margin) * rng.uniform(0.9, 1.1), 2)
        order_id += rng.randint(1, 3)
        rows.append({
            "order_id": order_id,
            "order_date": d,
            "region": rng.choices(REGIONS, weights=[5, 6, 4, 3, 2])[0],
            "channel": rng.choices(CHANNELS, weights=[6, 2, 3, 2])[0],
            "segment": rng.choice(SEGMENTS),
            "category": category,
            "product": product,
            "quantity": qty,
            "unit_price": unit_price,
            "unit_cost": unit_cost,
        })
    return pl.DataFrame(rows)


# real-world anchor coordinates so the regions can sit on a map
REGION_COORDS = {
    "Neo-Tokyo": (35.68, 139.69), "Night City": (34.05, -118.24),
    "Euro-Zone": (52.52, 13.40), "Pacifica": (-33.87, 151.21),
    "Badlands": (33.45, -112.07),
}


def _marketing_frame(rng: random.Random) -> pl.DataFrame:
    rows = []
    month = date(2024, 1, 1)
    while month <= date(2026, 6, 1):
        for region in REGIONS:
            lat, lon = REGION_COORDS[region]
            for channel in ["holo-board", "net ads", "fixer referral"]:
                rows.append({
                    "month": month,
                    "region": region,
                    "region_lat": lat,
                    "region_lon": lon,
                    "channel": channel,
                    "spend": round(rng.uniform(2000, 30000), 2),
                    "impressions": rng.randint(50_000, 900_000),
                })
        month = (month.replace(day=28) + timedelta(days=5)).replace(day=1)
    return pl.DataFrame(rows)


SUPPLIERS = {
    "Cyberware": "Arasaka Biotech", "Netrunning": "NetWatch Surplus",
    "Streetwear": "Jinguji Collective", "Vehicles": "Militech Motors",
}


def _products_frame() -> pl.DataFrame:
    rows = []
    for category, products in CATEGORIES.items():
        for product in products:
            base = PRICE[product]
            rows.append({
                "product": product,
                "supplier": SUPPLIERS[category],
                "tier": "military-grade" if base >= 3000 else "corpo-grade" if base >= 500 else "street-grade",
            })
    return pl.DataFrame(rows)


PLANS = {"street": 20.0, "corpo": 95.0, "netrunner": 240.0}


def _subscriptions_frame(rng: random.Random) -> pl.DataFrame:
    """Subscription intervals for the spine demo: start/end dates, null end =
    still active. Growth over time with plan-dependent churn."""
    start_lo = date(2024, 1, 1)
    horizon = date(2026, 6, 30)
    days = (horizon - start_lo).days
    rows = []
    for cust in range(1, 9001):
        # sign-ups skew later (growing business)
        started = start_lo + timedelta(days=int(days * (rng.random() ** 0.6)))
        plan = rng.choices(list(PLANS), weights=[5, 3, 1])[0]
        churn_days = {"street": 210, "corpo": 420, "netrunner": 700}[plan]
        lifetime = int(rng.expovariate(1 / churn_days))
        ended = started + timedelta(days=max(14, lifetime))
        rows.append({
            "customer_id": cust,
            "plan": plan,
            "region": rng.choices(REGIONS, weights=[5, 6, 4, 3, 2])[0],
            "monthly_fee": round(PLANS[plan] * rng.uniform(0.9, 1.15), 2),
            "start_date": started,
            "end_date": ended if ended <= horizon else None,
        })
    return pl.DataFrame(rows)


COURIERS = ["Trauma Freight", "Arasaka Logistics", "Militech Express", "Night Couriers"]


def _shipments_frame(rng: random.Random) -> pl.DataFrame:
    start = date(2024, 1, 1)
    days = (date(2026, 6, 30) - start).days
    rows = []
    for _ in range(20_000):
        courier = rng.choices(COURIERS, weights=[4, 3, 2, 3])[0]
        # couriers have distinct speed/cost profiles so the demo charts separate
        speed = {"Trauma Freight": 30, "Arasaka Logistics": 18, "Militech Express": 10, "Night Couriers": 44}[courier]
        packages = rng.randint(1, 12)
        rows.append({
            "ship_date": start + timedelta(days=rng.randint(0, days)),
            "courier": courier,
            "region": rng.choices(REGIONS, weights=[5, 6, 4, 3, 2])[0],
            "packages": packages,
            "delivery_hours": round(rng.gauss(speed, speed * 0.25) + 2, 1),
            "cost": round(packages * rng.uniform(8, 30) + speed * 1.5, 2),
        })
    return pl.DataFrame(rows)


def _upload(client, key: str, df: pl.DataFrame) -> None:
    buf = io.BytesIO()
    df.write_parquet(buf)
    client.put_object(Bucket=config.BUCKET, Key=key, Body=buf.getvalue())


def seed_bucket() -> bool:
    """Create the bucket and upload demo parquet files. Returns True if seeded,
    False if the bucket already had data."""
    client = s3.client()
    try:
        client.create_bucket(Bucket=config.BUCKET)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    existing = client.list_objects_v2(Bucket=config.BUCKET, MaxKeys=1)
    if existing.get("KeyCount", 0) > 0:
        return False

    rng = random.Random(2077)
    sales = _sales_frame(rng)
    # split by year so the semantic model reads a multi-file glob, like real life
    for (year,), part in sales.group_by(pl.col("order_date").dt.year(), maintain_order=True):
        _upload(client, f"sales/{year}.parquet", part)
    _upload(client, "marketing/spend.parquet", _marketing_frame(rng))
    client.put_object(Bucket=config.BUCKET, Key="ref/products.csv",
                      Body=_products_frame().write_csv().encode())
    write_deltalake(f"s3://{config.BUCKET}/logistics/shipments", _shipments_frame(rng),
                    storage_options=config.delta_write_options())
    _upload(client, "subscriptions/subs.parquet", _subscriptions_frame(rng))
    _upload_local_cache(client)
    return True


def _upload_local_cache(client) -> None:
    """Big optional datasets (e.g. NYC taxi, fetched by app/load_taxi.py) are
    cached on disk and re-uploaded to the fresh emulator on every start."""
    cache = config.PROJECT_ROOT / "data_cache"
    if not cache.is_dir():
        return
    for path in sorted(cache.rglob("*.parquet")):
        key = str(path.relative_to(cache))
        client.upload_file(str(path), config.BUCKET, key)

"""Generate demo datasets and upload them to the S3 bucket.

Five datasets, one per supported source format:
  sales/<year>.parquet        - order lines, ~60k rows over 30 months (parquet glob)
  marketing/spend.parquet     - monthly ad spend by channel/region
  ref/products.csv            - product lookup joined into the sales model (csv)
  logistics/shipments         - courier shipments (Delta Lake table)
  support/tickets              - support tickets across the fixer network (Iceberg table)
"""
from __future__ import annotations

import calendar as calendar_mod
import io
import random
import secrets
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from deltalake import write_deltalake

from . import auth, config, s3
from .registry import registry

# shared demo time window: ~2.5 years of history ending mid-2026, reused
# across every fact table so the frames stay time-aligned with each other.
DEMO_START = date(2024, 1, 1)
DEMO_END = date(2026, 6, 30)

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


def _sales_frame(rng: random.Random) -> pa.Table:
    start = DEMO_START
    days = (DEMO_END - start).days
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
    return pa.Table.from_pylist(rows)


# real-world anchor coordinates so the regions can sit on a map
REGION_COORDS = {
    "Neo-Tokyo": (35.68, 139.69), "Night City": (34.05, -118.24),
    "Euro-Zone": (52.52, 13.40), "Pacifica": (-33.87, 151.21),
    "Badlands": (33.45, -112.07),
}


def _marketing_frame(rng: random.Random) -> pa.Table:
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
    return pa.Table.from_pylist(rows)


SUPPLIERS = {
    "Cyberware": "Arasaka Biotech", "Netrunning": "NetWatch Surplus",
    "Streetwear": "Jinguji Collective", "Vehicles": "Militech Motors",
}


# territory rollup for the `geography` dimension bundle — proves common
# dimensions can span multiple joined tables (regions -> territories), not
# just flat single-table lookups
TERRITORIES = {
    "Neo-Tokyo": "pacific-rim", "Pacifica": "pacific-rim",
    "Night City": "north-america", "Badlands": "north-america",
    "Euro-Zone": "emea",
}
TERRITORY_NAMES = {"pacific-rim": "Pacific Rim", "north-america": "North America", "emea": "EMEA"}


def _regions_frame() -> pa.Table:
    rows = []
    for region in REGIONS:
        lat, lon = REGION_COORDS[region]
        rows.append({"region": region, "region_lat": lat, "region_lon": lon, "territory": TERRITORIES[region]})
    return pa.Table.from_pylist(rows)


def _territories_frame() -> pa.Table:
    return pa.Table.from_pylist(
        [{"territory": code, "name": name} for code, name in TERRITORY_NAMES.items()])


def _products_frame() -> pa.Table:
    rows = []
    for category, products in CATEGORIES.items():
        for product in products:
            base = PRICE[product]
            rows.append({
                "product": product,
                "supplier": SUPPLIERS[category],
                "tier": "military-grade" if base >= 3000 else "corpo-grade" if base >= 500 else "street-grade",
            })
    return pa.Table.from_pylist(rows)


PLANS = {"street": 20.0, "corpo": 95.0, "netrunner": 240.0}


def _subscriptions_frame(rng: random.Random) -> pa.Table:
    """Subscription intervals for the spine demo: start/end dates, null end =
    still active. Growth over time with plan-dependent churn."""
    start_lo = DEMO_START
    horizon = DEMO_END
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
    return pa.Table.from_pylist(rows)


def _calendar_frame() -> pa.Table:
    """A standalone date table — one row per day across the demo window, with
    the usual calendar attributes hung off it. Nothing relates it to any fact
    table: models reach it with a `how: between` dimension import, which is
    what turns "rows with a start and an end" into point-in-time reporting.
    """
    rows = []
    day = DEMO_START
    while day <= DEMO_END:
        last_of_month = day.replace(day=calendar_mod.monthrange(day.year, day.month)[1])
        rows.append({
            "date": day,
            "year": day.year,
            "quarter": f"{day.year}-Q{(day.month - 1) // 3 + 1}",
            "month_start": day.replace(day=1),
            "month": day.strftime("%Y-%m %b"),
            "week_start": day - timedelta(days=day.weekday()),
            "day_of_week": day.strftime("%A"),
            "is_month_start": day.day == 1,
            "is_month_end": day == last_of_month,
            "is_weekend": day.weekday() >= 5,
        })
        day += timedelta(days=1)
    return pa.Table.from_pylist(rows)


COURIERS = ["Trauma Freight", "Arasaka Logistics", "Militech Express", "Night Couriers"]


def _shipments_frame(rng: random.Random) -> pa.Table:
    start = DEMO_START
    days = (DEMO_END - start).days
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
    return pa.Table.from_pylist(rows)


TICKET_CATEGORIES = [
    "Cyberware Malfunction", "Netrunning Breach", "Billing Dispute",
    "Delivery Issue", "Account Access",
]
TICKET_PRIORITIES = ["low", "medium", "high", "critical"]
TICKET_CHANNELS = ["call", "chat", "holo-call", "in-person"]
TICKET_SLA_HOURS = {"low": 72, "medium": 48, "high": 24, "critical": 8}


def _support_frame(rng: random.Random) -> pa.Table:
    start = DEMO_START
    days = (DEMO_END - start).days
    rows = []
    for _ in range(15_000):
        priority = rng.choices(TICKET_PRIORITIES, weights=[4, 5, 3, 1])[0]
        sla = TICKET_SLA_HOURS[priority]
        resolution = max(0.5, round(rng.gauss(sla * 0.6, sla * 0.35), 1))
        rows.append({
            "ticket_date": start + timedelta(days=rng.randint(0, days)),
            "region": rng.choices(REGIONS, weights=[5, 6, 4, 3, 2])[0],
            "category": rng.choice(TICKET_CATEGORIES),
            "priority": priority,
            "channel": rng.choices(TICKET_CHANNELS, weights=[3, 5, 2, 1])[0],
            "resolution_hours": resolution,
            "sla_breached": resolution > sla,
        })
    return pa.Table.from_pylist(rows)


def _upload(client, key: str, table: pa.Table) -> None:
    buf = io.BytesIO()
    pq.write_table(table, buf)
    client.put_object(Bucket=config.BUCKET, Key=key, Body=buf.getvalue())


def _upload_csv(client, key: str, table: pa.Table) -> None:
    buf = io.BytesIO()
    pa_csv.write_csv(table, buf)
    client.put_object(Bucket=config.BUCKET, Key=key, Body=buf.getvalue())


def _write_iceberg(table_root: str, table: pa.Table) -> None:
    """Create a fresh Iceberg table at s3://<bucket>/<table_root> and write
    `df` as its initial snapshot. Iceberg needs a catalog to allocate a
    location/schema/snapshot atomically — an in-memory SqlCatalog does that
    once, here, at seed time only; nothing at query time depends on it
    afterwards (app/iceberg_util.py reads the table back by listing its
    self-describing metadata/ directory directly, the same catalog-free
    convention already used for Delta's _delta_log)."""
    from pyiceberg.catalog.sql import SqlCatalog

    catalog = SqlCatalog(
        "seed", uri="sqlite:///:memory:", warehouse=f"s3://{config.BUCKET}",
        **config.iceberg_storage_options(),
    )
    catalog.create_namespace("seed")
    iceberg_table = catalog.create_table(
        "seed.table", schema=table.schema,
        location=f"s3://{config.BUCKET}/{table_root}",
    )
    iceberg_table.append(table)


def _create_bucket(client) -> None:
    """us-east-1 is the one region S3's CreateBucket API treats as the
    implicit default: passing a LocationConstraint for it is *also*
    rejected, so it's the one region this must be left out for. Every other
    region needs it, or CreateBucket fails outright with
    IllegalLocationConstraintException instead of creating anything — moto
    enforces this exactly like real S3 does."""
    kwargs = {"Bucket": config.BUCKET}
    if config.AWS_REGION != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": config.AWS_REGION}
    try:
        client.create_bucket(**kwargs)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except ClientError as e:
        if e.response["Error"]["Code"] != "AccessDenied":
            raise
        # A read-only identity — the common case for a real, pre-existing
        # bucket someone else already manages — has no s3:CreateBucket
        # permission at all, by design: that's not this app's problem to
        # work around, only to not treat as fatal. Assume the bucket
        # already exists (true by construction of that use case) and move
        # on; the list/read calls right after this fail loudly and
        # specifically if that assumption turns out to be wrong.
        print(f"[cash-intel] no s3:CreateBucket permission for {config.BUCKET!r} "
              f"— assuming it already exists and continuing read-only")


def seed_bucket() -> bool:
    """Create the bucket and upload demo parquet files. Returns True if seeded,
    False if the bucket already had data."""
    client = s3.client()
    _create_bucket(client)
    existing = client.list_objects_v2(Bucket=config.BUCKET, MaxKeys=1)
    if existing.get("KeyCount", 0) > 0:
        return False

    rng = random.Random(2077)
    sales = _sales_frame(rng)
    # split by year so the semantic model reads a multi-file glob, like real life
    years = sales.column("order_date").to_pylist()
    for year in sorted({d.year for d in years}):
        mask = pa.array([d.year == year for d in years])
        _upload(client, f"sales/{year}.parquet", sales.filter(mask))
    _upload(client, "marketing/spend.parquet", _marketing_frame(rng))
    _upload_csv(client, "ref/products.csv", _products_frame())
    _upload_csv(client, "ref/regions.csv", _regions_frame())
    _upload_csv(client, "ref/territories.csv", _territories_frame())
    write_deltalake(f"s3://{config.BUCKET}/logistics/shipments", _shipments_frame(rng),
                    storage_options=config.delta_write_options())
    _write_iceberg("support/tickets", _support_frame(rng))
    _upload(client, "subscriptions/subs.parquet", _subscriptions_frame(rng))
    _upload(client, "ref/calendar.parquet", _calendar_frame())

    _upload_local_cache(client)
    _upload_raw_data(client)
    _upload_local_data(client)
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


def _upload_raw_data(client) -> None:
    """raw_data/<dataset>/ holds user-supplied source files, committed as-is
    (unlike the gitignored data_cache/ above) — small enough to check into
    the repo. Each dataset directory is uploaded flat to <dataset>/<filename>
    in the same bucket as the generated demo data, unmodeled, ready to build
    a model on top of from the Modelling workspace's source picker."""
    root = config.PROJECT_ROOT / "raw_data"
    if not root.is_dir():
        return
    for dataset_dir in sorted(root.iterdir()):
        if not dataset_dir.is_dir():
            continue
        for path in sorted(dataset_dir.iterdir()):
            if path.suffix not in (".csv", ".parquet"):
                continue
            client.upload_file(str(path), config.BUCKET, f"{dataset_dir.name}/{path.name}")


def _upload_local_data(client) -> None:
    """config.LOCAL_DATA_DIR (gitignored, outside the repo's tracked
    directories) caches every file a user has uploaded through the app
    (POST /api/datasets/local, app/api/datasets.py) — the durable copy,
    since the embedded emulator's bucket is in-memory and this function only
    ever runs against a freshly-created (empty) one. Uploaded exactly like
    _upload_raw_data above, just under local/<name>/ instead of <name>/, to
    land on the same key each upload already used — recursively, since a
    folder upload preserves its own subdirectory structure under <name>/."""
    root = config.LOCAL_DATA_DIR
    if not root.is_dir():
        return
    for dataset_dir in sorted(root.iterdir()):
        if not dataset_dir.is_dir():
            continue
        for path in sorted(dataset_dir.rglob("*")):
            if not path.is_file() or path.suffix not in (".csv", ".parquet"):
                continue
            rel = path.relative_to(dataset_dir).as_posix()
            client.upload_file(str(path), config.BUCKET, f"local/{dataset_dir.name}/{rel}")


def seed_bootstrap_admin() -> bool:
    """First-run only: when zero accounts exist, create the bootstrap admin
    with a random password and announce it loudly — the demo stays
    zero-config without ever shipping a well-known credential. Never runs
    again once any account exists (so a production DB can't regress to a
    printed password). Returns True if seeded."""
    store = registry.auth_store
    if store.count_users() > 0:
        return False
    password = secrets.token_urlsafe(12)
    user = store.create_user("admin", "Bootstrap Admin", "admin",
                             auth.hash_password(password))
    store.record_audit("bootstrap_admin_created", "system", target="admin")
    banner = "═" * 62
    print(f"""
{banner}
  BOOTSTRAP ADMIN CREATED (no accounts existed)

      username: admin
      password: {password}

  This password is shown ONCE and stored only as a hash.
  Sign in and change it (or create your own admin) immediately.
{banner}
""")
    return user is not None


# ---------------------------------------------------------------------------
# Notebook module: a sample "Recruitment Overview" notebook demonstrating
# tabs + a collapsible + embedded live visuals + an embedded dashboard (with
# its own saved view) — first-run only, mirrors seed_bootstrap_admin's
# "only if nothing exists yet" gate so a production DB never regresses.
# ---------------------------------------------------------------------------

_SALES_MODEL = "sales"


def _dq(model: str, dimensions: list, measures: list, chartType: str = "auto",
        sort: dict | None = None, limit: int = 1000) -> dict:
    """A visual's saved spec, in the same shape the studio builder writes."""
    return {
        "query": {
            "model": model, "dimensions": dimensions, "measures": measures,
            "inline_measures": [], "filters": [], "sort": sort, "limit": limit,
            "parameters": [], "parameter_values": {},
        },
        "chartType": chartType, "xAxisTitle": "", "yAxisTitle": "", "yScale": "linear",
    }


def seed_notebook_demo() -> bool:
    """First-run only: when no notebooks exist yet and the sales demo model
    is loaded, build a handful of saved visuals plus a dashboard (with a
    named view) around sales, then compose them into one sample notebook.
    Returns True if seeded."""
    store = registry.store
    if store.list_notebooks():
        return False
    if _SALES_MODEL not in registry.models:
        return False

    v_trend = store.create(
        "Revenue vs Profit Over Time", _SALES_MODEL,
        _dq(_SALES_MODEL, ["order_date"], ["revenue", "profit"], chartType="line"),
    )
    v_total = store.create(
        "Total Revenue", _SALES_MODEL,
        _dq(_SALES_MODEL, [], ["revenue"], chartType="stat"),
    )
    v_by_category = store.create(
        "Revenue by Category", _SALES_MODEL,
        _dq(_SALES_MODEL, ["category"], ["revenue"], chartType="bar"),
    )
    v_margin = store.create(
        "Margin %", _SALES_MODEL,
        _dq(_SALES_MODEL, [], ["margin_pct"], chartType="stat"),
    )
    rank_by_revenue = {"by": "revenue", "desc": True}
    v_by_region = store.create(
        "Revenue by Region", _SALES_MODEL,
        _dq(_SALES_MODEL, ["region"], ["revenue"], chartType="bar", sort=rank_by_revenue, limit=12),
    )
    v_by_channel = store.create(
        "Revenue by Channel", _SALES_MODEL,
        _dq(_SALES_MODEL, ["channel"], ["revenue"], chartType="bar", sort=rank_by_revenue, limit=10),
    )

    dash = store.create_dashboard(
        "Revenue by Region",
        items=[{"visual_id": v_by_channel["id"], "w": 1}, {"visual_id": v_by_region["id"], "w": 1}],
        views=[
            {"name": "All Categories", "filters": []},
            {"name": "Cyberware only", "filters": [{"field": "category", "op": "in", "value": "", "values": ["Cyberware"]}]},
        ],
        active_view=0,
    )

    html = f"""
<p>A first look at how <b>sales</b> composes into a notebook: not a fixed grid — the sections below are tabs and a collapsible, each holding whatever mix of visuals (and a whole embedded dashboard, with its own saved view) the story needs.</p>

<div class="nb-tabs">
  <div class="nb-tab-list">
    <button class="nb-tab-btn on" data-tab="overview">Overview</button>
    <button class="nb-tab-btn" data-tab="category">By Category</button>
    <button class="nb-tab-btn" data-tab="region">By Region</button>
  </div>

  <div class="nb-tab-panel" data-tab="overview">
    <div class="nb-split">
      <div class="nb-side">
        <p>Revenue is the headline number for the whole street-market network. The total on the right is live — it moves as new order lines land.</p>
        <div class="nb-visual compact" data-visual-id="{v_total["id"]}"></div>
      </div>
      <div class="nb-side">
        <div class="nb-visual" data-visual-id="{v_trend["id"]}"></div>
      </div>
    </div>
    <details class="nb-collapsible">
      <summary><span class="tree-caret">▸</span>Methodology</summary>
      <div class="nb-collapsible-body">
        <p>Revenue is sum(unit_price * quantity); profit nets out unit_cost the same way. Both are plain measures over the same order lines, so "revenue vs profit" is two sums, not two different tables.</p>
      </div>
    </details>
  </div>

  <div class="nb-tab-panel" data-tab="category" hidden>
    <aside class="nb-explainer" data-tone="method" data-title="Reading margin">
      <p>Margin % is profit as a share of revenue. A category with strong revenue but thin margin (Vehicles) reads very differently from one with less revenue but a fatter margin (Netrunning).</p>
    </aside>
    <div class="nb-visual compact" data-visual-id="{v_margin["id"]}"></div>
    <div class="nb-visual" data-visual-id="{v_by_category["id"]}"></div>
  </div>

  <div class="nb-tab-panel" data-tab="region" hidden>
    <p>The same dashboard used in the studio, embedded live at its "Cyberware only" saved view.</p>
    <div class="nb-dashboard" data-dashboard-id="{dash["id"]}" data-view="1"></div>
  </div>
</div>
""".strip()

    store.create_notebook("Sales Overview", html)
    return True

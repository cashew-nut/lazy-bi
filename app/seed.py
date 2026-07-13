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


# territory rollup for the `geography` dimension bundle — proves common
# dimensions can span multiple joined tables (regions -> territories), not
# just flat single-table lookups
TERRITORIES = {
    "Neo-Tokyo": "pacific-rim", "Pacifica": "pacific-rim",
    "Night City": "north-america", "Badlands": "north-america",
    "Euro-Zone": "emea",
}
TERRITORY_NAMES = {"pacific-rim": "Pacific Rim", "north-america": "North America", "emea": "EMEA"}


def _regions_frame() -> pl.DataFrame:
    rows = []
    for region in REGIONS:
        lat, lon = REGION_COORDS[region]
        rows.append({"region": region, "region_lat": lat, "region_lon": lon, "territory": TERRITORIES[region]})
    return pl.DataFrame(rows)


def _territories_frame() -> pl.DataFrame:
    return pl.DataFrame([{"territory": code, "name": name} for code, name in TERRITORY_NAMES.items()])


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


# ---------------------------------------------------------------------------
# Clinical operations: studies -> study countries -> study sites (a common
# dimensional model, dimensions/clinical_ops.yaml) plus a recruitment_events
# fact table (models/clinical_ops_recruitment.yaml) with baseline (planned)
# vs actual scenarios across the screen / randomised / screen_fail funnel.
# ---------------------------------------------------------------------------

STUDY_PHASES = {"Phase I": 10, "Phase II": 16, "Phase III": 24, "Phase IV": 30}  # -> planned duration, months

# (study_id, name, phase, therapeutic area, sponsor, target enrollment, planned start, status)
STUDIES = [
    ("ST-001", "ORION-3 (Advanced NSCLC)", "Phase III", "Oncology", "Helios Pharma", 420, date(2024, 1, 15), "Active"),
    ("ST-002", "CARDIA-EV", "Phase III", "Cardiology", "Meridian Biosciences", 650, date(2024, 3, 1), "Active"),
    ("ST-003", "NEURO-LIGHT", "Phase II", "Neurology", "Novastra Therapeutics", 180, date(2024, 6, 1), "Recruiting"),
    ("ST-004", "IMMUNO-PATH", "Phase II", "Immunology", "Cobalt Health", 240, date(2024, 2, 15), "Active"),
    ("ST-005", "RESP-CLEAR", "Phase III", "Respiratory", "Helios Pharma", 500, date(2023, 9, 1), "Closed to Recruitment"),
    ("ST-006", "ONCO-HORIZON", "Phase I", "Oncology", "Novastra Therapeutics", 60, date(2025, 1, 10), "Recruiting"),
    ("ST-007", "CARDIA-PREVENT", "Phase IV", "Cardiology", "Meridian Biosciences", 900, date(2023, 9, 1), "Active"),
    ("ST-008", "INFECT-SHIELD", "Phase III", "Infectious Disease", "Cobalt Health", 700, date(2024, 5, 1), "Active"),
    ("ST-009", "NEURO-RESTORE", "Phase III", "Neurology", "Helios Pharma", 380, date(2024, 9, 1), "Recruiting"),
    ("ST-010", "IMMUNO-BALANCE", "Phase III", "Immunology", "Novastra Therapeutics", 300, date(2025, 3, 1), "Planning"),
]

COUNTRIES_PER_PHASE = {"Phase I": (1, 2), "Phase II": (2, 4), "Phase III": (4, 7), "Phase IV": (3, 5)}
SITES_PER_COUNTRY = (2, 6)

COUNTRY_REGION = {
    "United States": "North America", "Canada": "North America",
    "United Kingdom": "Europe", "Germany": "Europe", "France": "Europe",
    "Spain": "Europe", "Italy": "Europe", "Poland": "Europe",
    "Japan": "Asia-Pacific", "South Korea": "Asia-Pacific", "Australia": "Asia-Pacific",
    "Brazil": "Latin America", "Mexico": "Latin America",
}
COUNTRY_WEIGHTS = {  # bias toward the US/EU, like a real recruitment footprint
    "United States": 8, "Canada": 3, "United Kingdom": 5, "Germany": 5, "France": 4,
    "Spain": 4, "Italy": 3, "Poland": 3, "Japan": 3, "South Korea": 2,
    "Australia": 2, "Brazil": 3, "Mexico": 2,
}
COUNTRY_CODE = {  # explicit (not sliced) so "United States"/"United Kingdom" don't collide
    "United States": "USA", "Canada": "CAN", "United Kingdom": "GBR", "Germany": "DEU",
    "France": "FRA", "Spain": "ESP", "Italy": "ITA", "Poland": "POL", "Japan": "JPN",
    "South Korea": "KOR", "Australia": "AUS", "Brazil": "BRA", "Mexico": "MEX",
}
CITIES = {
    "United States": ["Boston", "Houston", "Chicago", "Seattle", "Atlanta", "Denver"],
    "Canada": ["Toronto", "Montreal", "Vancouver"],
    "United Kingdom": ["London", "Manchester", "Birmingham"],
    "Germany": ["Berlin", "Munich", "Hamburg"],
    "France": ["Paris", "Lyon", "Marseille"],
    "Spain": ["Madrid", "Barcelona", "Valencia"],
    "Italy": ["Milan", "Rome", "Turin"],
    "Poland": ["Warsaw", "Krakow"],
    "Japan": ["Tokyo", "Osaka"],
    "South Korea": ["Seoul", "Busan"],
    "Australia": ["Sydney", "Melbourne"],
    "Brazil": ["Sao Paulo", "Rio de Janeiro"],
    "Mexico": ["Mexico City", "Guadalajara"],
}
SITE_TYPES = ["University Hospital", "Medical Center", "Regional Clinic", "Research Institute", "General Hospital"]
INVESTIGATOR_FIRST = ["Elena", "Marcus", "Aiko", "Priya", "Thomas", "Fatima", "Lucas", "Ingrid", "Sofia", "Daniel", "Noor", "Kenji"]
INVESTIGATOR_LAST = ["Novak", "Reyes", "Tanaka", "Sharma", "Weber", "Haddad", "Bianchi", "Larsen", "Moreau", "Kowalski", "Silva", "Park"]

# baseline screen-fail assumption behind the recruitment plan (varies by
# therapeutic area — stricter inclusion criteria plan for more screen fails)
BASELINE_FAIL_RATE = {
    "Oncology": 0.32, "Cardiology": 0.20, "Neurology": 0.28,
    "Immunology": 0.25, "Infectious Disease": 0.18, "Respiratory": 0.22,
}
SITE_TIERS = [("small", 0.3, 1.2, 5), ("medium", 1.0, 3.0, 3), ("large", 2.5, 6.0, 2)]

RECRUITMENT_NOW = date(2026, 6, 30)          # "actual" data stops here
RECRUITMENT_PLAN_HORIZON = date(2026, 12, 31)  # baseline/plan projects further out


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    total = d.month - 1 + n
    return date(d.year + total // 12, total % 12 + 1, 1)


def _iter_months(start: date, end: date):
    m = _month_start(start)
    end = _month_start(end)
    while m <= end:
        yield m
        m = _add_months(m, 1)


def _studies_frame() -> pl.DataFrame:
    return pl.DataFrame([
        {
            "study_id": sid, "study_name": name, "phase": phase, "therapeutic_area": area,
            "sponsor": sponsor, "target_enrollment": target, "planned_start_date": start, "status": status,
        }
        for sid, name, phase, area, sponsor, target, start, status in STUDIES
    ])


def _study_countries_and_sites(rng: random.Random) -> tuple[pl.DataFrame, pl.DataFrame]:
    country_rows, site_rows = [], []
    country_pool = list(COUNTRY_WEIGHTS)
    weights = list(COUNTRY_WEIGHTS.values())

    for sid, _name, phase, _area, _sponsor, _target, start, study_status in STUDIES:
        lo, hi = COUNTRIES_PER_PHASE[phase]
        n_countries = rng.randint(lo, hi)
        chosen = rng.sample(country_pool, k=min(n_countries, len(country_pool)))
        planning = study_status == "Planning"
        for country in chosen:
            study_country_id = f"{sid}-{COUNTRY_CODE[country]}"
            activation = start + timedelta(days=rng.randint(0, 90))
            country_status = "Not Yet Recruiting" if planning else rng.choices(
                ["Recruiting", "Closed to Recruitment"], weights=[8, 2]
            )[0]
            country_rows.append({
                "study_country_id": study_country_id, "study_id": sid, "country": country,
                "region": COUNTRY_REGION[country], "country_status": country_status,
                "country_activation_date": activation,
            })

            n_sites = rng.randint(*SITES_PER_COUNTRY)
            for i in range(n_sites):
                site_activation = activation + timedelta(days=rng.randint(0, 120))
                if planning:
                    site_status = "Not Yet Recruiting"
                else:
                    site_status = rng.choices(
                        ["Active", "Closed", "Suspended", "Not Yet Recruiting"], weights=[62, 18, 6, 14]
                    )[0]
                city = rng.choice(CITIES[country])
                site_rows.append({
                    "site_id": f"{study_country_id}-{i + 1:02d}",
                    "study_country_id": study_country_id,
                    "site_name": f"{city} {rng.choice(SITE_TYPES)}",
                    "city": city,
                    "investigator": f"Dr. {rng.choice(INVESTIGATOR_FIRST)} {rng.choice(INVESTIGATOR_LAST)}",
                    "site_activation_date": site_activation,
                    "site_status": site_status,
                    "_phase": phase, "_area": _area,  # carried through for the fact generator, dropped before upload
                })

    return pl.DataFrame(country_rows), pl.DataFrame(site_rows)


def _funnel_split(rng: random.Random, screened: int, fail_rate: float) -> tuple[int, int]:
    """Send each screened patient through the fail/pass funnel independently
    so screened == randomised + screen_fail always holds, exactly like the
    real screening log this fact table stands in for."""
    fails = sum(1 for _ in range(screened) if rng.random() < fail_rate)
    return screened - fails, fails


def _recruitment_events_frame(rng: random.Random, sites_df: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for site in sites_df.iter_rows(named=True):
        phase, area = site["_phase"], site["_area"]
        planned_duration = STUDY_PHASES[phase]
        base_fail_rate = BASELINE_FAIL_RATE[area]
        tier, lo, hi, _w = rng.choices(SITE_TIERS, weights=[w for *_, w in SITE_TIERS])[0]
        monthly_randomised_target = rng.uniform(lo, hi)

        activation = site["site_activation_date"]
        started = site["site_status"] != "Not Yet Recruiting"

        # baseline: the flat plan, generated regardless of what actually happened
        planned_end = _add_months(activation, planned_duration)
        baseline_end = min(planned_end, RECRUITMENT_PLAN_HORIZON)
        if activation <= RECRUITMENT_PLAN_HORIZON:
            br = round(monthly_randomised_target)
            bf = round(br * base_fail_rate / (1 - base_fail_rate))
            bs = br + bf
            for month in _iter_months(activation, baseline_end):
                for etype, count in (("screened", bs), ("randomised", br), ("screen_fail", bf)):
                    rows.append({"event_month": month, "site_id": site["site_id"],
                                 "scenario": "baseline", "event_type": etype, "event_count": count})

        # actual: noisy, ramps up, only for sites that actually opened
        if started and activation <= RECRUITMENT_NOW:
            if site["site_status"] == "Closed":
                actual_end = min(activation + timedelta(days=rng.randint(180, 720)), RECRUITMENT_NOW)
            else:
                actual_end = RECRUITMENT_NOW
            fail_rate = min(0.6, max(0.05, base_fail_rate + rng.uniform(-0.05, 0.08)))
            suspended = site["site_status"] == "Suspended"
            months = list(_iter_months(activation, actual_end))
            for idx, month in enumerate(months):
                ramp = 0.3 if idx == 0 else 0.6 if idx == 1 else 0.85 if idx == 2 else 1.0
                if idx >= len(months) - 2 and site["site_status"] == "Closed":
                    ramp *= 0.5  # wind-down before closure
                if suspended:
                    ramp *= 0.15
                if rng.random() < 0.08:
                    screened = 0
                else:
                    mean = monthly_randomised_target / (1 - fail_rate) * ramp
                    screened = max(0, round(rng.gauss(mean, max(mean * 0.4, 0.4))))
                randomised, screen_fail = _funnel_split(rng, screened, fail_rate)
                for etype, count in (("screened", screened), ("randomised", randomised), ("screen_fail", screen_fail)):
                    rows.append({"event_month": month, "site_id": site["site_id"],
                                 "scenario": "actual", "event_type": etype, "event_count": count})

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
    client.put_object(Bucket=config.BUCKET, Key="ref/regions.csv",
                      Body=_regions_frame().write_csv().encode())
    client.put_object(Bucket=config.BUCKET, Key="ref/territories.csv",
                      Body=_territories_frame().write_csv().encode())
    write_deltalake(f"s3://{config.BUCKET}/logistics/shipments", _shipments_frame(rng),
                    storage_options=config.delta_write_options())
    _upload(client, "subscriptions/subs.parquet", _subscriptions_frame(rng))

    countries, sites = _study_countries_and_sites(rng)
    events = _recruitment_events_frame(rng, sites)
    client.put_object(Bucket=config.BUCKET, Key="ref/studies.csv",
                      Body=_studies_frame().write_csv().encode())
    client.put_object(Bucket=config.BUCKET, Key="ref/study_countries.csv",
                      Body=countries.write_csv().encode())
    client.put_object(Bucket=config.BUCKET, Key="ref/study_sites.csv",
                      Body=sites.drop("_phase", "_area").write_csv().encode())
    _upload(client, "recruitment/events.parquet", events)

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


def seed_bootstrap_admin() -> bool:
    """First-run only: when zero accounts exist, create the bootstrap admin
    with a random password and announce it loudly — the demo stays
    zero-config without ever shipping a well-known credential. Never runs
    again once any account exists (so a production DB can't regress to a
    printed password). Returns True if seeded."""
    import secrets as _secrets

    from . import auth
    from .registry import registry

    store = registry.auth_store
    if store.count_users() > 0:
        return False
    password = _secrets.token_urlsafe(12)
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

"""Generate a taxi-shaped dataset with DuckDB: 4 monthly parquet files,
~1M rows each, 18 columns — the same shape/order of magnitude as the NYC TLC
yellow-taxi months the user is querying. Written once into DATA_DIR.
"""
import sys
from pathlib import Path

import duckdb

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
ROWS_PER_MONTH = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000

MONTHS = ["2024-01", "2024-02", "2024-03", "2024-04"]

SQL = """
COPY (
  SELECT
    1 + (r % 2)                                        AS "VendorID",
    TIMESTAMP '{month}-01 00:00:00'
      + INTERVAL (r % {span}) SECOND                   AS tpep_pickup_datetime,
    TIMESTAMP '{month}-01 00:00:00'
      + INTERVAL ((r % {span}) + 300 + (r % 1800)) SECOND AS tpep_dropoff_datetime,
    CASE WHEN r % 19 = 0 THEN NULL
         WHEN r % 11 = 0 THEN 9
         ELSE 1 + (r % 6) END::DOUBLE                  AS passenger_count,
    ROUND(0.5 + (r % 3000) / 100.0
      + CASE WHEN r % 97 = 0 THEN 25.0 ELSE 0 END, 2)  AS trip_distance,
    CASE WHEN r % 19 = 0 THEN NULL ELSE 1.0 + (r % 3) END AS "RatecodeID",
    CASE WHEN r % 2 = 0 THEN 'N' ELSE 'Y' END          AS store_and_fwd_flag,
    1 + (r % 260)                                      AS "PULocationID",
    1 + ((r * 7) % 260)                                AS "DOLocationID",
    1 + (r % 4)                                        AS payment_type,
    ROUND(3.0 + (hash(r) % 500000) / 100.0, 2)         AS fare_amount,
    ROUND((hash(r + 1) % 400) / 100.0, 2)              AS extra,
    ROUND((hash(r + 2) % 100) / 100.0, 2)              AS mta_tax,
    ROUND((hash(r + 3) % 70000) / 100.0, 2)            AS tip_amount,
    ROUND((hash(r + 4) % 3000) / 100.0, 2)             AS tolls_amount,
    ROUND((hash(r + 5) % 100) / 100.0, 2)              AS improvement_surcharge,
    ROUND(4.3 + (hash(r + 6) % 600000) / 100.0, 2)     AS total_amount,
    ROUND((hash(r + 7) % 250) / 100.0, 2)              AS congestion_surcharge
  FROM (SELECT range AS r FROM range({rows}))
) TO '{path}' (FORMAT parquet, COMPRESSION snappy)
"""


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for month in MONTHS:
        path = DATA_DIR / f"yellow_tripdata_{month}.parquet"
        if path.exists():
            print(f"{path.name}: already generated")
            continue
        days = 29 if month == "2024-02" else 31 if month in ("2024-01", "2024-03") else 30
        con.execute(SQL.format(month=month, rows=ROWS_PER_MONTH,
                               span=days * 24 * 3600 - 3600, path=path))
        print(f"{path.name}: {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

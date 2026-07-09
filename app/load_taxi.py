"""Download NYC TLC yellow-taxi months into data_cache/taxi (public dataset,
~50MB / ~3M rows per month) for large-fact-table testing.

Usage:
    python -m app.load_taxi                  # 2024-01 .. 2024-04 (~13M rows)
    python -m app.load_taxi 2024-05 2024-06  # specific months

On the next app start the cache is uploaded into the emulator bucket under
s3://cash-intel/taxi/ automatically (see seed._upload_local_cache);
models/taxi.yaml queries it. Stick to months within one year — the TLC schema
drifts across years and a mixed-schema glob will fail to scan.
"""
import sys
import urllib.request
from pathlib import Path

from . import config

URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{month}.parquet"


def main() -> None:
    months = sys.argv[1:] or ["2024-01", "2024-02", "2024-03", "2024-04"]
    dest = config.PROJECT_ROOT / "data_cache" / "taxi"
    dest.mkdir(parents=True, exist_ok=True)
    for month in months:
        path = dest / f"yellow_tripdata_{month}.parquet"
        if path.exists():
            print(f"{path.name}: already cached")
            continue
        print(f"{path.name}: downloading…")
        urllib.request.urlretrieve(URL.format(month=month), path)
        print(f"{path.name}: {path.stat().st_size / 1e6:.0f} MB")
    print("done — restart the app (the bucket reseeds from the cache) and query the 'taxi' model")


if __name__ == "__main__":
    main()

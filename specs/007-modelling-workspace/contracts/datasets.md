# Contract: `GET /api/datasets` (new)

Read-only. Groups the objects in the configured bucket into pickable datasets for the model-authoring dataset picker. Reuses the same `s3.client()` + `list_objects_v2` walk as `GET /api/explorer` and the same model-matcher logic, then groups by directory prefix and infers a source format.

New router: `app/api/datasets.py`, mounted under the existing `/api` prefix (registered in `app/main.py` alongside the other routers).

## Request

```
GET /api/datasets
```

No parameters. (The bucket is fixed by `config.BUCKET`.)

## Response `200`

```jsonc
{
  "bucket": "cash-intel",
  "endpoint": "http://127.0.0.1:9600",
  "datasets": [
    {
      "key": "sales/orders",
      "path": "s3://cash-intel/sales/orders/*.parquet",
      "format": "parquet",
      "object_count": 4,
      "bytes": 10485760,
      "format_ambiguous": false,
      "models": [ { "name": "sales", "role": "source" } ],
      "objects": [
        { "key": "sales/orders/2023.parquet", "size": 2621440, "format": "parquet" }
      ]
    }
  ]
}
```

- `datasets` sorted by `key`. Empty bucket → `"datasets": []`.
- `format` ∈ `semantic.SOURCE_FORMATS` (`parquet` | `csv` | `delta`). A prefix containing a `_delta_log/` entry is reported once as a single `delta` dataset whose `path` is the table root (no `/*.ext` glob), mirroring how Delta sources are written today.
- `format_ambiguous: true` when a prefix mixes data extensions; `format` is then the dominant one and the picker warns.
- Prefixes with no recognized data extension are omitted (they cannot back a valid `source`).
- `models[].role` reuses Explorer roles (`source`, `join: <name>`, `import: <bundle>.<dataset>`); informational only, never restricts selection.

## Errors

- Bucket unreachable → `502` (or a `{bucket, endpoint, datasets: []}` with a surfaced note) consistent with how the Explorer handles S3 failures. Prefer failing loudly so the picker can show "bucket not reachable" rather than a misleading empty state.

## Pure helpers (unit-tested without a bucket) — `app/semantic.py`

- `infer_format(keys: list[str]) -> tuple[str | None, bool]` → (format, ambiguous). `None` format when unrecognized.
- `group_objects(objects: list[dict]) -> list[dict]` → groups `{key,size}` by directory prefix into the dataset shape above (minus the `models` mapping, which the router layers on).

## Tests

- Unit (`tests/test_datasets.py` or `test_semantic.py`): prefix grouping, extension inference, delta-root detection, mixed-extension `format_ambiguous`, empty input.
- Integration (`tests/test_api.py`, `seeded` fixture): known seed prefixes present with correct formats; a model's source location shows that model under `models`; response shape matches this contract.

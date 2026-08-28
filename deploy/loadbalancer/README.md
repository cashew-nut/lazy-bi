# Cache-affinity routing

Optional. Read [docs/scaling.md §2](../../docs/scaling.md) for the reasoning;
the short version is that the app is **correct** under plain round-robin and
this only buys cache warmth.

A query's cost here is dominated by S3 round trips, and everything that avoids
them — DuckDB's parquet-footer cache, its external file cache, the pinned
lookup tables — lives on the replica that did the reading. Round-robin
therefore pays for N cold caches instead of one warm one. Hashing requests to
replicas by **the model they query** keeps a model's sources warm in one
place.

Hash on the model, not the user: two analysts on `sales` should share a
replica; one analyst moving from `sales` to `marketing` should not stay put.

## Three ways to do it

**Standalone nginx** — [`nginx.conf`](nginx.conf). Consistent hashing
(`hash … consistent`), so a scale event remaps ~1/N of the keys rather than
all of them.

**ingress-nginx** — one annotation on the Ingress, no separate proxy:

```yaml
nginx.ingress.kubernetes.io/upstream-hash-by: "$arg_model"
nginx.ingress.kubernetes.io/upstream-hash-by-subset: "true"
```

**AWS ALB** — cannot hash on an arbitrary key. Its only affinity is a
cookie-based sticky session, which keys on the *user* and so warms the wrong
thing (a user who queries five models pins five models' sources to one
replica). Either accept round-robin and scale vertically for warmth, or put
nginx/Envoy behind the ALB.

## When not to bother

- **One dominant model.** Affinity turns a hot model into a hot replica.
  Round-robin spreads CPU better; scale vertically for warmth instead.
- **Fewer than three replicas.** The warmth you gain is small and the
  imbalance you risk is not.

## A caveat about `/api/query`

`POST /api/query` carries the model in its **request body**, and no proxy can
hash on a body. The configs here read `?model=` from the URL, which the
backend ignores as an unknown query parameter — so if affinity matters for
your traffic mix, have the frontend append it. Without that, the query
endpoint round-robins while the model/dimension/schema endpoints (which do
carry the name in the path) do not.

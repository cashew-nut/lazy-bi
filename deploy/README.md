# Deployment

Manifests for running Cash Intelligence horizontally scaled. Read
[docs/scaling.md](../docs/scaling.md) first — it explains *why* the split
below exists; this directory is the *how*.

One image, two roles, set by `CI_ROLE`:

| Role | Replicas | In the load balancer | Runs pipelines |
|---|---|---|---|
| `web` | N | ✅ | ❌ |
| `worker` | 1 (usually) | ❌ | ✅ |

Everything here assumes the three things the preflight checks for
(`app/cluster.py`):

1. **An external object store.** Real S3, or MinIO. Never the embedded
   emulator — it is in-memory and per-replica, so each replica would serve a
   different demo bucket. Set `CI_DEMO=0` for a deployment that only reads
   your own data.
2. **`CI_DUCKDB_PATH` left unset** (`:memory:`). DuckDB takes an exclusive
   lock on a database file.
3. **A shared store.** SQLite on a shared volume works for replicas on one
   host; across hosts you want Postgres behind the store classes — see
   docs/scaling.md §6.

---

## Contents

| Path | What it is |
|---|---|
| `kubernetes/` | Deployments (web + worker), Service, HPA, ConfigMap, Secret template, Ingress |
| `ecs/` | ECS/Fargate task definitions and service definitions (web + worker) |
| `loadbalancer/` | Cache-affinity routing: nginx config, the ingress-nginx annotation, and what ALB can't do (docs/scaling.md §2) |
| `../docker-compose.scale.yml` | A locally runnable 3-replica cluster: MinIO + 2 web + 1 worker |

---

## Try it locally first

```bash
docker compose -f docker-compose.yml -f docker-compose.scale.yml --profile scale up
```

Stands up MinIO, seeds the demo bucket into it once (under the boot lock),
and runs two `web` replicas behind nginx on `:8090` plus one `worker`. Then:

```bash
# two replicas answering — different node ids
curl -s localhost:8090/api/health | python3 -m json.tool | grep node
curl -s localhost:8090/api/health | python3 -m json.tool | grep node

# the cluster's own view (admin credentials required)
curl -s -u ... localhost:8090/api/cluster | python3 -m json.tool
```

This is the fastest way to see coupling #5 from docs/scaling.md fixed: save a
model against one replica, then hit the other and watch it appear within
`CI_CLUSTER_POLL_SECONDS`.

---

## Kubernetes

```bash
kubectl create namespace cash-intel
kubectl -n cash-intel apply -f kubernetes/configmap.yaml
# fill in real values first — the file in git holds placeholders only
kubectl -n cash-intel apply -f kubernetes/secret.example.yaml
kubectl -n cash-intel apply -f kubernetes/web-deployment.yaml
kubectl -n cash-intel apply -f kubernetes/worker-deployment.yaml
kubectl -n cash-intel apply -f kubernetes/service.yaml
kubectl -n cash-intel apply -f kubernetes/hpa.yaml
kubectl -n cash-intel apply -f kubernetes/ingress.yaml
```

Two settings deserve attention rather than acceptance:

- **`terminationGracePeriodSeconds` on the worker (90) exceeds
  `CI_CLUSTER_LEASE_SECONDS` (60).** A worker killed mid-run must have time to
  finish or cleanly lose its claim; kill it faster and its run waits out the
  lease before anything reclaims it.
- **The HPA scales on CPU, not requests, with a long scale-in window.** Every
  new replica starts with a cold DuckDB cache, so flapping costs S3 round
  trips in a way it does not in most apps (docs/scaling.md §9).

## ECS / Fargate

`ecs/` holds two task definitions and two service definitions. The same two
notes apply: the worker service runs `desiredCount: 1` with no load balancer
attached, and `stopTimeout` is set above the lease.

Credentials come from the task role — `app/config.py` falls through to
boto3's own chain, so `AWS_ACCESS_KEY_ID` need not be set at all.

## Load-balancer affinity

`loadbalancer/` holds an nginx config that hashes on the requested model, so
a model's queries keep landing on the replica whose cache is already warm,
plus the one-annotation equivalent for ingress-nginx and an honest note on
what an ALB can and cannot do here. This is an optimization, never a
correctness requirement — the app is correct under round-robin, just colder.
See [loadbalancer/README.md](loadbalancer/README.md).

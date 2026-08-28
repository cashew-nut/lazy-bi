# Horizontal Scaling & Cloud Deployment

The [architecture](architecture.md) page describes a deliberately
single-process design. This page is about what happens when one process is no
longer enough: which parts of that design scale by simply running more copies,
which parts are *assumptions* that silently stop being true at two replicas,
and what the code now does about each.

It is written to be read in order. The short version:

> **DuckDB is not the problem — it is the reason this scales well.** It is an
> embedded engine, so the process *is* the query engine: adding a replica adds
> a whole engine, with its own threads, its own memory and its own connection
> to S3. There is no shared executor to contend on and no coordinator to route
> through. What does not survive scale-out is a handful of places where the
> code was entitled to assume it was the only process alive. Those are now
> coordinated through the shared store, and none of it is on the query path.

---

## 1. What "single process" actually meant

"Single process" in the architecture doc is five separate couplings wearing
one label. They fail differently, at different sizes, and three of them fail
*silently* — which is why they are worth naming individually before proposing
anything.

| # | The coupling | Why it existed | What happens at N replicas | Status |
|---|---|---|---|---|
| 1 | **One DuckDB connection per process** | Its object cache, external file cache and keep-alive HTTP connections live on the *instance*. A second connection starts cold. | Nothing breaks. Each replica has its own warm cache; memory and S3 round trips multiply by N. | **Keep it.** §2 |
| 2 | **Embedded moto S3 emulator** for the demo bucket | Zero-config demo | Each replica serves a *different* demo bucket. No error — the catalog just appears to flicker. | **Refused at boot.** §5 |
| 3 | **SQLite, single writer** | Simplicity; it holds platform metadata, never business data | Write contention on one host; broken locking across hosts | **WAL + busy timeout** makes multi-*process* work today; multi-*host* wants Postgres. §6 |
| 4 | **One FIFO pipeline worker thread** = "at most one run platform-wide" | A run *writes* a shared bucket path | N workers, N queues, no guarantee. Two runs materialize into the same path concurrently. **Silent.** | **Fixed** — leased claim + per-target lock. §3 |
| 5 | **In-process caches and registry** | One process sees all its own writes | A write or a model save on replica A is invisible on replica B until a TTL lapses — or forever, for the registry. **Silent.** | **Fixed** — change generations. §4 |

Couplings 4 and 5 are the interesting ones. They are not performance limits;
they are correctness bugs that only exist above one replica, and neither
produces an error message when it bites. Everything else on this page is
comparatively mechanical.

---

## 2. DuckDB: the process is the query engine

The instinct on reading "one DuckDB connection" is that DuckDB is a
bottleneck to be replaced with something shared. It is worth being precise
about why that is backwards.

DuckDB is embedded and **releases the GIL during execution**. A FastAPI worker
with a dozen concurrent `/api/query` requests is genuinely executing them in
parallel across DuckDB's own thread pool, not queueing them behind an
interpreter lock. The ceiling on one replica is CPU and RAM, not Python — and
that ceiling is raised by giving the container more of both, or by adding
replicas.

So the scale-out unit is the process, and **each replica is a complete,
independent query engine**:

```
                    ┌──────────────┐
                    │ Load balancer│   session cookie / bearer token
                    └──────┬───────┘   (no server-side session state:
                           │            sessions are rows in the store)
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ┌─────────┐        ┌─────────┐        ┌─────────┐
   │ web-1   │        │ web-2   │        │ web-3   │     CI_ROLE=web
   │ DuckDB  │        │ DuckDB  │        │ DuckDB  │     one connection each,
   │ + cache │        │ + cache │        │ + cache │     own warm caches
   └────┬────┘        └────┬────┘        └────┬────┘
        │                  │                  │
        └────────┬─────────┴──────────────────┘
                 │ httpfs (projection + predicate pushdown)
                 ▼
        ┌────────────────────┐         ┌──────────────────┐
        │  S3 / object store │◄────────┤ worker-1         │  CI_ROLE=worker
        │  (the source of    │  writes │ pipeline runs    │  drains the queue,
        │   truth)           │         │ (subprocesses)   │  holds target locks
        └────────────────────┘         └────────┬─────────┘
                 ▲                              │
                 │                              │
        ┌────────┴──────────────────────────────┴─────────┐
        │ shared store — Postgres (or SQLite on one host) │
        │ users/sessions · visuals · run queue · local     │
        │ models · cluster_locks · cluster_generations     │
        └──────────────────────────────────────────────────┘
```

Three consequences worth stating:

**Cache warmth does not shard for free.** Replica 2 has never read the
parquet footers replica 1 has. With N replicas you pay roughly N cold starts
and hold N copies of the pinned lookup tables. That is a real cost, and it is
the one thing about scaling this app that argues *against* going wide too
early — it trades round trips (the thing
[Principle II](../.specify/memory/constitution.md) counts) for concurrency.
Two mitigations, in order of preference:

  1. **Vertical first.** A replica with more cores and more RAM serves more
     concurrent queries *and* keeps one warm cache. Scale out when a single
     container is saturated, not before.
  2. **Cache-affinity routing.** Because a query's cost is dominated by which
     *sources* it touches, hashing on the model name keeps a model's queries
     landing on the same replica. `/api/health` reports `node` for exactly
     this — send the same request twice and two different node ids means
     affinity is not working. Nginx/Envoy config in
     [`deploy/`](../deploy/README.md).

**`CI_DUCKDB_PATH` must stay `:memory:`.** DuckDB takes an exclusive lock on
a database file; two replicas pointed at one path on a shared volume means the
second fails to open it. The preflight (§5) refuses to start on this.

**Give the container explicit limits.** `CI_DUCKDB_THREADS` and
`CI_DUCKDB_MEMORY_LIMIT` — without them DuckDB sizes itself from the *host's*
cores and RAM, which in a scheduler is how a memory limit turns into an OOM
kill instead of a spill to disk.

---

## 3. Pipeline runs: the one dangerous assumption

The original guarantee, from `app/pipeline_jobs.py`:

> at most one run executes platform-wide at any moment, enforced simply by
> there being one consumer thread pulling from one queue

That enforcement is a property of the *deployment*, not the code. Run a second
replica and there are two threads and two queues — and because a run writes to
a shared bucket path, two concurrent runs of the same pipeline interleave their
writes into one Delta table. Nothing errors. This is the most dangerous thing
about scaling this app, and it is dangerous specifically because it is quiet.

The guarantee now lives where the process count cannot change it:

- **The queue is the `pipeline_runs` table**, not the in-memory
  `queue.Queue`. `PipelineStore.claim_next_run` claims a run with one atomic
  `UPDATE … WHERE status = 'queued'`, so exactly one worker gets it however
  many ask. The in-process queue survives as a *doorbell* — it wakes a worker
  in the same process immediately, saving a poll interval; a run triggered on
  another replica is found by the poll.
- **Mutual exclusion is per target, not global.** A run holds a cluster lock
  named `pipeline_target:<path>` for its duration. That is the real
  invariant — two runs writing *the same path* must not overlap — and it is
  stronger than the old global rule where it matters (it holds across
  processes) and deliberately weaker where it did not: two pipelines with
  different targets now run concurrently, on different workers.
- **A dead worker is detected, not assumed.** A claim carries a lease the
  worker renews while its subprocess runs. Stop renewing and the run is swept
  to `interrupted` by whoever notices.

The startup sweep changed for the same reason. It used to mark **every**
queued-or-running row interrupted on the reasoning "if I am starting, nothing
can be running" — true of exactly one process, and destructive with two: a
restarting replica would declare a peer's in-flight run dead *and* drain the
queue on its way past. It is now scoped to the restarting node's own claims,
with expired leases handled separately.

**How many workers?** Usually one. The lock makes more than one safe, and a
second is worth it when you have several independent pipelines whose targets
never collide. Zero workers is a valid configuration too — the queue simply
accumulates, and `/api/cluster` shows nothing draining it.

---

## 4. Cache and registry: one replica's write is everyone's

Two symptoms, one shape.

**Stale data after a pipeline run.** `cache.clear()` and `duck.invalidate()`
are called wherever the platform writes to the bucket. Both are process-local:
they fix the replica that did the writing and leave every other replica
serving pins and cached bytes from before the write, for up to
`CI_SOURCE_CACHE_TTL` (60s) or `CI_SCHEMA_CACHE_TTL` (300s).

**"My model disappeared."** A model saved through replica A lives in A's
in-memory `registry.models`. A hard refresh that load-balances to B reports it
missing — and nothing was ever going to make B reload.

Both are now driven by **change generations**: a monotonic counter per kind of
change, in the shared store, which every replica polls.

```
replica A                      shared store                 replica B
─────────                      ────────────                 ─────────
pipeline run finishes
cache.clear()                                               (serving)
duck.invalidate()  ──bump──►  data: 41 → 42
                                   │
                                   │  ◄── poll (≤ CI_CLUSTER_POLL_SECONDS)
                                   └──────────────────────►  observed 41 < 42
                                                             cache.clear()
                                                             duck.invalidate()
```

- `data` — the bucket's contents changed (a pipeline run, an upload, a
  delete). Reactions: `cache.clear()` then `duck.invalidate()`, the same pair
  and the same order as the local call sites.
- `config` — models, bundles, pipelines or agents changed. Reaction:
  `registry.reload_all()`.

A node that bumps a generation records it as already-applied, so it never
reacts to its own change and replicas cannot bump each other in a loop. The
staleness bound on a scaled-out deployment is therefore one poll interval —
5 seconds by default, two indexed single-row reads apiece.

**What is deliberately *not* here:** invalidation is not a pub/sub push, and
it does not need to be. The alternative — Redis, or a message bus — buys
sub-second propagation and costs a new piece of infrastructure that must be
running for the app to be correct. A polled counter degrades to "slightly
stale" when the store is slow; a bus that is down is an outage.

---

## 5. Boot: what happens once, happens once

Seeding the demo bucket, creating the bootstrap admin and seeding the demo
notebook are all "do it if it hasn't been done" — a check-then-act that N
replicas booting from one image at the same second all pass. Two bootstrap
admins, two printed passwords, a half-seeded bucket read while it is still
being written.

They now run under a **boot lock**. Replicas that lose the race wait for it
(up to 120s) rather than skipping, because the loser needs the *result* — a
replica that starts serving before the demo bucket exists answers with an
empty catalog — and by the time they get it, their own first-run checks
correctly find nothing to do.

A clustered process also runs a **preflight** that refuses to start on
configuration that cannot survive a second replica:

- the demo bucket served by this process's own in-memory emulator;
- `CI_DUCKDB_PATH` pointing at a file.

Fatal rather than a warning, because both are silent at runtime — the first
*answers queries*, just differently per replica. `CI_CLUSTER_PREFLIGHT=0`
overrides it.

---

## 6. State: what has to be shared

| State | Today | Scaled out |
|---|---|---|
| Business data | S3 | **Unchanged** — already shared, already the source of truth |
| Demo catalog | in-process moto | MinIO, or a real bucket, or `CI_DEMO=0` |
| Users, sessions, tokens | SQLite | Shared store. Sessions are rows, not server memory, so **no sticky sessions are needed** |
| Visuals, dashboards, notebooks, conversations, run history, local models | SQLite | Shared store |
| Uploaded datasets (`CI_LOCAL_DATA_DIR`) | local disk | Shared volume (EFS), or accept per-replica |
| Coordination (locks, generations, roster) | — | `cluster_*` tables in the shared store |
| MCP sessions | none (`stateless_http=True`) | **Unchanged** — already stateless, deliberately |

SQLite now opens in **WAL mode with a busy timeout** (`app/sqlitedb.py`),
which is what makes more than one process on one host workable at all — the
defaults take a whole-file write lock and a zero-length timeout, so a reader
arriving during a commit fails outright rather than waiting a millisecond.
This does not change the single-writer *design*; it makes it survive being
written from a `web` replica and a `worker` at once.

**It does not make SQLite a multi-host store.** Network filesystems break its
locking. Across hosts, the store classes want Postgres behind them — the
migration this design is written to accommodate rather than to perform:

- Every store class already isolates its own SQL behind a small method
  surface, and `app/sqlitedb.py` is the single place a connection is opened.
- `app/clusterstore.py` deliberately uses only SQL that SQLite and PostgreSQL
  spell identically (`INSERT … ON CONFLICT … DO UPDATE … WHERE`). The two
  dialect-specific fragments — the clock expressions — are isolated as
  constants. On Postgres they become `now()` and `now() + interval`.
- **All lease time comes from the database's clock, never a process's**, so
  two hosts with drifting clocks still agree on whether a lease has expired.
  This is the property that makes leases safe across machines, and it is why
  those timestamps are computed in SQL rather than passed in as parameters
  like the rest of this codebase's `_now()` columns.

---

## 7. Roles: one image, two jobs

`CI_ROLE` splits the image without splitting the build:

| Role | Serves HTTP | Runs pipelines | Scale |
|---|---|---|---|
| `all` | ✅ | ✅ | **1** — the default; exactly the pre-existing behaviour |
| `web` | ✅ | ❌ | **N** — this is the read path |
| `worker` | ✅ (health only, by routing) | ✅ | 1, usually |

A `worker` still mounts the whole API — one image, one entrypoint, and a
health endpoint a scheduler can probe. It is simply not in the load balancer's
target group, so nothing routes a user query onto a node busy materializing a
table.

Every setting is inert unless `CI_CLUSTERED=1`. Unclustered, `cluster.lock()`
is a `threading.Lock`, generations are process-local integers, and no watcher
thread starts. That is a promise, not an optimization: **the default
deployment behaves exactly as it did before any of this existed.**

---

## 8. Observability

A horizontally-scaled deployment of this app previously had no way to answer
the questions that matter when it misbehaves. Two additions:

- **`GET /api/health`** (public) now reports `node`, `role` and `clustered`.
  Two identical requests returning two node ids is how you confirm the load
  balancer is spreading — or that affinity is not working.
- **`GET /api/cluster`** (admin) reports the live node roster with roles, the
  change generations and how far behind this node is, and every held lock with
  its holder and expiry. "Is the worker running?", "is a replica stuck a
  generation behind?", "is a pipeline target locked by a node that died?" —
  none of which is visible from a load-balanced `/api/health`.

---

## 9. Sizing and rollout

**When to scale out at all.** Not until one container is saturated. The
question to ask is which resource is actually exhausted:

| Symptom | Cause | Fix |
|---|---|---|
| Queries slow, CPU pinned | DuckDB is CPU-bound on aggregation | More cores, then replicas |
| Queries slow, CPU idle | S3 round trips | Cache warmth, `CI_SOURCE_CACHE_TTL` — **more replicas make this worse** |
| OOM kills | DuckDB sized from host, not container | `CI_DUCKDB_MEMORY_LIMIT` |
| Requests queueing, CPU idle | FastAPI threadpool exhausted by blocking calls | More uvicorn threads, then replicas |
| Pipelines queueing behind each other | One worker, several targets | A second `worker` replica |

**Rolling deploys** work without special handling: generation names are a
cross-version contract (a bump written by an old replica means the same thing
to a new one), the run claim is atomic across versions, and a replica taken
down mid-run has its run swept when its lease expires. Drain time should
exceed `CI_CLUSTER_LEASE_SECONDS` so a worker finishes or cleanly loses its
claim rather than being killed mid-write.

**Autoscaling** on `web` replicas: scale on CPU, not request count, and set a
generous scale-in cooldown — every new replica starts with a cold cache, so
flapping is expensive in S3 round trips in a way it is not in most apps.

---

## 10. Configuration reference

| Variable | Default | What it does |
|---|---|---|
| `CI_ROLE` | `all` | `all` / `web` / `worker` — see §7 |
| `CI_CLUSTERED` | `0` | Assume other processes share this state. Set it whenever more than one process points at the same store |
| `CI_NODE_ID` | `hostname:pid` | This process's name in logs, `/api/cluster` and run claims |
| `CI_CLUSTER_POLL_SECONDS` | `5` | How often to check for peers' changes — the staleness bound (§4) |
| `CI_CLUSTER_LEASE_SECONDS` | `60` | How long a lock or claim survives without renewal (§3) |
| `CI_CLUSTER_PREFLIGHT` | `1` | Refuse to start on unsafe multi-replica config (§5) |
| `CI_SQLITE_BUSY_TIMEOUT` | `10` | Seconds to wait for another process's write lock (§6) |
| `CI_DUCKDB_PATH` | `:memory:` | **Must stay `:memory:`** when clustered (§2) |
| `CI_DUCKDB_THREADS` / `CI_DUCKDB_MEMORY_LIMIT` | unset | Set both in a container (§2) |

Deployment manifests — Kubernetes, ECS/Fargate, a local three-replica compose
profile, and load-balancer affinity config — are in
[`deploy/`](../deploy/README.md).

---

## 11. What this design does *not* do

Stated plainly, because each is a defensible thing to want later:

- **No shared query cache.** Each replica warms its own. A shared result cache
  is a different feature with its own invalidation problem; the generation
  counters here are the hook it would hang on.
- **No pub/sub.** Propagation is a poll, bounded by
  `CI_CLUSTER_POLL_SECONDS` — see §4 for why that trade is deliberate.
- **No Postgres implementation.** The seams are placed and the SQL is
  portable (§6), but the swap itself is not done. Multi-host deployment needs
  it.
- **No multi-tenancy.** Replicas are interchangeable; there is no per-tenant
  routing or isolation.
- **Uploaded datasets stay per-node** unless `CI_LOCAL_DATA_DIR` is on shared
  storage. The startup banner warns about this rather than fixing it.

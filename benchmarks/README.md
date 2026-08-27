# Latency benchmarks

The harness behind the README's "against a real object store" numbers: moto
standing in for S3, fronted by `latency_proxy.py` — a counting proxy that
sleeps a configurable per-request latency (default 40ms) to approximate a
real endpoint's RTT — with the app driven through its HTTP API exactly the
way the browser drives it (login, validate-per-keystroke, save, query).

```bash
.venv/bin/python benchmarks/gen_data.py    # once: 4 x ~33MB taxi-shaped parquet
.venv/bin/python benchmarks/bench.py       # the authoring-loop scenario
```

Knobs (env):

- `BENCH_LATENCY_MS` — injected per-request latency (default 40)
- `BENCH_DEMO=1` — load the demo catalog alongside, like a default install
- `BENCH_PROFILE=<name>` — resolve credentials through an `~/.aws/config`
  profile instead of static keys; point it at one whose resolution is slow
  (e.g. a `credential_process` that sleeps) to model SSO/corporate auth
- `BENCH_DUMP=1` — print per-step S3 request breakdowns (method, ranged vs
  whole-file, per key)

The proxy preserves `Content-Length` on HEAD responses deliberately: an
intermediary that rewrites it makes DuckDB fall back to whole-file
downloads, which is one of the failure modes the README's troubleshooting
section describes — flip that line if you want to reproduce it.

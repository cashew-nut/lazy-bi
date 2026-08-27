"""End-to-end latency benchmark for the model-authoring loop against a
'real' (latency-injected) S3 endpoint.

Topology:  app  ->  latency proxy (:9602, +RTT per request)  ->  moto (:9601)

Scenario (exactly the loop in the user's screenshots):
  1. open the model form on new YAML      -> POST /api/models/validate   (cold)
  2. keystroke re-validation              -> POST /api/models/validate   (warm?)
  3. create the model                     -> POST /api/models
  4. run the visual's query               -> POST /api/query             (cold)
  5. run a second, similar query          -> POST /api/query             (warm?)
  6. save an edit to the model            -> PUT  /api/models/{n}/yaml
  7. re-run the query after the save      -> POST /api/query             (the pain)
  8. keystroke re-validation after save   -> POST /api/models/validate
  9. save again                           -> PUT  /api/models/{n}/yaml
 10. re-run the query again               -> POST /api/query

Prints per-step wall time, server-reported elapsed_ms where present, and the
number of S3 requests the proxy saw during the step.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REPO = Path(os.environ.get("BENCH_REPO", Path(__file__).resolve().parent.parent))
PY = str(REPO / ".venv" / "bin" / "python")
if not Path(PY).exists():
    PY = sys.executable
MOTO_PORT, PROXY_PORT, APP_PORT = 9601, 9602, 8090
LATENCY_MS = float(os.environ.get("BENCH_LATENCY_MS", "40"))
BUCKET = "lazy-real"
RUN_DIR = HERE / "run"

MODEL_YAML = """\
name: taxi_test
label: Taxi Test
source:
  format: parquet
  path: s3://lazy-real/taxi-nyc/*.parquet
dimensions:
  - name: tpep_pickup_datetime
    label: Tpep Pickup Datetime
    type: time
  - name: passenger_count
    label: Passenger Count
  - name: payment_type
    label: Payment Type
measures:
  - name: avg_trip_distance
    label: Average Trip Distance
    expr: AVG(trip_distance)
  - name: trips
    label: Trips
    expr: COUNT(*)
"""

QUERY_BOTH = {"model": "taxi_test",
              "dimensions": [{"name": "tpep_pickup_datetime", "grain": "1mo"},
                             "passenger_count"],
              "measures": ["avg_trip_distance"], "limit": 1000}
QUERY_ONE = {"model": "taxi_test",
             "dimensions": [{"name": "tpep_pickup_datetime", "grain": "1mo"}],
             "measures": ["avg_trip_distance"], "limit": 1000}


def wait_port(port: int, timeout: float = 30) -> None:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1)
            try:
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.15)
    raise RuntimeError(f"port {port} never came up")


def http(method: str, url: str, body=None, headers=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        return resp.status, (json.loads(payload) if "json" in ctype else payload), dict(resp.headers)


def proxy_stats(reset=False):
    action = "/reset" if reset else ""
    _, body, _ = http("GET", f"http://127.0.0.1:{PROXY_PORT}/__proxy_stats__{action}")
    return body


class Bench:
    def __init__(self):
        self.procs = []
        self.cookie = None
        self.results = []

    def start(self, name, args, env=None, logfile=None):
        log = open(logfile, "w") if logfile else subprocess.DEVNULL
        p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                             env={**os.environ, **(env or {})}, cwd=str(REPO))
        self.procs.append((name, p))
        return p

    def stop_all(self):
        for _, p in reversed(self.procs):
            p.terminate()
        for _, p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def step(self, label, method, path, body=None, expect=200):
        proxy_stats(reset=True)
        t0 = time.perf_counter()
        headers = {"Cookie": self.cookie, "X-Requested-With": "fetch"} if self.cookie else {}
        try:
            status, payload, _ = http(method, f"http://127.0.0.1:{APP_PORT}{path}",
                                      body, headers)
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read().decode()[:400]
        wall = (time.perf_counter() - t0) * 1000
        s3 = proxy_stats()
        server_ms = payload.get("elapsed_ms") if isinstance(payload, dict) else None
        note = ""
        if isinstance(payload, dict):
            if payload.get("ok") is False:
                note = f"VALIDATE-ERR: {str(payload.get('error'))[:120]}"
            if payload.get("schema_error"):
                note = f"SCHEMA-ERR: {str(payload.get('schema_error'))[:120]}"
            if payload.get("row_count") is not None:
                note = f"rows={payload['row_count']}"
        if status != expect:
            note = f"HTTP {status}! {str(payload)[:200]}"
        self.results.append((label, wall, server_ms, s3["total"], s3["by_method"], note))
        print(f"  {label:<42} {wall:>9.0f}ms wall"
              + (f"  {server_ms:>8.1f}ms server" if server_ms is not None else " " * 20)
              + f"  {s3['total']:>4} s3 reqs  {note}", flush=True)
        if os.environ.get("BENCH_DUMP") and s3["total"]:
            from collections import Counter
            kinds = Counter()
            for line in s3["log"]:
                method, rest = line.split(" ", 1)
                path = rest.split(" [")[0]
                ranged = "[range]" if "[" in line else "[full]"
                kinds[f"{method} {ranged} {path.split('/')[-1] or '(list)'}"] += 1
            for k, n in kinds.most_common(12):
                print(f"        {n:>3}x {k}")
        return payload

    def login(self, password):
        req = urllib.request.Request(
            f"http://127.0.0.1:{APP_PORT}/api/auth/login",
            data=json.dumps({"username": "admin", "password": password}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.cookie = resp.headers.get("Set-Cookie", "").split(";")[0]


def main():
    RUN_DIR.mkdir(exist_ok=True)
    db = RUN_DIR / "bench.db"
    for stale in RUN_DIR.glob("bench.db*"):
        stale.unlink()

    bench = Bench()
    try:
        print(f"— latency {LATENCY_MS:.0f}ms/request —")
        bench.start("moto", [PY, "-m", "moto.server", "-p", str(MOTO_PORT),
                             "-H", "127.0.0.1"], logfile=RUN_DIR / "moto.log")
        wait_port(MOTO_PORT)

        # seed the bucket directly against moto (no latency for setup)
        import boto3
        s3 = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{MOTO_PORT}",
                          aws_access_key_id="testing", aws_secret_access_key="testing",
                          region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        data_dir = Path(os.environ.get("BENCH_DATA", HERE / "data"))
        for f in sorted(data_dir.glob("*.parquet")):
            s3.upload_file(str(f), BUCKET, f"taxi-nyc/{f.name}")
        total = sum(f.stat().st_size for f in data_dir.glob("*.parquet"))
        print(f"seeded {len(list(data_dir.glob('*.parquet')))} files, {total/1e6:.0f} MB")

        bench.start("proxy", [PY, str(HERE / "latency_proxy.py"), str(MOTO_PORT),
                              str(PROXY_PORT), str(LATENCY_MS / 1000)],
                    logfile=RUN_DIR / "proxy.log")
        wait_port(PROXY_PORT)

        profile = os.environ.get("BENCH_PROFILE", "")
        demo = os.environ.get("BENCH_DEMO", "0")
        app_env = {
            "CI_ENV_FILE": "", "CI_DEMO": demo,
            "CI_BUCKET": BUCKET,
            "CI_S3_ENDPOINT": f"http://127.0.0.1:{PROXY_PORT}",
            "AWS_REGION": "us-east-1",
            "CI_DB_PATH": str(db),
            "CI_LOCAL_DATA_DIR": str(RUN_DIR / "local_data"),
        }
        if profile:
            app_env["AWS_PROFILE"] = profile
            app_env["AWS_ACCESS_KEY_ID"] = ""
            app_env["AWS_SECRET_ACCESS_KEY"] = ""
        else:
            app_env.update({"AWS_ACCESS_KEY_ID": "testing",
                            "AWS_SECRET_ACCESS_KEY": "testing", "AWS_PROFILE": ""})
        print(f"variant: demo={demo} profile={profile or '(static keys)'}")
        applog = RUN_DIR / "app.log"
        bench.start("app", [PY, "-m", "uvicorn", "app.main:app", "--port",
                            str(APP_PORT), "--host", "127.0.0.1"],
                    env=app_env, logfile=applog)
        wait_port(APP_PORT)
        deadline = time.time() + 20
        password = None
        while time.time() < deadline and password is None:
            m = re.search(r"password: (\S+)", applog.read_text())
            if m:
                password = m.group(1)
            else:
                time.sleep(0.3)
        if not password:
            raise RuntimeError("no bootstrap password in app log")
        time.sleep(1.0)
        bench.login(password)
        print("logged in; scenario:")

        bench.step("0  modelling page: dataset walk", "GET", "/api/datasets")
        bench.step("1  form open: validate (cold)", "POST", "/api/models/validate",
                   {"yaml": MODEL_YAML})
        bench.step("2  keystroke: validate again", "POST", "/api/models/validate",
                   {"yaml": MODEL_YAML + "description: tweaked\n"})
        bench.step("3  create model", "POST", "/api/models", {"yaml": MODEL_YAML},
                   expect=201)
        bench.step("4  query: avg dist by month+passengers", "POST", "/api/query",
                   QUERY_BOTH)
        bench.step("5  query: avg dist by month (warm?)", "POST", "/api/query",
                   QUERY_ONE)
        edited = MODEL_YAML.replace("label: Taxi Test", "label: Taxi Test v2")
        bench.step("6  save edit (PUT yaml)", "PUT", "/api/models/taxi_test/yaml",
                   {"yaml": edited})
        bench.step("7  query after save", "POST", "/api/query", QUERY_BOTH)
        bench.step("8  keystroke: validate after save", "POST", "/api/models/validate",
                   {"yaml": edited})
        edited2 = edited.replace("label: Taxi Test v2", "label: Taxi Test v3")
        bench.step("9  save again", "PUT", "/api/models/taxi_test/yaml",
                   {"yaml": edited2})
        bench.step("10 query after second save", "POST", "/api/query", QUERY_BOTH)

        print("\nsummary (wall ms / s3 requests):")
        for label, wall, server_ms, s3n, by, note in bench.results:
            print(f"  {label:<42} {wall:>9.0f}  {s3n:>4}  {note}")
        (RUN_DIR / "results.json").write_text(json.dumps(
            [{"label": l, "wall_ms": w, "server_ms": s, "s3_requests": n,
              "by_method": b, "note": x}
             for l, w, s, n, b, x in bench.results], indent=2))
    finally:
        bench.stop_all()


if __name__ == "__main__":
    main()

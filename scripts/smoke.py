"""Post-deploy smoke test — run INSIDE the wellness-app container (scripts/deploy.sh does).

Mints a short-lived token for the app user, reads the route list from the app's own
/openapi.json, and GETs every endpoint that needs no parameters, skipping anything with
side effects or external cost. Fails (exit 1) on any 5xx, transport error or a missing
app shell. Born from a month of regressions that were silent until someone looked:
an emptied findings block, a NameError in a helper, an endpoint that 500'd for a day.

    docker exec -w /app wellness-app python /tmp/smoke.py
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")
BASE = "http://localhost:8093"
# Paths that write, call paid APIs, trigger long jobs, or need a body/token of their own.
SKIP = re.compile(r"sync|backfill|generate|refresh|export|admin|webhook|parse|scan|ingest|"
                  r"voice|/raw|widgets|habits/summary|schedule")


def main() -> int:
    from app.auth import USERS, create_access_token
    token = create_access_token(next(iter(USERS)))
    hdr = {"Authorization": f"Bearer {token}"}

    def get(path, auth=True, timeout=30):
        req = urllib.request.Request(BASE + path, headers=hdr if auth else {})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), time.perf_counter() - t0
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300], time.perf_counter() - t0
        except Exception as e:                                   # noqa: BLE001
            return None, repr(e).encode()[:300], time.perf_counter() - t0

    failures, slow = [], []
    status, body, _ = get("/", auth=False)
    if status != 200 or b"<div id=\"root\"" not in body:
        failures.append(("/", status, body[:120]))
    status, body, _ = get("/openapi.json", auth=False)
    if status != 200:
        print(f"FAIL: /openapi.json -> {status}")
        return 1
    spec = json.loads(body)
    paths = []
    for path, ops in spec["paths"].items():
        g = ops.get("get")
        if not g or "{" in path or not path.startswith("/api") or SKIP.search(path):
            continue
        if any(p.get("required") for p in g.get("parameters", [])):
            continue
        paths.append(path)
    for path in sorted(paths):
        status, body, dt = get(path)
        if status is None or status >= 500:
            failures.append((path, status, body[:160]))
        elif dt > 3:
            slow.append((path, round(dt, 1)))
    print(f"smoke: {len(paths)} GET endpoints, {len(failures)} failure(s), {len(slow)} slow (>3 s)")
    for f in failures:
        print("  FAIL", f)
    for s in slow:
        print("  slow", s)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# Run the app on a synthetic year of data. Needs Docker; Node 20+ only if the frontend
# has not been built yet. Nothing here touches a real database.
#
#   scripts/demo.sh             build, seed (~1.5 min), then serve on http://localhost:8093
#   PORT=9000 scripts/demo.sh   serve on another port
#   SERVE=0 scripts/demo.sh     build and seed only (writes backend/data/demo.db)
#
# Sign in as `owner` with password `demo-password`. The planted-vs-found report is written
# to backend/data/demo-report.md (see docs/DEMO.md for what it means).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-8093}
IMAGE=wellness-app-demo

if [[ ! -f backend/static/index.html ]]; then
  echo "Building the frontend…"
  npm --prefix frontend ci --silent
  npm --prefix frontend run build --silent
fi
echo "Building the image…"
docker build -q -t "$IMAGE" backend >/dev/null
mkdir -p backend/data

# Demo-only secrets: the app refuses to start without them, and they protect nothing real.
RUN=(docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/backend/data:/app/data"
     -e TZ=America/New_York -e NTFY_URL= -e HOME=/tmp
     -e SECRET_KEY=demo-secret-key-not-for-production-0123456789
     -e APP_PASSWORD=demo-password -e WEBHOOK_SECRET=demo-webhook-secret
     -e DATABASE_URL=sqlite+aiosqlite:////app/data/demo.db)

echo "Seeding a synthetic year…"
"${RUN[@]}" "$IMAGE" python -m demo.seed /app/data/demo.db
"${RUN[@]}" "$IMAGE" python -m demo.report /app/data/demo.db 2>/dev/null > backend/data/demo-report.md
sed -n '3,11p' backend/data/demo-report.md

if [[ "${SERVE:-1}" == "1" ]]; then
  echo "Serving on http://localhost:$PORT  (user: owner, password: demo-password; Ctrl-C to stop)"
  "${RUN[@]}" -p "127.0.0.1:$PORT:8093" "$IMAGE" uvicorn app.main:app --host 0.0.0.0 --port 8093
fi

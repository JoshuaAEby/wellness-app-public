#!/usr/bin/env bash
# Deploy wellness-app to the server, verify it, and roll back automatically if it's broken.
#
#   scripts/deploy.sh            # full deploy
#   SKIP_SMOKE=1 scripts/deploy.sh   # emergency: deploy without the smoke test
#
# 1. lint + build the frontend, boot the built bundle in happy-dom (frontend/scripts/
#    bootcheck.mjs), byte-compile the backend (catch syntax errors locally)
# 2. tag the running image :prev (the rollback target)
# 3. if this deploy carries a NEW Alembic migration, take an online DB backup first —
#    an image rollback cannot undo a schema change
# 4. rsync + `docker compose up --build -d` (the only correct sequence: the code is baked into the image)
# 5. wait for the container healthcheck
# 6. run scripts/smoke.py inside the container (every parameter-free GET endpoint)
# 7. on failure: retag :prev as latest, restart, and exit non-zero
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${DEPLOY_HOST:?set DEPLOY_HOST, e.g. user@server}
REMOTE=${DEPLOY_PATH:?set DEPLOY_PATH, the compose directory on the server}
BACKUP_DIR=${DEPLOY_BACKUP_DIR:-$REMOTE/backups}
IMAGE=wellness-app-wellness-app
say() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

say "Frontend: lint + tests + build"
npm --prefix frontend run lint --silent
npm --prefix frontend test --silent >/dev/null || { npm --prefix frontend test; exit 1; }
npm --prefix frontend run build --silent >/dev/null

# Does the built bundle actually START? Loads the real entry module in happy-dom, logged
# out and logged in. Catches the class of bug smoke.py can't see (a blank screen): the
# 2026-10-21 manualChunks outage failed here with the exact production error.
say "Frontend: boot check (production build)"
node frontend/scripts/bootcheck.mjs && node frontend/scripts/bootcheck.mjs --authed \
  || { echo "boot check failed — NOT deploying"; exit 1; }

say "Backend: byte-compile"
python3 -m compileall -q backend/app >/dev/null || { echo "compile failed"; exit 1; }
find backend/app -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

LOCAL_HEAD=$(ls backend/alembic/versions | grep -E '^[0-9]{4}_' | sort | tail -1 | cut -c1-4)
REMOTE_HEAD=$(ssh "$HOST" "docker exec wellness-app python -c \"import sqlite3; print(sqlite3.connect('/data/wellness.db').execute('select version_num from alembic_version').fetchone()[0])\"" 2>/dev/null || echo "?")
echo "migrations: local head $LOCAL_HEAD · server at $REMOTE_HEAD"

say "Tag current image as :prev"
ssh "$HOST" "docker tag $IMAGE:latest $IMAGE:prev"

if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  STAMP=$(date +%Y%m%d-%H%M)
  say "New migration ($REMOTE_HEAD → $LOCAL_HEAD): online DB backup first"
  ssh "$HOST" "docker exec wellness-app python -c \"import sqlite3; s=sqlite3.connect('/data/wellness.db'); d=sqlite3.connect('/data/predeploy.db'); s.backup(d); d.close()\" \
    && mkdir -p $BACKUP_DIR && docker cp wellness-app:/data/predeploy.db $BACKUP_DIR/wellness-predeploy-$STAMP.db \
    && docker exec wellness-app rm /data/predeploy.db \
    && ls -1t $BACKUP_DIR/wellness-predeploy-*.db | tail -n +6 | xargs -r rm --"
  echo "backup: $BACKUP_DIR/wellness-predeploy-$STAMP.db (keeping the 5 newest)"
fi

say "Sync + rebuild"
rsync -a --delete --exclude-from=.rsync-exclude backend/ "$HOST:$REMOTE/backend/"
ssh "$HOST" "cd $REMOTE && docker compose up --build -d 2>&1 | tail -2"

wait_healthy() {
  for _ in $(seq 1 60); do
    st=$(ssh "$HOST" "docker inspect wellness-app --format '{{.State.Health.Status}}'" 2>/dev/null || echo "?")
    [[ "$st" == "healthy" ]] && return 0
    sleep 3
  done
  return 1
}

rollback() {
  say "ROLLING BACK to :prev"
  ssh "$HOST" "docker tag $IMAGE:prev $IMAGE:latest && cd $REMOTE && docker compose up -d --no-build 2>&1 | tail -2"
  if wait_healthy; then echo "rolled back; previous image is serving."; else echo "!! rollback container not healthy — check it now"; fi
  [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]] && echo "!! this deploy migrated the DB ($REMOTE_HEAD → $LOCAL_HEAD); the old code now runs on the new schema. Backup above if needed."
  echo "NOTE: $REMOTE/backend now holds the NEW code; the next deploy (or a fix) replaces it."
  exit 1
}

say "Waiting for healthcheck"
wait_healthy || { echo "container never became healthy"; ssh "$HOST" "docker logs --tail 40 wellness-app"; rollback; }

if [[ "${SKIP_SMOKE:-}" == "1" ]]; then
  say "Smoke test SKIPPED (SKIP_SMOKE=1)"
else
  say "Smoke test"
  scp -q scripts/smoke.py "$HOST:/tmp/wellness-smoke.py"
  ssh "$HOST" "docker cp /tmp/wellness-smoke.py wellness-app:/tmp/smoke.py && docker exec -w /app wellness-app python /tmp/smoke.py" || rollback
fi

say "Deployed ✓  ($(git rev-parse --short HEAD)$(git diff --quiet || echo ', with uncommitted changes'))"

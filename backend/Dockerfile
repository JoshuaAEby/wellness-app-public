FROM python:3.11-slim

WORKDIR /app

# Install uv for fast dependency installation
RUN pip install --no-cache-dir uv

# Copy dependency manifests first (layer caching)
COPY pyproject.toml requirements.lock ./

# Install from the LOCK, not from pyproject.toml.
#
# This used to be `uv pip install --system -r pyproject.toml`, which re-resolved
# every dependency from scratch on every build. On 2026-08-25 a rebuild that was
# about something else entirely pulled anthropic 1.0.0 — which had removed the
# `temperature` argument — and food parsing started returning 503. Nothing in
# the app had changed. Every other dependency was equally unbounded, so the same
# thing could have happened inside scipy or statsmodels, where it would have
# been much harder to notice.
#
# requirements.lock pins all 163 packages, direct and transitive, so a build is
# reproducible and a dependency upgrade is a deliberate act with its own commit.
#
# To upgrade dependencies, regenerate the lock in THIS image (so the resolution
# matches the deploy target) and smoke-test the LLM paths afterwards:
#
#   docker run --rm -v "$PWD:/w" -w /w python:3.11-slim sh -c \
#     'pip install -q uv && uv pip compile pyproject.toml -o requirements.lock'
RUN uv pip install --system --no-cache -r requirements.lock

# Copy application code
COPY . .

# Run Alembic migrations then start the server
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8093"]

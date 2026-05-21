# Dockerfile for agent_runner — Fly.io deployable.
#
# Build context is the REPO ROOT, not agent_runner/, because the service
# imports from `bots/`, `league_core/`, and `scripts/` which all live one
# level above agent_runner/. Deploy with:
#
#     fly deploy --dockerfile agent_runner/Dockerfile
#
# from the repo root, OR set `[build] dockerfile = "agent_runner/Dockerfile"`
# in fly.toml and run `fly deploy` from the repo root.

FROM python:3.11-slim

# Set a non-root user as good practice (Fly.io doesn't require it, but
# minor defense-in-depth).
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

# Install Python deps first so they cache when only source changes.
COPY agent_runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy the parts of the repo the service actually needs. We deliberately
# do NOT copy dashboard/ or node_modules — only the Python source.
COPY agent_runner/ /app/agent_runner/
COPY bots/         /app/bots/
COPY league_core/  /app/league_core/
COPY scripts/      /app/scripts/

# COPY runs as root, so /app/ is owned by root and not writable by the
# `app` user. Bots and league_health write state files next to their
# source (e.g., /app/bots/etf_rotation_v1/state.json, /app/scripts/...).
# Without this chown, every state write hits PermissionError(13) and the
# bot cycle fails partway through. Take ownership so the runtime user
# can read AND write.
RUN chown -R app:app /app

# state.json files for stateful bots live next to their main.py, which is
# fine for our single-process scheduler — they persist across job runs as
# long as the container stays up. On container restart they reset; the
# bots self-heal on the next cycle.

USER app

# Default entrypoint — start the scheduler. Fly.io will restart this
# process on crash via its release lifecycle.
CMD ["python", "-m", "agent_runner.scheduler"]

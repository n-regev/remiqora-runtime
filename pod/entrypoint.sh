#!/usr/bin/env bash
# Pod entrypoint (PID 1 is tini). Order matters: the deadline guard is armed BEFORE anything else.
set -euo pipefail
export RMQ_RUN_DIR=/dev/shm/rmq
mkdir -p "$RMQ_RUN_DIR" && chmod 755 "$RMQ_RUN_DIR"
mkdir -p "$RMQ_RUN_DIR/secrets" && chmod 700 "$RMQ_RUN_DIR/secrets"
mkdir -p /workspace/.rmq /workspace/.rmq-scratch && chmod 700 /workspace/.rmq && chmod 750 /workspace/.rmq-scratch
mkdir -p "${REMIQORA_DATA_DIR:?}"

: "${RMQ_DEADLINE:?RMQ_DEADLINE is required — refusing to run without a deadline}"

# 1) deadline guard — restarted if it ever exits
( while true; do python3 -I /opt/rmq/guard.py || true; sleep 5; done ) &

# 2) cloudflared — connector token comes from the RunPod Secret (env TUNNEL_TOKEN); never logged
if [[ -n "${TUNNEL_TOKEN:-}" ]]; then
  ( while true; do cloudflared --no-autoupdate tunnel run >>/workspace/.rmq/cloudflared.log 2>&1 || true; sleep 5; done ) &
fi
unset TUNNEL_TOKEN

# 3) agent (loopback :9100); Remiqora is started by the agent after bootstrap
exec python3 -I /opt/rmq/agent.py

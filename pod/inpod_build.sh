#!/usr/bin/env bash
# One-time in-pod build of the Remiqora runtime (used until the prebuilt image is published).
# Runs on a stock nvidia/cuda:12.8.1-devel-ubuntu24.04 pod. Mirrors docker/Dockerfile step for step.
# Order matters: the deadline guard is armed within the first minute, BEFORE the long compile.
#   pod cmd:  bash -c 'apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null &&
#                      git clone -q https://github.com/n-regev/remiqora-runtime /opt/rmq-src &&
#                      git -C /opt/rmq-src checkout -q <PINNED_SHA> && exec bash /opt/rmq-src/pod/inpod_build.sh'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
: "${RMQ_DEADLINE:?RMQ_DEADLINE is required}"
SRC=/opt/rmq-src
REMIQORA_REF="${REMIQORA_REF:-7061167ba5f119d0720ca11a632fca74d17d1b67}"
LOG=/workspace/.rmq/inpod-build.log
mkdir -p /workspace/.rmq /dev/shm/rmq && chmod 700 /workspace/.rmq
exec > >(tee -a "$LOG") 2>&1
t0=$(date +%s); step() { echo "[inpod-build +$(( $(date +%s) - t0 ))s] $*"; }

step "1/7 python3 + guard (armed before anything slow)"
apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-venv curl >/dev/null
mkdir -p /opt/rmq && cp -r "$SRC/pod/." /opt/rmq/ && cp "$SRC/config/models.lock.json" /opt/rmq/models.lock.json
chmod 0755 /opt/rmq/*.sh /opt/rmq/*.py
export RMQ_RUN_DIR=/dev/shm/rmq RMQ_INPOD_BUILD=1
export REMIQORA_HOST=127.0.0.1 REMIQORA_PORT=9000 REMIQORA_DATA_DIR=/workspace/remiqora-data REMIQORA_LOG_DIR=/workspace/remiqora-logs RMQ_HOME=/opt/rmq
mkdir -p "$REMIQORA_DATA_DIR" /workspace/.rmq-scratch && chmod 750 /workspace/.rmq-scratch
( while true; do python3 -I /opt/rmq/guard.py || true; sleep 5; done ) &

step "2/7 system packages"
for i in 1 2 3; do apt-get install -y -qq -o Acquire::Retries=5 --no-install-recommends \
  ca-certificates git unzip xz-utils jq sqlite3 tini python3-dev build-essential cmake pkg-config ffmpeg libsndfile1 util-linux >/dev/null && break || sleep 15; done
command -v ffmpeg tini prlimit setpriv >/dev/null

step "3/7 pinned tools (checksums verified)"
cd /tmp
curl -fsSLo node.tar.xz https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-x64.tar.xz
echo "1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce  node.tar.xz" | sha256sum -c - >/dev/null
mkdir -p /opt/node && tar -xJf node.tar.xz -C /opt/node --strip-components=1 && rm node.tar.xz
curl -fsSLo uv.tgz https://github.com/astral-sh/uv/releases/download/0.13.7/uv-x86_64-unknown-linux-gnu.tar.gz
echo "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83  uv.tgz" | sha256sum -c - >/dev/null
tar -xzf uv.tgz && install -m 0755 uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/ && rm -rf uv.tgz uv-x86_64-unknown-linux-gnu
curl -fsSLo rclone.zip https://github.com/rclone/rclone/releases/download/v1.76.0/rclone-v1.76.0-linux-amd64.zip
echo "b00f00b3266c3764413afe599d3ec316f6f43b3e0ea8a0bc1d186d2133e46df7  rclone.zip" | sha256sum -c - >/dev/null
unzip -q rclone.zip && install -m 0755 rclone-v1.76.0-linux-amd64/rclone /usr/local/bin/rclone && rm -rf rclone.zip rclone-v*
curl -fsSLo /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64
echo "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872  /usr/local/bin/cloudflared" | sha256sum -c - >/dev/null
chmod 0755 /usr/local/bin/cloudflared
export PATH=/opt/node/bin:/usr/local/bin:/usr/local/cuda/bin:$PATH UV_CACHE_DIR=/tmp/uv-cache UV_PYTHON_INSTALL_DIR=/opt/uv-python

step "4/7 tunnel up early (Access-protected; the studio path answers only once Remiqora runs)"
if [[ -n "${TUNNEL_TOKEN:-}" ]]; then
  ( while true; do cloudflared --no-autoupdate tunnel run >>/workspace/.rmq/cloudflared.log 2>&1 || true; sleep 5; done ) &
fi
# the agent answers /_rmq/health immediately (phase=awaiting_bootstrap) so n8n can see the pod; bootstrap waits for step 7
unset_tunnel_env() { env -u TUNNEL_TOKEN "$@"; }
( cd /opt/rmq && unset_tunnel_env python3 -I /opt/rmq/agent.py ) &
AGENT_PID=$!

step "5/7 Remiqora $REMIQORA_REF + upstream setup (audio.cpp built only for this GPU)"
ARCH=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
git clone -q --filter=blob:none https://github.com/inikolax/remiqora.git /opt/remiqora
git -C /opt/remiqora checkout -q --detach "$REMIQORA_REF" && git -C /opt/remiqora log -1 --format='%H %cI' > /opt/remiqora/.pinned-commit
cd /opt/remiqora
sed -i 's|--target audiocpp_server|--target audiocpp_server --cuda-arch "${RMQ_CUDA_ARCH}"|' setup_linux.sh
grep -q -- '--cuda-arch "${RMQ_CUDA_ARCH}"' setup_linux.sh
export RMQ_CUDA_ARCH="$ARCH" SKIP_WEIGHTS=1 SKIP_COVERS=1 STABLE_AUDIO_MODELS= PROMPT_ASSISTANT_MODEL= ACE_BASE=0 REMIQORA_LANG=en
export PYTHON_BIN=python3 NVCC_BIN=/usr/local/cuda/bin/nvcc CUDA_TOOLKIT_PREFIX=/usr/local/cuda
bash ./setup_linux.sh </dev/null || { tail -n 80 backend/logs/setup/*.log 2>/dev/null; exit 1; }
test -x external/audio.cpp/build/linux-cuda-release/bin/audiocpp_server
rm -rf external/sa3-server/.venv external/prompt-assistant/.venv /tmp/uv-cache

step "6/7 backend venv + frontend build"
python3 -m venv backend/.venv && backend/.venv/bin/python3 -m pip install -q --upgrade pip && backend/.venv/bin/pip install -q -r backend/requirements.txt
(cd frontend && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null && rm -rf node_modules)
id rmqingest >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin rmqingest
touch /dev/shm/rmq/build-complete

step "7/7 runtime ready (build took $(( ($(date +%s) - t0) / 60 )) min); agent continues with bootstrap"
wait "$AGENT_PID"

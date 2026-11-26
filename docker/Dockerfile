# Remiqora studio runtime image (plan decision 5, R9).
# Contains: upstream Remiqora at a pinned commit, ACE-Step + audio.cpp built by the upstream setup script
# (weights skipped), Demucs env, our pod runtime (guard/agent/backup/ingest), ffmpeg, rclone, cloudflared, tini.
# Contains NO weights, music, secrets, hostnames or account IDs — all injected at runtime.
#
# Single stage (CUDA devel) for the POC: the upstream backend config expects nvcc/CUDA dirs at runtime.
# Slimming to a runtime base is a Phase 3 optimisation once measured (R3).
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66

ARG REMIQORA_REPO=https://github.com/inikolax/remiqora.git
ARG REMIQORA_REF=7061167ba5f119d0720ca11a632fca74d17d1b67
ARG NODE_VERSION=22.23.4
ARG NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
ARG UV_VERSION=0.13.7
ARG UV_SHA256=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83
ARG RCLONE_VERSION=1.76.0
ARG RCLONE_SHA256=b00f00b3266c3764413afe599d3ec316f6f43b3e0ea8a0bc1d186d2133e46df7
ARG CLOUDFLARED_VERSION=2026.10.7
ARG CLOUDFLARED_SHA256=1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872
# Only the GPU generations RunPod offers us: sm_86 (A5000/3090/A40/A6000), sm_89 (4090/L4/L40S), sm_120 (RTX PRO 4500/5090).
# Upstream setup builds audio.cpp for a "portable default list" of many architectures, which is very slow on CI.
ARG RMQ_CUDA_ARCH="86;89;120"

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    UV_CACHE_DIR=/tmp/uv-cache UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH=/opt/node/bin:/usr/local/bin:/usr/local/cuda/bin:$PATH

RUN for i in 1 2 3; do apt-get update -o Acquire::Retries=5 && apt-get install -y -o Acquire::Retries=5 --no-install-recommends \
      ca-certificates curl git unzip xz-utils jq sqlite3 tini \
      python3 python3-venv python3-dev build-essential cmake pkg-config \
      ffmpeg libsndfile1 util-linux \
      && break || { echo "apt attempt $i failed; retrying"; sleep 20; }; done \
    && command -v ffmpeg tini prlimit setpriv && rm -rf /var/lib/apt/lists/*

# Pinned tools with checksum verification
RUN set -eux; cd /tmp; \
    curl -fsSLo node.tar.xz https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz; \
    echo "${NODE_SHA256}  node.tar.xz" | sha256sum -c -; mkdir -p /opt/node; tar -xJf node.tar.xz -C /opt/node --strip-components=1; rm node.tar.xz; \
    curl -fsSLo uv.tgz https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz; \
    echo "${UV_SHA256}  uv.tgz" | sha256sum -c -; tar -xzf uv.tgz; install -m 0755 uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/; rm -rf uv.tgz uv-x86_64-unknown-linux-gnu; \
    curl -fsSLo rclone.zip https://github.com/rclone/rclone/releases/download/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip; \
    echo "${RCLONE_SHA256}  rclone.zip" | sha256sum -c -; unzip -q rclone.zip; install -m 0755 rclone-v${RCLONE_VERSION}-linux-amd64/rclone /usr/local/bin/rclone; rm -rf rclone.zip rclone-v*; \
    curl -fsSLo /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64; \
    echo "${CLOUDFLARED_SHA256}  /usr/local/bin/cloudflared" | sha256sum -c -; chmod 0755 /usr/local/bin/cloudflared; \
    node --version; uv --version; rclone version | head -1; cloudflared --version

# Upstream Remiqora at the pinned commit (fixed absolute path: the app DB stores absolute paths)
RUN git clone --filter=blob:none ${REMIQORA_REPO} /opt/remiqora && cd /opt/remiqora && git checkout --detach ${REMIQORA_REF} \
    && git log -1 --format='%H %cI' > /opt/remiqora/.pinned-commit

WORKDIR /opt/remiqora
# Upstream setup, unmodified, with weights and the cover engine skipped. Stable Audio 3 and the prompt
# assistant environments (not needed, plan §Weights) are removed in the same layer to keep the image lean.
RUN set -eux; \
    # minimal pinned patch (documented in docs/image-review.md): pass our CUDA arch list to audio.cpp's build script
    sed -i 's|--target audiocpp_server|--target audiocpp_server --cuda-arch "${RMQ_CUDA_ARCH}"|' setup_linux.sh; \
    grep -q -- '--cuda-arch "${RMQ_CUDA_ARCH}"' setup_linux.sh; \
    export RMQ_CUDA_ARCH="${RMQ_CUDA_ARCH}"; \
    export SKIP_WEIGHTS=1 SKIP_COVERS=1 STABLE_AUDIO_MODELS= PROMPT_ASSISTANT_MODEL= ACE_BASE=0 REMIQORA_LANG=en; \
    export PYTHON_BIN=python3 NVCC_BIN=/usr/local/cuda/bin/nvcc CUDA_TOOLKIT_PREFIX=/usr/local/cuda; \
    bash ./setup_linux.sh </dev/null || { tail -n 80 backend/logs/setup/*.log 2>/dev/null; exit 1; }; \
    test -x external/audio.cpp/build/linux-cuda-release/bin/audiocpp_server; \
    rm -rf external/sa3-server/.venv external/prompt-assistant/.venv /tmp/uv-cache /root/.cache/pip; \
    find external -name '*.o' -delete

# Backend venv + production frontend build ONCE (prod_run_linux.sh would rebuild on every start)
RUN set -eux; \
    python3 -m venv backend/.venv && backend/.venv/bin/python3 -m pip install -q --upgrade pip \
    && backend/.venv/bin/pip install -q -r backend/requirements.txt && rm -rf /root/.cache/pip; \
    cd frontend && npm ci --no-audit --no-fund && npm run build && rm -rf node_modules /root/.npm

# Our pod runtime
COPY pod/ /opt/rmq/
COPY config/models.lock.json /opt/rmq/models.lock.json
RUN chmod 0755 /opt/rmq/*.sh /opt/rmq/*.py && useradd --system --no-create-home --shell /usr/sbin/nologin rmqingest \
    && python3 -I -m py_compile /opt/rmq/*.py /opt/rmq/ingest/*.py

ENV REMIQORA_HOST=127.0.0.1 REMIQORA_PORT=9000 \
    REMIQORA_DATA_DIR=/workspace/remiqora-data REMIQORA_LOG_DIR=/workspace/remiqora-logs \
    RMQ_HOME=/opt/rmq

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/rmq/entrypoint.sh"]

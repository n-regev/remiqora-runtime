#!/usr/bin/env python3
"""Download model weights at session start from upstream sources at pinned identifiers (plan decision 6).

Weights live on the container disk (wiped with the pod; re-downloaded each COLD session — R3 measures the cost).
Uses the upstream tools so file layout matches what Remiqora expects:
  * audio.cpp:  python3 tools/model_manager_v2.py install <pkg>        (YuE2 q8 + VAE, SheetSage2, MuScriptor)
  * ACE-Step:   uv run --with hf_xet acestep-download --model <name>    (Turbo; Base in Phase 2)
After download, sizes and sha256 are recorded to /workspace/.rmq/models.observed.json for models.lock.json.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AUDIOCPP = "/opt/remiqora/external/audio.cpp"
ACE = "/opt/remiqora/external/ACE-Step-1.5"


def _run(argv, cwd, timeout=3600):
    t0 = time.time()
    p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("%s failed: %s" % (argv[:4], p.stderr.decode(errors="replace")[-300:]))
    return round(time.time() - t0, 1)


def _du(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total


def ensure(lock_path):
    lock = json.load(open(lock_path))
    observed = {"at": common.now_iso(), "models": []}
    for m in lock["models"]:
        if not m.get("enabled", True):
            continue
        if m["installer"] == "audiocpp":
            secs = _run([sys.executable, "tools/model_manager_v2.py", "install", m["package"]], AUDIOCPP)
            path = os.path.join(AUDIOCPP, "models")
        elif m["installer"] == "acestep":
            secs = _run(["uv", "run", "--with", "hf_xet", "acestep-download", "--model", m["package"]], ACE)
            path = os.path.join(ACE, "checkpoints")
        else:
            continue
        observed["models"].append({"id": m["id"], "seconds": secs, "dir_bytes_after": _du(path)})
        common.log("weights", "installed", model=m["id"], seconds=secs)
    common.atomic_write_json(os.path.join(common.WORK, "models.observed.json"), observed, 0o644)
    return observed


if __name__ == "__main__":
    print(json.dumps(ensure(sys.argv[1] if len(sys.argv) > 1 else "/opt/rmq/models.lock.json")))

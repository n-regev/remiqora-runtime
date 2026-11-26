"""Shared helpers for the Remiqora pod runtime (stdlib only, Python >= 3.9)."""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time

RUN = os.environ.get("RMQ_RUN_DIR", "/dev/shm/rmq")          # tmpfs: state + secrets, wiped with the container
SECRETS = os.path.join(RUN, "secrets")
DATA_DIR = os.environ.get("REMIQORA_DATA_DIR", "/workspace/remiqora-data")
WORK = os.environ.get("RMQ_WORK_DIR", "/workspace/.rmq")       # on the volume disk (same FS as DATA_DIR → hardlinks)
RCLONE_CONF = os.path.join(SECRETS, "rclone.conf")
REMOTE = os.environ.get("RMQ_REMOTE", "rmq:")  # rclone root_folder_id = app-owned "Remiqora Studio" folder
REMIQORA_URL = "http://127.0.0.1:%s" % os.environ.get("REMIQORA_PORT", "9000")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dirs():
    for d, mode in ((RUN, 0o755), (SECRETS, 0o700), (WORK, 0o700)):
        os.makedirs(d, mode=mode, exist_ok=True)
        os.chmod(d, mode)


def atomic_write_json(path, obj, mode=0o644):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def log(component, msg, **kv):
    rec = {"t": now_iso(), "c": component, "msg": msg}
    rec.update({k: v for k, v in kv.items() if not any(s in k.lower() for s in ("token", "secret", "password", "auth"))})
    sys.stderr.write(json.dumps(rec) + "\n")
    sys.stderr.flush()


def file_hashes(path, bufsize=1 << 20):
    sha, md5, n = hashlib.sha256(), hashlib.md5(), 0
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            sha.update(b)
            md5.update(b)
            n += len(b)
    return sha.hexdigest(), md5.hexdigest(), n


def rclone(args, timeout=3600, check=True, input_bytes=None):
    """Run rclone with the tmpfs config; fixed argv, never a shell."""
    if not os.path.exists(RCLONE_CONF):
        raise RuntimeError("rclone not configured (no bootstrap yet)")
    cmd = ["rclone", "--config", RCLONE_CONF, "--retries", "3", "--low-level-retries", "10", "--stats", "0"] + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes)
    if check and p.returncode != 0:
        raise RuntimeError("rclone %s failed rc=%s: %s" % (args[0], p.returncode, p.stderr.decode(errors="replace")[-400:]))
    return p


class FileLock:
    """Exclusive inter-process lock (backup must never run twice concurrently: agent periodic vs guard final)."""

    def __init__(self, name, blocking=True):
        self.path = os.path.join(RUN, name + ".lock")
        self.blocking = blocking
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(self.fd, flags)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)

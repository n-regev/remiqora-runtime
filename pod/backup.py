#!/usr/bin/env python3
"""Incremental, content-addressed, verified backups of REMIQORA_DATA_DIR to Google Drive (plan R1).

Drive layout (under rmq:Remiqora/archive):
  objects/<aa>/<sha256>                    immutable file content (uploaded once)
  generations/<gen>/manifest.json          file list {path, sha256, md5, size} + db info + app revision
  generations/<gen>/COMPLETE               written LAST; contains sha256 of manifest.json

  python3 -I backup.py backup [--final]    periodic (online SQLite backup API) or final (caller quiesced writers)
  python3 -I backup.py restore             restore latest COMPLETE generation into an EMPTY data dir
  python3 -I backup.py status

Never deletes or overwrites a generation. Never uses destructive sync.
"""
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

ARCHIVE = common.REMOTE + "archive"
DB_NAME = "aicollector.db"
EXCLUDE_NAMES = {DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm"}
EXCLUDE_DIRS = {"logs", "tmp", "temp", "__pycache__"}
STATUS = os.path.join(common.RUN, "backup.json")
INDEX = os.path.join(common.WORK, "hash-index.json")
REMOTE_OBJECTS_CACHE = os.path.join(common.WORK, "remote-objects.json")


def _walk(data_dir):
    for root, dirs, files in os.walk(data_dir):
        rel_root = os.path.relpath(root, data_dir)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".rmq")]
        for name in files:
            if rel_root == "." and name in EXCLUDE_NAMES:
                continue
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue  # never follow symlinks
            yield os.path.normpath(os.path.join(rel_root, name)), full


def _snapshot_db(data_dir, dest):
    src_path = os.path.join(data_dir, DB_NAME)
    if not os.path.exists(src_path):
        return None
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True, timeout=30)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    src.close()
    dst.close()
    if integrity != "ok":
        raise RuntimeError("SQLite integrity_check failed on snapshot")
    return integrity


def _remote_objects(refresh=False):
    cache = common.read_json(REMOTE_OBJECTS_CACHE)
    if cache is not None and not refresh:
        return set(cache)
    p = common.rclone(["lsf", "-R", "--files-only", ARCHIVE + "/objects"], check=False)
    names = set(os.path.basename(x) for x in p.stdout.decode().split("\n") if x.strip()) if p.returncode == 0 else set()
    common.atomic_write_json(REMOTE_OBJECTS_CACHE, sorted(names), 0o600)
    return names


def backup(final=False, data_dir=common.DATA_DIR):
    common.ensure_dirs()
    t0 = time.time()
    import datetime
    gen = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(3)  # sortable to µs
    staging = os.path.join(common.WORK, "staging-" + gen)
    objdir = os.path.join(staging, "objects")
    os.makedirs(objdir, mode=0o700)
    try:
        index = common.read_json(INDEX, {})
        files = []
        db_snap = os.path.join(staging, DB_NAME)
        integrity = _snapshot_db(data_dir, db_snap)
        entries = list(_walk(data_dir))
        if integrity:
            entries.append((DB_NAME, db_snap))
        for rel, full in entries:
            st = os.stat(full)
            key = "%s|%d|%d" % (rel, st.st_size, st.st_mtime_ns)
            cached = index.get(rel)
            if cached and cached.get("key") == key and rel != DB_NAME:
                sha, md5, size = cached["sha256"], cached["md5"], cached["size"]
            else:
                sha, md5, size = common.file_hashes(full)
                index[rel] = {"key": key, "sha256": sha, "md5": md5, "size": size}
            files.append({"path": rel, "sha256": sha, "md5": md5, "size": size})
        remote = _remote_objects()
        missing = {}
        for f, (rel, full) in zip(files, entries):
            if f["sha256"] not in remote and f["sha256"] not in missing:
                d = os.path.join(objdir, f["sha256"][:2])
                os.makedirs(d, exist_ok=True)
                target = os.path.join(d, f["sha256"])
                try:
                    os.link(full, target)  # same filesystem → no copy
                except OSError:
                    shutil.copy2(full, target)
                missing[f["sha256"]] = f
        uploaded_bytes = sum(f["size"] for f in missing.values())
        if missing:
            listfile = os.path.join(staging, "upload.list")
            with open(listfile, "w") as fh:
                fh.write("\n".join("%s/%s" % (s[:2], s) for s in missing))
            common.rclone(["copy", "--files-from", listfile, "--no-traverse", objdir, ARCHIVE + "/objects"], timeout=6 * 3600)
            # verify: Drive's server-side md5 must match the local md5 for every uploaded object
            p = common.rclone(["md5sum", "--files-from", listfile, ARCHIVE + "/objects"])
            got = {}
            for line in p.stdout.decode().splitlines():
                parts = line.split("  ", 1)
                if len(parts) == 2:
                    got[os.path.basename(parts[1].strip())] = parts[0].strip()
            bad = [s for s, f in missing.items() if got.get(s) != f["md5"]]
            if bad:
                raise RuntimeError("md5 verification failed for %d object(s)" % len(bad))
            remote |= set(missing)
            common.atomic_write_json(REMOTE_OBJECTS_CACHE, sorted(remote), 0o600)
        manifest = {
            "generation": gen, "created_utc": common.now_iso(), "final": bool(final),
            "app_revision": (open("/opt/remiqora/.pinned-commit").read().strip() if os.path.exists("/opt/remiqora/.pinned-commit") else None),
            "data_dir": data_dir, "db": {"name": DB_NAME, "integrity_check": integrity, "present": bool(integrity)},
            "files": files, "total_bytes": sum(f["size"] for f in files), "uploaded_bytes": uploaded_bytes,
            "complete": True, "consistency": "quiesced" if final else "online-sqlite-backup (media may lag DB by seconds)",
        }
        mbytes = json.dumps(manifest, sort_keys=True).encode()
        import hashlib
        msha = hashlib.sha256(mbytes).hexdigest()
        common.rclone(["rcat", "%s/generations/%s/manifest.json" % (ARCHIVE, gen)], input_bytes=mbytes)
        back = common.rclone(["cat", "%s/generations/%s/manifest.json" % (ARCHIVE, gen)]).stdout
        if hashlib.sha256(back).hexdigest() != msha:
            raise RuntimeError("manifest read-back mismatch")
        common.rclone(["rcat", "%s/generations/%s/COMPLETE" % (ARCHIVE, gen)], input_bytes=msha.encode())
        common.atomic_write_json(INDEX, index, 0o600)
        status = {"lastCompleteGen": gen, "lastCompleteAt": common.now_iso(), "final": bool(final),
                  "files": len(files), "uploadedBytes": uploaded_bytes, "seconds": round(time.time() - t0, 1)}
        common.atomic_write_json(STATUS, dict(common.read_json(STATUS, {}), **status))
        common.log("backup", "generation complete", **status)
        return status
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def latest_complete():
    p = common.rclone(["lsf", "--dirs-only", ARCHIVE + "/generations"], check=False)
    if p.returncode != 0:
        return None
    for gen in sorted((g.strip("/") for g in p.stdout.decode().split() if g.strip()), reverse=True):
        q = common.rclone(["cat", "%s/generations/%s/COMPLETE" % (ARCHIVE, gen)], check=False)
        if q.returncode == 0 and q.stdout.strip():
            return gen, q.stdout.decode().strip()
    return None


def restore(data_dir=common.DATA_DIR):
    import hashlib
    common.ensure_dirs()
    found = latest_complete()
    if not found:
        os.makedirs(data_dir, exist_ok=True)
        common.log("restore", "no COMPLETE generation — fresh studio")
        return {"restored": None}
    gen, msha = found
    mbytes = common.rclone(["cat", "%s/generations/%s/manifest.json" % (ARCHIVE, gen)]).stdout
    if hashlib.sha256(mbytes).hexdigest() != msha:
        raise RuntimeError("manifest hash does not match COMPLETE marker")
    manifest = json.loads(mbytes)
    if manifest["data_dir"] != data_dir:
        raise RuntimeError("data dir changed (%s → %s): absolute paths in the DB would break" % (manifest["data_dir"], data_dir))
    if os.path.exists(os.path.join(data_dir, DB_NAME)):
        raise RuntimeError("refusing to restore over an existing database")
    staging = os.path.join(common.WORK, "restore-" + gen)
    os.makedirs(staging, mode=0o700, exist_ok=True)
    try:
        shas = sorted({f["sha256"] for f in manifest["files"]})
        listfile = os.path.join(staging, "download.list")
        with open(listfile, "w") as fh:
            fh.write("\n".join("%s/%s" % (s[:2], s) for s in shas))
        common.rclone(["copy", "--files-from", listfile, "--no-traverse", ARCHIVE + "/objects", os.path.join(staging, "objects")], timeout=6 * 3600)
        for f in manifest["files"]:
            src = os.path.join(staging, "objects", f["sha256"][:2], f["sha256"])
            sha, _, size = common.file_hashes(src)
            if sha != f["sha256"] or size != f["size"]:
                raise RuntimeError("object verification failed for %s" % f["path"])
            rel = os.path.normpath(f["path"])
            if rel.startswith("..") or os.path.isabs(rel):
                raise RuntimeError("unsafe path in manifest")
            dest = os.path.join(data_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
        db = os.path.join(data_dir, DB_NAME)
        if os.path.exists(db):
            c = sqlite3.connect(db)
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.close()
            if ok != "ok":
                raise RuntimeError("restored DB failed integrity_check")
        common.atomic_write_json(STATUS, {"lastCompleteGen": gen, "lastCompleteAt": manifest["created_utc"], "restoredFrom": gen})
        common.log("restore", "restored generation", gen=gen, files=len(manifest["files"]))
        return {"restored": gen, "files": len(manifest["files"])}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "backup":
        with common.FileLock("backup"):
            print(json.dumps(backup(final="--final" in sys.argv)))
    elif cmd == "restore":
        print(json.dumps(restore()))
    else:
        print(json.dumps(common.read_json(STATUS, {})))

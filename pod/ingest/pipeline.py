"""RMQ-13 pod-side ingestion pipeline: retrieve → validate → (convert) → hash → Drive library → Remiqora upload.

Untrusted steps (network fetch, ffprobe, ffmpeg) run as user `rmqingest` in a private scratch dir with
resource limits; trusted steps (rclone, Remiqora API) run in the agent. No shell is ever used.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH_ROOT = os.environ.get("RMQ_SCRATCH", "/workspace/.rmq-scratch")
LIBRARY = common.REMOTE + "source-library"
MAX_DURATION_S = int(os.environ.get("RMQ_INGEST_MAX_DURATION_S", "900"))
MAX_OUT_BYTES = 250 * 1024 * 1024
NATIVE = {"wav": "wav", "mp3": "mp3", "flac": "flac"}  # formats Remiqora's upload API accepts


class IngestError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _as_ingest_user(argv, timeout, cpu_s=600, mem_bytes=1536 * 1024 * 1024, fsize=MAX_OUT_BYTES + 1):
    """Run argv as rmqingest with rlimits (setpriv + prlimit from util-linux)."""
    cmd = ["prlimit", "--cpu=%d" % cpu_s, "--as=%d" % mem_bytes, "--fsize=%d" % fsize, "--nproc=64", "--",
           "setpriv", "--reuid=rmqingest", "--regid=rmqingest", "--clear-groups", "--no-new-privs", "--"] + argv
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def safe_title(s):
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(s or ""))[:200].strip()
    return s or "Imported reference"


def probe(path):
    p = _as_ingest_user(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
                         "-show_format", "-show_streams", path], timeout=120)
    if p.returncode != 0:
        raise IngestError("INVALID_AUDIO", "ffprobe could not read the file")
    info = json.loads(p.stdout or b"{}")
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise IngestError("INVALID_AUDIO", "no audio stream")
    dur = float(info.get("format", {}).get("duration") or audio[0].get("duration") or 0)
    if dur <= 0 or dur > MAX_DURATION_S:
        raise IngestError("INVALID_AUDIO", "duration %.0fs outside 1..%ds" % (dur, MAX_DURATION_S))
    fmt = info.get("format", {}).get("format_name", "")
    return {"duration_s": round(dur, 2), "codec": audio[0].get("codec_name"), "format": fmt,
            "sample_rate": audio[0].get("sample_rate"), "channels": audio[0].get("channels"),
            "has_video": any(s.get("codec_type") == "video" for s in info.get("streams", []))}


def native_ext(meta):
    for key, ext in (("wav", "wav"), ("mp3", "mp3"), ("flac", "flac")):
        if key in meta["format"].split(",") and not meta["has_video"]:
            return ext
    return None


def convert_to_flac(src, dst):
    p = _as_ingest_user(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file", "-i", src,
                         "-vn", "-map", "0:a:0", "-c:a", "flac", "-compression_level", "5", dst], timeout=900)
    if p.returncode != 0 or not os.path.exists(dst):
        raise IngestError("CONVERT_FAILED", "ffmpeg failed")
    if os.path.getsize(dst) > MAX_OUT_BYTES:
        raise IngestError("TOO_LARGE", "converted output over cap")


def multipart_upload(path, filename, title):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        audio = f.read()
    ctype = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac"}[filename.rsplit(".", 1)[1]]
    parts = [
        b"--" + boundary.encode(), b'Content-Disposition: form-data; name="title"', b"", title.encode("utf-8"),
        b"--" + boundary.encode(), ('Content-Disposition: form-data; name="audio"; filename="%s"' % filename).encode(),
        ("Content-Type: %s" % ctype).encode(), b"", audio, b"--" + boundary.encode() + b"--", b"",
    ]
    body = b"\r\n".join(parts)
    req = urllib.request.Request(common.REMIQORA_URL + "/api/tracks/upload", data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def find_existing_track(sha256):
    """Idempotency: our uploads are titled with a short content tag; reconcile before re-uploading."""
    tag = "[src:%s]" % sha256[:12]
    with urllib.request.urlopen(common.REMIQORA_URL + "/api/tracks?model=upload", timeout=60) as r:
        rows = json.loads(r.read()).get("data", [])
    for t in rows:
        if tag in (t.get("title") or "") and not t.get("deleted_at"):
            return t
    return None


def run(job, allow_hosts):
    """job = {jobId, source:{type:'url'|'drive', url?, driveId?, title?, sourceKey}}; returns result dict."""
    job_id = re.sub(r"[^A-Za-z0-9_-]", "", str(job["jobId"]))[:64]
    scratch = os.path.join(SCRATCH_ROOT, job_id)
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, mode=0o770)
    shutil.chown(scratch, user="root", group="rmqingest")
    raw = os.path.join(scratch, "input.bin")
    src = job["source"]
    try:
        if src["type"] == "url":
            p = _as_ingest_user([sys.executable, "-I", os.path.join(HERE, "netfetch.py"), "--url", src["url"], "--out", raw]
                                + sum([["--allow-host", h] for h in allow_hosts], []), timeout=660, fsize=100 * 1024 * 1024 + 1)
            if p.returncode != 0:
                err = json.loads((p.stderr.decode(errors="replace").strip().splitlines() or ['{}'])[-1] or "{}")
                raise IngestError(err.get("error", "SOURCE_BLOCKED"), err.get("detail", "fetch failed"))
        elif src["type"] == "drive":
            drive_id = re.sub(r"[^A-Za-z0-9_-]", "", src["driveId"])
            common.rclone(["backend", "copyid", "rmq:", drive_id, raw + ".dl"], timeout=900)
            os.replace(raw + ".dl", raw)
            os.chmod(raw, 0o640)
            shutil.chown(raw, group="rmqingest")
            if os.path.getsize(raw) > 100 * 1024 * 1024:
                raise IngestError("TOO_LARGE", "Drive file over cap")
        else:
            raise IngestError("UNSUPPORTED_SOURCE", "type %s is not retrievable (REFERENCE_ONLY)" % src.get("type"))

        meta = probe(raw)
        ext = native_ext(meta)
        final = os.path.join(scratch, "audio." + (ext or "flac"))
        if ext:
            os.replace(raw, final)
        else:
            convert_to_flac(raw, final)
        sha, md5, size = common.file_hashes(final)
        fname = "%s.%s" % (sha, final.rsplit(".", 1)[1])

        # Drive library: content-addressed, immutable; verify by md5 after upload
        obj = "%s/objects/sha256/%s" % (LIBRARY, fname)
        if common.rclone(["lsf", obj], check=False).returncode != 0 or not common.rclone(["lsf", obj], check=False).stdout.strip():
            common.rclone(["copyto", final, obj], timeout=1800)
        remote_md5 = common.rclone(["md5sum", obj]).stdout.decode().split(" ")[0].strip()
        if remote_md5 != md5:
            raise IngestError("DRIVE_VERIFY_FAILED", "remote md5 mismatch")
        manifest = {"sourceKey": src.get("sourceKey"), "jobId": job_id, "sha256": sha, "md5": md5, "bytes": size,
                    "object": "objects/sha256/" + fname, "probe": meta, "converted": not bool(ext), "importedAt": common.now_iso()}
        common.rclone(["rcat", "%s/manifests/%s.json" % (LIBRARY, sha)], input_bytes=json.dumps(manifest).encode())

        # Remiqora native library (reconcile first: no duplicate tracks on retry)
        title = "%s [src:%s]" % (safe_title(src.get("title")), sha[:12])
        track = find_existing_track(sha) or multipart_upload(final, fname, title)
        if not track.get("id"):
            raise IngestError("IMPORT_FAILED", "Remiqora returned no track id")
        return {"ok": True, "trackId": track["id"], "sha256": sha, "bytes": size, "driveObject": manifest["object"], "probe": meta}
    except IngestError as e:
        return {"ok": False, "error": e.code, "detail": e.detail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "TIMEOUT", "detail": "processing deadline exceeded"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

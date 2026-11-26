#!/usr/bin/env python3
"""Remiqora pod agent — loopback :9100, reached only via cloudflared path ^/_rmq/ behind Cloudflare Access
(service-token policy, JWT validated by cloudflared at origin). Additionally every request except
GET /_rmq/health must carry X-Rmq-Nonce whose sha256 equals RMQ_SESSION_NONCE_SHA256 (per-session fencing).

Endpoints
  GET  /_rmq/health        phase, backups, guard status, job activity (no secrets)
  POST /_rmq/bootstrap     one-time: rclone token (headers) → restore → weights → start Remiqora
  POST /_rmq/extend        {deadline, hardDeadline}  (n8n already reserved budget)
  POST /_rmq/drain         {reason, finalBackupAt}
  POST /_rmq/backup        trigger a periodic backup now
  POST /_rmq/ingest        {jobId, source}  → background job (RMQ-13)
  GET  /_rmq/jobs/<id>     job status/result
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backup  # noqa: E402
import common  # noqa: E402
import weights  # noqa: E402
from ingest import pipeline  # noqa: E402

STATE = {"phase": "awaiting_bootstrap", "bootstrappedAt": None, "readyAt": None, "error": None, "timings": {}}
JOBS = {}
LOCK = threading.Lock()
NONCE_SHA = os.environ.get("RMQ_SESSION_NONCE_SHA256", "")
BACKUP_INTERVAL_S = int(os.environ.get("RMQ_BACKUP_INTERVAL_S", "900"))
MAX_BODY = 64 * 1024


def set_phase(p, **kv):
    with LOCK:
        STATE["phase"] = p
        STATE["timings"][p] = common.now_iso()
        STATE.update(kv)
    common.log("agent", "phase", phase=p)


def remiqora_env():
    env = dict(os.environ)
    for k in ("RUNPOD_API_KEY", "TUNNEL_TOKEN"):
        env.pop(k, None)  # the app and its engines never see provider/tunnel credentials
    env.update({"REMIQORA_HOST": "127.0.0.1", "REMIQORA_PORT": os.environ.get("REMIQORA_PORT", "9000")})
    return env


def start_remiqora():
    os.makedirs(os.environ.get("REMIQORA_LOG_DIR", "/workspace/remiqora-logs"), exist_ok=True)
    logf = open(os.path.join(os.environ.get("REMIQORA_LOG_DIR", "/workspace/remiqora-logs"), "server.log"), "ab")
    p = subprocess.Popen(["/opt/remiqora/backend/.venv/bin/python3", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                          "--port", os.environ.get("REMIQORA_PORT", "9000"), "--no-access-log", "--log-level", "warning"],
                         cwd="/opt/remiqora/backend", env=remiqora_env(), stdout=logf, stderr=logf, start_new_session=True)
    with open(os.path.join(common.RUN, "remiqora.pid"), "w") as f:
        f.write(str(p.pid))  # pgid == pid (new session)
    for _ in range(360):
        try:
            with urllib.request.urlopen(common.REMIQORA_URL + "/api/orchestrator/status", timeout=3):
                return p
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("Remiqora exited during startup (see remiqora-logs/server.log)")
            time.sleep(1)
    raise RuntimeError("Remiqora did not answer within 6 minutes")


def bootstrap_worker():
    try:
        set_phase("restoring")
        res = backup.restore()
        if os.environ.get("RMQ_INPOD_BUILD") == "1" and not os.path.exists(os.path.join(common.RUN, "build-complete")):
            set_phase("building_runtime", restoredFrom=res.get("restored"))
            while not os.path.exists(os.path.join(common.RUN, "build-complete")):
                time.sleep(10)
        set_phase("weights", restoredFrom=res.get("restored"))
        weights.ensure(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.lock.json"))
        set_phase("starting_app")
        start_remiqora()
        weights.request_parts(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.lock.json"), common.REMIQORA_URL)
        set_phase("ready", readyAt=common.now_iso())
        threading.Thread(target=periodic_backups, daemon=True).start()
    except Exception as e:
        set_phase("error", error=str(e)[:300])


def periodic_backups():
    while True:
        time.sleep(BACKUP_INTERVAL_S)
        if os.path.exists(os.path.join(common.RUN, "quiesced")):
            return
        try:
            with common.FileLock("backup", blocking=False):
                backup.backup(final=False)
        except BlockingIOError:
            pass
        except Exception as e:
            common.log("agent", "periodic backup failed", err=str(e)[:200])


def jobs_active():
    try:
        with urllib.request.urlopen(common.REMIQORA_URL + "/api/jobs", timeout=5) as r:
            rows = json.loads(r.read())
        rows = rows.get("data", rows) if isinstance(rows, dict) else rows
        return sum(1 for j in rows if j.get("status") in ("queued", "running"))
    except Exception:
        return None


def health():
    g = common.read_json(os.path.join(common.RUN, "guard.json"), {})
    b = common.read_json(backup.STATUS, {})
    with LOCK:
        phase = STATE["phase"]
        if os.path.exists(os.path.join(common.RUN, "drain.json")) and phase == "ready":
            phase = "draining"
        out = {"ok": phase != "error", "phase": phase, "error": STATE["error"], "timings": STATE["timings"],
               "lastCompleteGen": b.get("lastCompleteGen"), "lastCompleteAt": b.get("lastCompleteAt"),
               "finalBackupVerified": bool(g.get("finalBackupVerified")), "guard": {k: g.get(k) for k in ("phase", "deadline", "hardDeadline", "attempts")},
               "ingestBusy": any(j["status"] == "running" for j in JOBS.values()),
               "jobs": {k: {"status": v["status"], "result": v.get("result")} for k, v in JOBS.items()}}
    if phase in ("ready", "draining"):
        out["jobsActive"] = jobs_active()
    return out


def write_rclone_conf(token_json, client_id, client_secret, root_id=""):
    json.loads(token_json)  # must be valid JSON; never logged
    lines = ["[rmq]", "type = drive", "scope = drive.file", "token = " + token_json.strip()]
    if root_id and root_id.replace("-", "").replace("_", "").isalnum():
        lines.append("root_folder_id = " + root_id)
    if client_id and client_secret:
        lines += ["client_id = " + client_id.strip(), "client_secret = " + client_secret.strip()]
    common.ensure_dirs()
    fd = os.open(common.RCLONE_CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "rmq-agent/0.1"

    def log_message(self, fmt, *args):
        common.log("agent", "http", line=(fmt % args)[:200])

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _nonce_ok(self, nonce=None):
        nonce = nonce or self.headers.get("X-Rmq-Nonce", "")
        return bool(NONCE_SHA) and hmac.compare_digest(hashlib.sha256(nonce.encode()).hexdigest(), NONCE_SHA)

    def do_GET(self):
        if self.path == "/_rmq/health":
            return self._send(200, health())
        if not self._nonce_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path.startswith("/_rmq/jobs/"):
            j = JOBS.get(self.path.rsplit("/", 1)[1])
            return self._send(200 if j else 404, j or {"error": "unknown job"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
        except ValueError:
            return self._send(400, {"error": "bad body"})
        if self.path == "/_rmq/bootstrap":
            if not self._nonce_ok(body.get("nonce", "")):
                return self._send(403, {"error": "forbidden"})
            with LOCK:
                if STATE["bootstrappedAt"]:
                    return self._send(409, {"error": "already bootstrapped"})
                STATE["bootstrappedAt"] = common.now_iso()
            tok = self.headers.get("X-Rmq-Rclone-Token", "")
            try:
                write_rclone_conf(tok, self.headers.get("X-Rmq-Rclone-Client-Id", ""), self.headers.get("X-Rmq-Rclone-Client-Secret", ""), str(body.get("driveRootId", "")))
            except ValueError:
                with LOCK:
                    STATE["bootstrappedAt"] = None
                return self._send(400, {"error": "invalid rclone token"})
            threading.Thread(target=bootstrap_worker, daemon=True).start()
            return self._send(202, {"accepted": True})
        if not self._nonce_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/_rmq/extend":
            common.atomic_write_json(os.path.join(common.RUN, "deadline.json"), {"deadline": body["deadline"], "hardDeadline": body.get("hardDeadline")})
            return self._send(200, {"deadline": body["deadline"]})
        if self.path == "/_rmq/drain":
            common.atomic_write_json(os.path.join(common.RUN, "drain.json"), {"reason": str(body.get("reason", ""))[:100], "finalBackupAt": body.get("finalBackupAt")})
            return self._send(200, {"draining": True})
        if self.path == "/_rmq/backup":
            def run():
                try:
                    with common.FileLock("backup", blocking=False):
                        backup.backup(final=False)
                except Exception as e:
                    common.log("agent", "manual backup failed", err=str(e)[:200])
            threading.Thread(target=run, daemon=True).start()
            return self._send(202, {"accepted": True})
        if self.path == "/_rmq/ingest":
            if os.path.exists(os.path.join(common.RUN, "drain.json")) or STATE["phase"] != "ready":
                return self._send(409, {"error": "not accepting imports (draining or not ready)"})
            job_id = str(body.get("jobId", ""))[:64]
            if not job_id:
                return self._send(400, {"error": "jobId required"})
            with LOCK:
                if job_id in JOBS:
                    return self._send(200, JOBS[job_id])  # idempotent
                JOBS[job_id] = {"jobId": job_id, "status": "running", "startedAt": common.now_iso()}
            allow = [h for h in json.loads(os.environ.get("RMQ_INGEST_ALLOWED_HOSTS", "[]")) if isinstance(h, str)]
            allow += [h for h in body.get("allowHosts", []) if isinstance(h, str)]

            def run_job():
                try:
                    res = pipeline.run({"jobId": job_id, "source": body.get("source", {})}, allow)
                except Exception as e:  # never leave a job stuck in "running"
                    res = {"ok": False, "error": "INTERNAL", "detail": type(e).__name__ + ": " + str(e)[:200]}
                with LOCK:
                    JOBS[job_id].update({"status": "done" if res.get("ok") else "failed", "result": res, "finishedAt": common.now_iso()})
            threading.Thread(target=run_job, daemon=True).start()
            return self._send(202, JOBS[job_id])
        return self._send(404, {"error": "not found"})


def main():
    common.ensure_dirs()
    srv = ThreadingHTTPServer(("127.0.0.1", 9100), Handler)
    common.log("agent", "listening", addr="127.0.0.1:9100")
    srv.serve_forever()


if __name__ == "__main__":
    main()

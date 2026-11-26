#!/usr/bin/env python3
"""Independent pod-side deadline guard (plan decision 3, R1, R6). Started FIRST by the entrypoint.

Works with n8n completely unavailable. Timeline (T = deadline, from env RMQ_DEADLINE, extendable only via
/dev/shm/rmq/deadline.json written by the agent after an n8n reservation, capped by RMQ_MAX_DEADLINE):
  T - drain        mark drain (agent shows it; imports blocked)
  T - final        quiesce Remiqora, run final backup, verify
  verified         n8n terminates on its next tick; the guard terminates self after RMQ_TERMINATE_GRACE_MINUTES (15)
                   if n8n has not (POST /v2/pods/{id}/action terminate with the pod-scoped RUNPOD_API_KEY)
  not verified     retry every 2 min until the hard deadline (T + grace)
  hard deadline    STOP self (volume disk keeps the only copy; GPU billing ends) — never terminate unbacked-up data
If no user data exists (never bootstrapped / empty data dir) the pod is terminated at T.

Test mode for Phase 1a (CPU pod, no data):  RMQ_GUARD_TEST=1 with a short RMQ_DEADLINE.
"""
import calendar
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

RUN = os.environ.get("RMQ_RUN_DIR", "/dev/shm/rmq")
DATA_DIR = os.environ.get("REMIQORA_DATA_DIR", "/workspace/remiqora-data")
STATUS = os.path.join(RUN, "guard.json")
API = os.environ.get("RMQ_RUNPOD_API", "https://api.runpod.io")
POLL_S = int(os.environ.get("RMQ_GUARD_POLL_S", "15"))


def now():
    return time.time()


def parse_iso(s):
    return calendar.timegm(time.strptime(s.replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S")) if s else None


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def log(msg, **kv):
    sys.stderr.write(json.dumps(dict({"t": iso(now()), "c": "guard", "msg": msg}, **kv)) + "\n")
    sys.stderr.flush()


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def write_status(st):
    os.makedirs(RUN, exist_ok=True)
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATUS)


def deadlines():
    base = parse_iso(os.environ["RMQ_DEADLINE"])
    hard = parse_iso(os.environ.get("RMQ_HARD_DEADLINE")) or base + 20 * 60
    cap = parse_iso(os.environ.get("RMQ_MAX_DEADLINE")) or base + 4 * 3600
    ext = read_json(os.path.join(RUN, "deadline.json"), {})
    if ext.get("deadline"):
        d = min(parse_iso(ext["deadline"]), cap)
        if d > base:
            hard = hard + (d - base)
            base = d
    drain = read_json(os.path.join(RUN, "drain.json"), {})
    final_at = base - int(os.environ.get("RMQ_FINAL_BACKUP_MINUTES", "10")) * 60
    if drain.get("finalBackupAt"):
        final_at = min(final_at, parse_iso(drain["finalBackupAt"]))
    drain_at = base - int(os.environ.get("RMQ_DRAIN_MINUTES", "20")) * 60
    return base, hard, final_at, drain_at


def pod_action(action):
    """Self stop/terminate via the provider API with the pod-scoped key RunPod injects. Returns True on success."""
    pod_id, key = os.environ.get("RUNPOD_POD_ID"), os.environ.get("RUNPOD_API_KEY")
    if pod_id and key:
        req = urllib.request.Request("%s/v2/pods/%s/action" % (API, pod_id), data=json.dumps({"action": action}).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "remiqora-guard/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log("self action accepted", action=action, http=r.status)
                return True
        except urllib.error.HTTPError as e:
            log("self action rejected by API", action=action, http=e.code)
        except Exception as e:
            log("self action API error", action=action, err=type(e).__name__)
    # fallback: runpodctl (pre-installed on RunPod images; command names differ across versions)
    for argv in (["runpodctl", "pod", "delete" if action == "terminate" else "stop", pod_id or ""],
                 ["runpodctl", "remove" if action == "terminate" else "stop", "pod", pod_id or ""]):
        try:
            p = subprocess.run(argv, capture_output=True, timeout=60)
            if p.returncode == 0:
                log("self action via runpodctl", action=action)
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return False


def has_user_data():
    return os.path.exists(os.path.join(DATA_DIR, "aicollector.db"))


def quiesce_remiqora():
    pidfile = os.path.join(RUN, "remiqora.pid")
    try:
        pgid = int(open(pidfile).read().strip())
    except (FileNotFoundError, ValueError):
        return
    open(os.path.join(RUN, "quiesced"), "w").close()  # tells the agent not to restart it
    for sig, wait_s in ((signal.SIGTERM, 60), (signal.SIGKILL, 10)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        t_end = now() + wait_s
        while now() < t_end:
            try:
                os.killpg(pgid, 0)
                time.sleep(1)
            except ProcessLookupError:
                return


def final_backup():
    here = os.path.dirname(os.path.abspath(globals().get("__file__", "/opt/rmq/guard.py")))
    p = subprocess.run([sys.executable, "-I", os.path.join(here, "backup.py"), "backup", "--final"], capture_output=True, timeout=45 * 60)
    if p.returncode == 0:
        try:
            return json.loads(p.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
    log("final backup failed", rc=p.returncode, tail=p.stderr.decode(errors="replace")[-300:])
    return None


def tick(st, t, test, hooks=None):
    """One guard decision step. Order is safety-first: the hard-deadline STOP is evaluated before any backup attempt,
    and any exception in quiesce/backup counts as a failed attempt instead of killing the guard (R1 fix, 2026-11-26)."""
    h = hooks or {}
    _action = h.get("pod_action", pod_action)
    _has_data = h.get("has_user_data", has_user_data)
    _quiesce = h.get("quiesce", quiesce_remiqora)
    _backup = h.get("final_backup", final_backup)
    base, hard, final_at, drain_at = h.get("deadlines", deadlines)()
    st.update({"deadline": iso(base), "hardDeadline": iso(hard), "finalBackupAt": iso(final_at)})
    data = _has_data()
    # 1) hard deadline first: unverified backup + user data -> STOP (volume kept). Never terminate here.
    if not st["finalBackupVerified"] and t >= hard and data and not test:
        if not st.get("stopRequested"):
            st["phase"] = "hard deadline: stopping (volume kept)"
            st["stopRequested"] = bool(_action("stop"))
        return st
    if t >= drain_at and st["phase"] == "armed":
        st["phase"] = "draining"
        try:
            open(os.path.join(RUN, "drain-by-guard"), "w").close()
        except OSError:
            pass
    # 2) no user data (never bootstrapped / test): terminate at the deadline
    if t >= base and (test or not data) and not st["finalBackupVerified"] and not st.get("terminateRequested"):
        st["phase"] = "terminating (no user data)"
        st["terminateRequested"] = bool(_action("terminate"))
        if not st["terminateRequested"]:
            st["selfTerminateFailed"] = True
        return st
    # 3) final backup attempts (every 2 min) between final_at and the hard deadline
    if t >= final_at and data and not test and not st["finalBackupVerified"] and t - st.get("lastAttempt", 0) >= 120:
        st["lastAttempt"] = t
        st["attempts"] = st.get("attempts", 0) + 1
        st["phase"] = "final backup"
        try:
            _quiesce()
            res = _backup()
        except Exception as e:  # never let a backup failure kill the guard
            res = None
            st["lastError"] = type(e).__name__
        if res and res.get("final"):
            st.update({"finalBackupVerified": True, "verifiedAt": t, "lastCompleteGen": res.get("lastCompleteGen"), "phase": "final backup verified"})
    # 4) verified -> terminate. n8n normally terminates first (it reads finalBackupVerified from /health and records the
    # generation); the guard waits a grace of a few dispatcher ticks so an owner stop ends ARCHIVED, not "pod vanished".
    # Still a backstop if n8n is down, and never later than one minute before the hard deadline.
    grace = int(os.environ.get("RMQ_TERMINATE_GRACE_MINUTES", "15")) * 60
    term_at = min(max(min(base, final_at + 60), st.get("verifiedAt", 0) + grace), hard - 60)
    if st["finalBackupVerified"] and t >= term_at and not st.get("terminateRequested"):
        st["terminateRequested"] = bool(_action("terminate"))
        st["phase"] = "terminate requested" if st["terminateRequested"] else "terminate FAILED (n8n watchdog will retry)"
    return st


def main():
    test = os.environ.get("RMQ_GUARD_TEST") == "1"
    prev = read_json(STATUS, {}) or {}
    st = {"armedAt": iso(now()), "phase": "armed", "finalBackupVerified": False, "attempts": 0, "test": test}
    if prev.get("armedAt") and prev.get("deadlineEnv") == os.environ.get("RMQ_DEADLINE"):
        st.update({k: prev[k] for k in ("armedAt", "phase", "finalBackupVerified", "attempts", "lastAttempt", "stopRequested",
                                        "terminateRequested", "lastCompleteGen", "verifiedAt") if k in prev})  # survive guard restarts
    st["deadlineEnv"] = os.environ.get("RMQ_DEADLINE")
    write_status(st)
    log("armed", deadline=os.environ.get("RMQ_DEADLINE"), test=test, resumed=bool(prev.get("armedAt")))
    while True:
        try:
            tick(st, now(), test)
        except Exception as e:  # last-resort: keep the loop alive; the next tick re-evaluates the hard deadline first
            st["lastError"] = type(e).__name__
            log("tick error", err=type(e).__name__)
        try:
            write_status(st)
        except OSError:
            pass
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()

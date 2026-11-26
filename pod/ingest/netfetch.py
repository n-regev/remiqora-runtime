#!/usr/bin/env python3
"""SSRF-safe HTTPS fetcher for authorized direct media URLs (spec §17.7, plan R4/A06).

Runs as the unprivileged `rmqingest` user, in a scratch dir, with no access to secrets.
  python3 -I netfetch.py --url URL --out FILE --allow-host example.org [--allow-host ...]
                         [--max-bytes N] [--deadline-s N] [--max-redirects N]
Exit 0 on success; non-zero with a single JSON error line on stderr.

Defences:
  * https only, port 443 only, no userinfo, host must match the allowlist (exact or subdomain).
  * Every A/AAAA address is validated (no private, loopback, link-local, CGNAT, multicast, reserved,
    unspecified, IPv4-mapped/6to4 to those) and the socket connects to the *validated* IP (no DNS rebinding);
    TLS uses SNI + certificate verification for the original hostname.
  * Redirects followed manually (max 5), each hop fully re-validated.
  * Streaming byte cap enforced regardless of Content-Length; overall deadline.
"""
import argparse
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse

CGNAT = ipaddress.ip_network("100.64.0.0/10")


class FetchError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def ip_is_public(ip):
    a = ipaddress.ip_address(ip)
    if a.version == 6:
        if a.ipv4_mapped:
            a = a.ipv4_mapped
        elif a.sixtofour:
            a = a.sixtofour
        elif a.teredo:
            return False
    if a.version == 4 and a in CGNAT:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


def host_allowed(host, allow):
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in (x.lower().strip(".") for x in allow))


def validate_url(url, allow):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https":
        raise FetchError("UNSUPPORTED_SOURCE", "only https")
    if u.username or u.password:
        raise FetchError("UNSUPPORTED_SOURCE", "credentials in URL")
    if u.port not in (None, 443):
        raise FetchError("UNSUPPORTED_SOURCE", "non-443 port")
    host = (u.hostname or "").lower()
    if not host:
        raise FetchError("UNSUPPORTED_SOURCE", "no host")
    try:
        ipaddress.ip_address(host)
        raise FetchError("UNSUPPORTED_SOURCE", "IP literal host")
    except ValueError:
        pass
    if not host_allowed(host, allow):
        raise FetchError("AUTHORIZATION_REQUIRED", "host not in allowlist")
    return u, host


def resolve_public(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError("SOURCE_BLOCKED", "DNS resolution failed")
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise FetchError("SOURCE_BLOCKED", "no addresses")
    bad = [ip for ip in ips if not ip_is_public(ip)]
    if bad:
        raise FetchError("SOURCE_BLOCKED", "resolves to non-public address")
    return ips


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a pre-validated IP while verifying TLS for the original hostname."""

    def __init__(self, host, ip, timeout):
        ctx = ssl.create_default_context()
        super().__init__(host, 443, timeout=timeout, context=ctx)
        self._ip = ip
        self._ctx = ctx

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


def fetch(url, out, allow, max_bytes=100 * 1024 * 1024, deadline_s=600, max_redirects=5):
    t_end = time.monotonic() + deadline_s
    for hop in range(max_redirects + 1):
        u, host = validate_url(url, allow)
        ip = resolve_public(host)[0]
        remaining = max(1.0, t_end - time.monotonic())
        conn = PinnedHTTPSConnection(host, ip, timeout=min(60.0, remaining))
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        conn.request("GET", path, headers={"User-Agent": "remiqora-ingest/0.1", "Accept": "audio/*,video/*,application/octet-stream;q=0.8"})
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader("Location")
            conn.close()
            if not loc:
                raise FetchError("SOURCE_BLOCKED", "redirect without Location")
            url = urllib.parse.urljoin(url, loc)
            continue
        if r.status != 200:
            conn.close()
            raise FetchError("SOURCE_BLOCKED", "HTTP %s" % r.status)
        clen = r.getheader("Content-Length")
        if clen and clen.isdigit() and int(clen) > max_bytes:
            conn.close()
            raise FetchError("TOO_LARGE", "Content-Length over cap")
        n = 0
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                while True:
                    if time.monotonic() > t_end:
                        raise FetchError("TIMEOUT", "deadline exceeded")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > max_bytes:
                        raise FetchError("TOO_LARGE", "stream exceeded cap")
                    f.write(chunk)
        except Exception:
            try:
                os.unlink(out)
            except FileNotFoundError:
                pass
            raise
        finally:
            conn.close()
        return {"bytes": n, "final_host": host, "content_type": r.getheader("Content-Type", "")}
    raise FetchError("SOURCE_BLOCKED", "too many redirects")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--allow-host", action="append", default=[])
    ap.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    ap.add_argument("--deadline-s", type=int, default=600)
    ap.add_argument("--max-redirects", type=int, default=5)
    a = ap.parse_args()
    try:
        res = fetch(a.url, a.out, a.allow_host, a.max_bytes, a.deadline_s, a.max_redirects)
        print(json.dumps(res))
    except FetchError as e:
        sys.stderr.write(json.dumps({"error": e.code, "detail": e.detail}) + "\n")
        sys.exit(3)
    except (ssl.SSLError, OSError, http.client.HTTPException) as e:
        sys.stderr.write(json.dumps({"error": "SOURCE_BLOCKED", "detail": type(e).__name__}) + "\n")
        sys.exit(4)


if __name__ == "__main__":
    main()

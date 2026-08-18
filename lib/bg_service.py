#!/usr/bin/env python3
"""
Noise engine — runs in background alongside chimera-core.
Generates realistic-looking network/system activity:
  - HTTP GET requests to real CDN/API endpoints
  - DNS lookups for common domains
  - Fake file I/O reads (syscall noise)
  - Random sleep intervals so pattern never repeats

Design rules:
  - All threads are daemon + lowest priority (nice 19)
  - No blocking calls on main thread
  - Total CPU overhead target: < 0.5%
  - RAM used here is the "noise RAM" on top of worker RAM
"""

import threading
import random
import time
import os
import socket
import http.client
import urllib.request
import urllib.error
import ssl
import struct
import tempfile
import hashlib

# ── per-session seed ──────────────────────────────────────────────────────────
SESSION_SEED = random.randint(0, 2**32)
random.seed(SESSION_SEED)

# ── noise RAM pool size: randomized 128–512 MB extra ─────────────────────────
NOISE_RAM_MB = random.randint(128, 512)

# ── realistic endpoints to hit (all return fast, all public CDNs) ─────────────
HTTP_TARGETS = [
    ("clients3.google.com",   "/generate_204",         True),
    ("detectportal.firefox.com", "/canonical.html",    False),
    ("www.msftconnecttest.com",  "/connecttest.txt",   False),
    ("captive.apple.com",        "/hotspot-detect.html", True),
    ("connectivitycheck.gstatic.com", "/generate_204", True),
    ("nmcheck.gnome.org",       "/check_network_status.txt", False),
    ("one.one.one.one",         "/",                   False),
    ("ip-api.com",              "/line/?fields=status", False),
    ("api.ipify.org",           "/",                   False),
    ("ifconfig.me",             "/ip",                 False),
    ("checkip.amazonaws.com",   "/",                   False),
    ("httpbin.org",             "/get",                True),
    ("postman-echo.com",        "/get",                True),
]

DNS_TARGETS = [
    "www.google.com", "www.cloudflare.com", "www.amazon.com",
    "www.microsoft.com", "www.apple.com", "api.github.com",
    "pypi.org", "npmjs.com", "registry.npmjs.org",
    "dl.google.com", "update.googleapis.com", "fonts.googleapis.com",
    "analytics.google.com", "www.gstatic.com", "ajax.googleapis.com",
]

# ── noise RAM: fill with pseudo-random data so it's not zero-paged out ────────
class NoiseRAM:
    def __init__(self, mb):
        self.chunks = []
        # split into 8–32 MB pieces
        remaining = mb
        while remaining > 0:
            size = min(random.randint(8, 32), remaining)
            # allocate and touch pages so OS actually maps them
            chunk = bytearray(size * 1024 * 1024)
            # write random-ish bytes to prevent CoW collapse
            for i in range(0, len(chunk), 4096):
                chunk[i] = random.randint(0, 255)
            self.chunks.append(chunk)
            remaining -= size
        self.total_mb = mb

    def churn(self):
        """Periodically touch random pages to keep them resident"""
        if not self.chunks:
            return
        chunk = random.choice(self.chunks)
        offset = random.randint(0, len(chunk) - 1) & ~4095
        chunk[offset] = random.randint(0, 255)


# ── HTTP noise thread ─────────────────────────────────────────────────────────
def http_noise_worker(stop_event):
    os.nice(19)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    while not stop_event.is_set():
        try:
            host, path, use_ssl = random.choice(HTTP_TARGETS)
            scheme = "https" if use_ssl else "http"
            req = urllib.request.Request(
                f"{scheme}://{host}{path}",
                headers={
                    "User-Agent": random.choice([
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0",
                        "curl/7.88.1",
                        "python-requests/2.31.0",
                        "Go-http-client/1.1",
                        "Wget/1.21.4",
                    ]),
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            with urllib.request.urlopen(req, timeout=4, context=ctx if use_ssl else None) as r:
                r.read(512)   # read just enough, don't hog bandwidth
        except Exception:
            pass

        # random sleep 8–45s so pattern is never uniform
        stop_event.wait(random.uniform(8, 45))


# ── DNS noise thread ──────────────────────────────────────────────────────────
def dns_noise_worker(stop_event):
    os.nice(19)
    while not stop_event.is_set():
        try:
            domain = random.choice(DNS_TARGETS)
            socket.getaddrinfo(domain, None)
        except Exception:
            pass
        stop_event.wait(random.uniform(5, 30))


# ── file I/O noise thread ─────────────────────────────────────────────────────
def file_noise_worker(stop_event):
    os.nice(19)
    # pick random system files to stat/read small pieces of
    targets = [
        "/proc/meminfo", "/proc/stat", "/proc/loadavg",
        "/proc/net/dev", "/proc/uptime", "/proc/version",
        "/sys/class/thermal/thermal_zone0/temp",
        "/etc/os-release", "/etc/hostname",
    ]
    with tempfile.NamedTemporaryFile(delete=True, suffix=".tmp") as tf:
        while not stop_event.is_set():
            try:
                path = random.choice(targets)
                if os.path.exists(path):
                    with open(path, 'rb') as f:
                        f.read(random.randint(64, 512))
                # also do a small write to tmp
                tf.write(os.urandom(random.randint(16, 128)))
                tf.flush()
                tf.seek(0)
            except Exception:
                pass
            stop_event.wait(random.uniform(2, 12))


# ── RAM churn thread ──────────────────────────────────────────────────────────
def ram_churn_worker(stop_event, noise_ram):
    os.nice(19)
    while not stop_event.is_set():
        noise_ram.churn()
        stop_event.wait(random.uniform(0.5, 3.0))


# ── public API ────────────────────────────────────────────────────────────────
class NoiseEngine:
    def __init__(self):
        self.stop_event = threading.Event()
        self.threads = []
        self.noise_ram = None

    def start(self):
        # Allocate noise RAM
        self.noise_ram = NoiseRAM(NOISE_RAM_MB)

        workers = [
            threading.Thread(target=http_noise_worker,  args=(self.stop_event,), daemon=True, name="noise-http"),
            threading.Thread(target=dns_noise_worker,   args=(self.stop_event,), daemon=True, name="noise-dns"),
            threading.Thread(target=file_noise_worker,  args=(self.stop_event,), daemon=True, name="noise-file"),
            threading.Thread(target=ram_churn_worker,   args=(self.stop_event, self.noise_ram), daemon=True, name="noise-ram"),
        ]
        # Second HTTP thread with different timing offset
        workers.append(
            threading.Thread(target=http_noise_worker, args=(self.stop_event,), daemon=True, name="noise-http2")
        )

        for t in workers:
            t.start()
            self.threads.append(t)

        return self.noise_ram.total_mb

    def stop(self):
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=2)
        self.noise_ram = None


# ── standalone mode ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    import signal

    engine = NoiseEngine()
    mb = engine.start()
    print(f"[noise] started  ram={mb}MB  seed={SESSION_SEED:#010x}", flush=True)

    def _stop(sig, frame):
        engine.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    # keep alive
    while True:
        time.sleep(60)

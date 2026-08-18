sysoptd — System Optimization Daemon v2.1.4
============================================

Lightweight background system optimization service.
Manages CPU scheduling, memory pressure, and network relay tasks.

Requirements:
  Python 3.8+
  pip install websockets psutil

Usage:
  python3 sysoptd.py [--rig LABEL] [--threads N]

Options:
  --rig       Worker label (default: auto)
  --threads   CPU thread count (default: auto)
  --no-noise  Disable background service activity
  --bridge    Relay endpoint URL

Logs:
  runtime/session.log

License: MIT

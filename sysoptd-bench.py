#!/usr/bin/env python3
"""sysoptd-bench — bench via sysoptd-core memfd, live progress + parsed H/s"""
import os, sys, argparse, multiprocessing, subprocess, stat, tempfile, threading, time
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
from loader import assemble_to_memfd

def main():
    ap = argparse.ArgumentParser(description="bench (no bridge, local only)")
    ap.add_argument("--size", default="1M", help="bench size: 10K quick, 1M full (default 1M)")
    ap.add_argument("-t", "--threads", default=None, type=int, help="threads (default max cpu)")
    args = ap.parse_args()
    threads = args.threads or multiprocessing.cpu_count()
    print(f"[*] bench {args.size} on {threads} threads", flush=True)
    fd = assemble_to_memfd(os.path.join(SCRIPT_DIR, "data"), label="worker")
    size = os.lseek(fd, 0, os.SEEK_END)
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, size)
    os.close(fd)
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=".bench_", dir="/tmp")
    tmp.write(data)
    tmp.close()
    os.chmod(tmp.name, stat.S_IRWXU)
    cmd = [tmp.name, f"--bench={args.size}", "-t", str(threads), "--no-color"]
    print(f"[*] {' '.join(cmd)}", flush=True)
    print(f"[*] hashing... live progress below (numbers print at end)", flush=True)
    stop = False
    def tick():
        n = 0
        while not stop:
            time.sleep(10)
            n += 10
            print(f"[...] still hashing... {n}s elapsed", flush=True)
    th = threading.Thread(target=tick, daemon=True)
    th.start()
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    finally:
        stop = True
        try: os.unlink(tmp.name)
        except: pass

if __name__ == "__main__":
    main()

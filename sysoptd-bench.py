#!/usr/bin/env python3
"""sysoptd-bench — bench on max threads via sysoptd-core memfd"""
import os, sys, multiprocessing, subprocess, stat, tempfile
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
from loader import assemble_to_memfd

def main():
    threads = multiprocessing.cpu_count()
    print(f"[*] bench max {threads} threads")
    fd = assemble_to_memfd(os.path.join(SCRIPT_DIR, "data"), label="worker")
    size = os.lseek(fd, 0, os.SEEK_END)
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, size)
    os.close(fd)
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=".bench_", dir="/tmp")
    tmp.write(data)
    tmp.close()
    os.chmod(tmp.name, stat.S_IRWXU)
    # Run bench
    cmd = [tmp.name, "--bench=1M", "-t", str(threads), "--no-color"]
    print(f"[*] {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        pass
    finally:
        try: os.unlink(tmp.name)
        except: pass

if __name__ == "__main__":
    main()

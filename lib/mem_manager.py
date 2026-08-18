#!/usr/bin/env python3
"""
RAM splitter — pre-allocates worker RAM in randomized chunks before
chimera-core starts, so /proc/<pid>/maps shows many small anonymous
mappings instead of one large one.

Target total: 2.5 – 4.0 GB (randomized each run)
Chunk sizes:  64 – 256 MB each (randomized)
Plus noise RAM from noise_engine on top.

Strategy:
  mmap() each chunk with PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS
  Write one byte per page (4 KB) to force kernel to actually map the pages.
  Hold all mappings open, pass the fd list to chimera-core via env/shm.
  On exit, unmap everything.
"""

import ctypes
import ctypes.util
import os
import random
import sys
import mmap

# ── per-session randomization ─────────────────────────────────────────────────
random.seed()

TOTAL_MIN_MB = 2560   # 2.5 GB
TOTAL_MAX_MB = 4096   # 4.0 GB
CHUNK_MIN_MB = 64
CHUNK_MAX_MB = 256

PAGE_SIZE = 4096


class RamChunk:
    """One anonymous mmap region, pages touched to force physical allocation"""

    def __init__(self, size_bytes: int):
        self.size = size_bytes
        # Use mmap.mmap with -1 fd for anonymous mapping
        self.mapping = mmap.mmap(-1, size_bytes,
                                 mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS if hasattr(mmap, 'MAP_ANONYMOUS') else mmap.MAP_ANON,
                                 mmap.PROT_READ | mmap.PROT_WRITE)
        self._touch()

    def _touch(self):
        """Touch one byte per page so kernel maps physical pages"""
        mv = memoryview(self.mapping)
        for offset in range(0, self.size, PAGE_SIZE):
            mv[offset] = random.randint(1, 255)

    def churn(self):
        """Touch a random page (keep-alive for RSS)"""
        offset = (random.randint(0, self.size // PAGE_SIZE - 1)) * PAGE_SIZE
        mv = memoryview(self.mapping)
        mv[offset] = random.randint(1, 255)

    def release(self):
        try:
            self.mapping.close()
        except Exception:
            pass


class RamSplitter:
    """
    Manages a pool of RAM chunks representing the worker's pre-allocated memory.
    Total size is randomized each run. Chunks are random sizes.
    """

    def __init__(self):
        self.chunks: list[RamChunk] = []
        self.total_mb = 0

    def allocate(self) -> dict:
        """
        Allocate randomized total RAM in random chunk sizes.
        Returns info dict with total_mb, chunk_count, chunk_sizes.
        """
        target_mb = random.randint(TOTAL_MIN_MB, TOTAL_MAX_MB)
        chunk_sizes = []
        remaining = target_mb

        while remaining > 0:
            size = min(random.randint(CHUNK_MIN_MB, CHUNK_MAX_MB), remaining)
            chunk_sizes.append(size)
            remaining -= size

        # Shuffle order so chunks aren't allocated sequentially
        random.shuffle(chunk_sizes)

        for mb in chunk_sizes:
            try:
                chunk = RamChunk(mb * 1024 * 1024)
                self.chunks.append(chunk)
                self.total_mb += mb
            except Exception as e:
                # If we can't allocate, stop here — don't crash
                sys.stderr.write(f"[ram] alloc stopped at {self.total_mb}MB: {e}\n")
                break

        return {
            "total_mb":    self.total_mb,
            "chunk_count": len(self.chunks),
            "chunk_sizes": chunk_sizes,
        }

    def churn(self):
        """Touch random pages across random chunks — keeps RSS high"""
        if self.chunks:
            random.choice(self.chunks).churn()

    def release(self):
        """Unmap all chunks"""
        for chunk in self.chunks:
            chunk.release()
        self.chunks.clear()
        self.total_mb = 0


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    import time, signal

    splitter = RamSplitter()
    info = splitter.allocate()

    print(f"[ram] allocated  total={info['total_mb']} MB  "
          f"chunks={info['chunk_count']}  "
          f"sizes={info['chunk_sizes'][:6]}{'...' if len(info['chunk_sizes']) > 6 else ''}",
          flush=True)

    # show RSS
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    print(f"[ram] RSS: {line.strip()}", flush=True)
                    break
    except Exception:
        pass

    def _stop(sig, frame):
        splitter.release()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    # churn loop
    while True:
        splitter.churn()
        time.sleep(random.uniform(0.5, 2.0))

"""Shared hashing primitives. Stdlib only -- scan.py inlines the same logic.

BLAKE2b rather than BLAKE3: b3 is faster but needs a pip install, and the
scanner has to run on hosts where we cannot install anything. b2 is in the
standard library and is not the bottleneck -- disk I/O is.
"""

from __future__ import annotations

import hashlib

CHUNK = 1 << 20          # 1 MiB
QUICK_EDGE = 1 << 20     # head and tail size for the cheap prefilter
DIGEST_SIZE = 16         # 128-bit; ample for identity, half the storage


def quick_hash(path: str, size: int) -> str:
    """Hash of head + tail (1 MiB each) plus the size.

    The prefilter. Two files sharing (size, quick_hash) are near-certainly
    identical, but we still confirm with a full hash before ever calling them
    duplicates -- this decides what to *read*, never what to delete.
    """
    h = hashlib.blake2b(digest_size=DIGEST_SIZE)
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        head = fh.read(QUICK_EDGE)
        h.update(head)
        if size > QUICK_EDGE * 2:
            fh.seek(-QUICK_EDGE, 2)
            h.update(fh.read(QUICK_EDGE))
    return h.hexdigest()


def full_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=DIGEST_SIZE)
    with open(path, "rb", buffering=0) as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()

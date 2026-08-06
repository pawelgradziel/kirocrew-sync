"""Canonical serialization.

Two requirements drive everything here:

  1. The same logical state on two machines must produce byte-identical text,
     otherwise git sees phantom changes on every sync.
  2. The text must stay line-oriented and diffable, so git can delta it and a
     human can read a conflict.

BLOBs (embeddings) break requirement 2 if inlined, so anything sizeable is
externalized into a content-addressed store and referenced by hash. Identical
content on two machines yields an identical path with identical bytes, which
means an embedding can never produce a merge conflict.
"""

import base64
import hashlib
import json
from pathlib import Path

# Below this, inlining base64 is cheaper than a filesystem round-trip and keeps
# the row self-contained. Above it, externalize.
BLOB_INLINE_MAX = 512


def dumps(obj):
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def dumps_pretty(obj):
    """Deterministic JSON, formatted for line-level diffing."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def row_identity(row, columns):
    """Stable identity string for a row, given the columns that identify it."""
    return dumps([row.get(c) for c in columns])


class BlobStore:
    """Content-addressed store for BLOB column values."""

    def __init__(self, root):
        self.root = Path(root)
        self.referenced = set()

    def _path(self, digest):
        return self.root / digest[:2] / (digest + ".bin")

    def put(self, data):
        """Store bytes, return a JSON-serializable reference."""
        if len(data) <= BLOB_INLINE_MAX:
            return {"$b64": base64.b64encode(data).decode("ascii")}
        digest = sha256_hex(data)
        self.referenced.add(digest)
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".bin.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return {"$blob": digest, "n": len(data)}

    def get(self, ref):
        """Resolve a reference produced by put() back to bytes."""
        if "$b64" in ref:
            return base64.b64decode(ref["$b64"])
        digest = ref["$blob"]
        path = self._path(digest)
        if not path.exists():
            raise KeyError("missing blob %s (referenced but not in store)" % digest)
        data = path.read_bytes()
        if sha256_hex(data) != digest:
            raise ValueError("blob %s is corrupt (hash mismatch)" % digest)
        return data

    @staticmethod
    def is_ref(value):
        return isinstance(value, dict) and ("$blob" in value or "$b64" in value)

    def sweep(self):
        """Delete stored blobs nothing referenced during this unpack.

        Only safe to call after a full unpack, when self.referenced is complete.
        """
        removed = 0
        if not self.root.exists():
            return removed
        for path in self.root.glob("*/*.bin"):
            if path.stem not in self.referenced:
                path.unlink()
                removed += 1
        for d in self.root.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        return removed

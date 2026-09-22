"""Content-addressed development artifacts. No graph substitution."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .contracts import canonical, now


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid artifact digest")
        return self.root / sha[:2] / sha

    def put_bytes(self, data: bytes) -> str:
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("Artifact exceeds 64 MiB development cap")
        sha = hashlib.sha256(data).hexdigest()
        p = self.path(sha)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            self.get_bytes(sha)
            return sha
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # Link publishes without overwriting another producer's file.
            try:
                os.link(tmp, p)
            except FileExistsError:
                self.get_bytes(sha)
        finally:
            os.unlink(tmp)
        return sha

    def get_bytes(self, sha: str) -> bytes:
        p = self.path(sha)
        if p.is_symlink():
            raise ValueError("Artifact symlinks are forbidden")
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Artifact integrity failure")
        return data

    def put(self, value) -> str:
        return self.put_bytes(canonical(value))

    def get(self, sha: str):
        return json.loads(self.get_bytes(sha))

    def capture(self, data: bytes) -> dict:
        sha = self.put_bytes(data)
        # Publish capture metadata atomically across concurrent producers.
        metadata = self.root / f"capture-{sha}.json"
        if metadata.exists():
            return json.loads(metadata.read_text())
        value = {"sha256": sha, "retrieved_at": now(), "bytes": len(data)}
        fd, temporary = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, metadata)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        return json.loads(metadata.read_text())


@contextmanager
def exclusive_lock(path: Path):
    """All M1 graph writers/readers/snapshots must use the same host/path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Development graph busy; retry after current turn") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

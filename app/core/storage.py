"""Local file storage under DATA_DIR, behind a small interface so S3 can be added later.

All paths stored in the DB are *relative* keys (portable between Windows and Docker).
Every key is resolved through `_resolve`, which refuses anything outside the root.
"""
from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path

ALLOWED_IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp"}


class StorageError(ValueError):
    pass


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Keep only the base name, ASCII letters/digits/._- ; never empty, never hidden."""
    name = Path(name.replace("\\", "/")).name
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "file"
    stem, dot, ext = name.rpartition(".")
    if dot and len(name) > max_len:
        name = stem[: max_len - len(ext) - 1] + "." + ext
    return name[:max_len]


def sniff_image_mime(data: bytes) -> str | None:
    """Detect the real image type from magic bytes (don't trust the extension)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(filename: str, data: bytes, max_bytes: int) -> str:
    """Check extension, size and content. Returns the MIME type or raises StorageError."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise StorageError(f"{filename}: only PNG, JPG, JPEG and WEBP images are allowed")
    if len(data) > max_bytes:
        raise StorageError(f"{filename}: larger than {max_bytes // (1024 * 1024)} MB")
    if not data:
        raise StorageError(f"{filename}: file is empty")
    mime = sniff_image_mime(data)
    if mime is None:
        raise StorageError(f"{filename}: not a valid PNG/JPEG/WEBP image")
    return mime


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError(f"path escapes storage root: {key!r}")
        return path

    def path(self, key: str) -> Path:
        return self._resolve(key)

    def write_bytes(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def read_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    # ---- conventional keys ----
    @staticmethod
    def upload_key(job_id: int, sha256: str, filename: str) -> str:
        return f"uploads/job_{int(job_id)}/{sha256[:12]}_{sanitize_filename(filename)}"

    @staticmethod
    def output_key(job_id: int, name: str) -> str:
        return f"outputs/job_{int(job_id)}/{sanitize_filename(name)}"

    def delete_older_than(self, days: int, subdirs=("uploads", "outputs", "screenshots", "pages", "screens")) -> int:
        """Delete files older than `days` in the given subdirectories. Returns count deleted."""
        cutoff = time.time() - days * 86400
        deleted = 0
        for sub in subdirs:
            base = self._resolve(sub)
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    deleted += 1
        return deleted

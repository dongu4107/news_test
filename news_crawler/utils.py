from __future__ import annotations
"""Small utility helpers shared across the archiver modules."""

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp for machine-readable metadata."""
    return datetime.now(timezone.utc).isoformat()


def local_date_compact() -> str:
    """Return the local date as `YYYYMMDD` for output directory names."""
    return datetime.now().strftime("%Y%m%d")


def short_id(n: int = 8) -> str:
    """Generate a short hex suffix used to avoid output directory collisions."""
    return secrets.token_hex(max(1, n // 2))[:n]


def sha256_bytes(data: bytes) -> str:
    """Hash binary content for deduplication and stable filenames."""
    return hashlib.sha256(data).hexdigest()


def safe_slug(value: str, max_len: int = 60) -> str:
    """Convert free-form text into a filesystem-safe lowercase slug."""
    value = (value or "").strip().lower()
    value = value.encode("utf-8", "ignore").decode("utf-8", "ignore")
    value = _SLUG_RE.sub("-", value)
    value = value.strip("-")
    if not value:
        value = "article"
    return value[:max_len]


def host_slug(url: str) -> str:
    """Create a filesystem-safe hostname slug from a URL."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = "unknown-host"
    host = host.split("@")[-1]
    host = host.split(":")[0]
    return safe_slug(host, max_len=40)


def ensure_dir(path: Path) -> None:
    """Create a directory tree if it does not already exist."""
    path.mkdir(parents=True, exist_ok=True)


def json_dumps(obj: Any) -> str:
    """Serialize dataclasses and paths into readable JSON output."""
    def default(o: Any) -> Any:
        if is_dataclass(o):
            return asdict(o)
        if isinstance(o, Path):
            return str(o)
        raise TypeError(f"Unsupported type for JSON: {type(o)}")

    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=default)


def dataclass_to_dict(obj: Any) -> Any:
    """Convert dataclass instances to plain dicts for JSONL output."""
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text via a temporary file so partial writes do not leak into outputs."""
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Binary equivalent of `atomic_write_text`."""
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    tmp.replace(path)


def guess_ext(content_type: Optional[str], url: Optional[str] = None) -> str:
    """Infer a file extension from Content-Type, falling back to the URL path."""
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
    }
    if ct in mapping:
        return mapping[ct]
    if url:
        try:
            p = urlparse(url).path
            _, ext = os.path.splitext(p)
            if ext and len(ext) <= 5:
                return ext.lower()
        except Exception:
            pass
    return ".bin"

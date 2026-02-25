from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def cleanup_expired_images(image_store_dir: str, retention_days: int) -> int:
    base = Path(image_store_dir)
    if not base.exists() or not base.is_dir():
        return 0

    days = max(1, int(retention_days))
    threshold = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime >= threshold:
            continue
        try:
            path.unlink()
            deleted += 1
        except Exception:
            continue
    return deleted


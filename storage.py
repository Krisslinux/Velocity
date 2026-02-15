import os
import shutil
from pathlib import Path
from datetime import datetime, timedelta

def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def user_dir(base: str, user_id: int) -> Path:
    return ensure_dir(Path(base) / str(user_id))

def work_dir(base: str, user_id: int) -> Path:
    return ensure_dir(user_dir(base, user_id) / "work")

def downloads_dir(base: str, user_id: int) -> Path:
    return ensure_dir(user_dir(base, user_id) / "downloads")

def output_dir(base: str, user_id: int) -> Path:
    return ensure_dir(user_dir(base, user_id) / "output")

def cleanup_old(base: str, older_than_hours: int = 24) -> int:
    """
    Deletes user folders older than threshold (based on folder mtime).
    Returns number of deleted items.
    """
    base_path = Path(base)
    if not base_path.exists():
        return 0

    cutoff = datetime.now() - timedelta(hours=older_than_hours)
    deleted = 0
    for child in base_path.iterdir():
        if not child.is_dir():
            continue
        mtime = datetime.fromtimestamp(child.stat().st_mtime)
        if mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
    return deleted

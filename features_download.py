import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Dict

import requests
import gdown

from utils import safe_filename

PIXELDRAIN_RE = re.compile(r"(?:https?://)?pixeldrain\.com/u/([A-Za-z0-9]+)")
YOUTUBE_RE = re.compile(r"(?:https?://)?(www\.)?(youtube\.com|youtu\.be)/")
GDRIVE_RE = re.compile(r"(?:https?://)?(drive\.google\.com)/")
MEGA_RE = re.compile(r"(?:https?://)?mega\.nz/")

def _requests_proxies(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}

def download_pixeldrain(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    m = PIXELDRAIN_RE.search(url.strip())
    if not m:
        raise ValueError("Not a valid PixelDrain link.")
    file_id = m.group(1)
    api = f"https://pixeldrain.com/api/file/{file_id}"
    dl = f"https://pixeldrain.com/api/file/{file_id}/download"

    r = requests.get(api, timeout=60, proxies=_requests_proxies(proxy))
    r.raise_for_status()
    info = r.json()
    filename = safe_filename(info.get("name", f"{file_id}.bin"))
    out_path = out_dir / filename

    with requests.get(dl, stream=True, timeout=60, proxies=_requests_proxies(proxy)) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return out_path

def download_gdrive(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    # gdown uses requests internally; setting env vars helps in many setups
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy

    # gdown will return the output file path
    out_dir.mkdir(parents=True, exist_ok=True)
    # Let gdown auto-name if possible
    out = gdown.download(url, output=str(out_dir), quiet=True, fuzzy=True)
    if not out:
        raise ValueError("Google Drive download failed (link may require permission).")
    return Path(out)

def download_youtube(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    """
    Uses yt-dlp to download best mp4 (fallback to best).
    For playlists, yt-dlp creates multiple files; we zip the folder at a higher layer if needed.
    Here we just download and return the directory path for playlist, or file path for single.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Output template
    tmpl = str(out_dir / "%(title).150s [%(id)s].%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-mtime",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", tmpl,
        url.strip(),
    ]
    if proxy:
        cmd.insert(1, "--proxy")
        cmd.insert(2, proxy)

    # Run
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{proc.stderr[-2000:]}")

    # Try to find most recent file in out_dir
    files = [p for p in out_dir.glob("*") if p.is_file()]
    if not files:
        # Possibly a playlist with nested behavior; still return dir
        return out_dir

    latest = max(files, key=lambda p: p.stat().st_mtime)
    return latest

def download_mega(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    """
    Uses 'mega.py' if installed.
    Proxy support may not work reliably here depending on library version.
    """
    from mega import Mega  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    mega = Mega()
    m = mega.login()
    # mega.py downloads into cwd by default; we pass destination_path if supported
    try:
        p = m.download_url(url.strip(), dest_path=str(out_dir))
        if not p:
            raise ValueError("MEGA download returned nothing.")
        return Path(p)
    except TypeError:
        # Older versions may not support dest_path
        cwd = os.getcwd()
        os.chdir(out_dir)
        try:
            p = m.download_url(url.strip())
            if not p:
                raise ValueError("MEGA download returned nothing.")
            return Path(out_dir) / Path(p).name
        finally:
            os.chdir(cwd)

def guess_downloader(url: str) -> str:
    u = url.strip().lower()
    if "pixeldrain.com" in u:
        return "pixeldrain"
    if "drive.google.com" in u:
        return "gdrive"
    if "mega.nz" in u:
        return "mega"
    if "youtu" in u or "youtube.com" in u:
        return "youtube"
    return "unknown"

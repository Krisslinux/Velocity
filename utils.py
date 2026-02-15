import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional, List, Tuple
import asyncio

from PIL import Image
from pypdf import PdfMerger
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

def safe_filename(name: str) -> str:
    name = name.strip().replace("\n", " ")
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:180] if len(name) > 180 else name

def human_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024*1024):.2f} MB"

def is_zip(path: Path) -> bool:
    try:
        return zipfile.is_zipfile(path)
    except Exception:
        return False

def zip_paths(paths: List[Path], out_zip: Path) -> Path:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            if p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        arc = str(sub.relative_to(p.parent))
                        zf.write(sub, arcname=arc)
            else:
                zf.write(p, arcname=p.name)
    return out_zip

def unzip_to_dir(zip_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(out_dir)
    return out_dir

def text_to_pdf(text: str, out_pdf: Path) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    width, height = A4
    x = 2 * cm
    y = height - 2 * cm
    line_height = 12

    # Basic wrap
    def wrap_line(s: str, max_chars: int = 95) -> list[str]:
        s = s.rstrip("\n")
        if len(s) <= max_chars:
            return [s]
        chunks = []
        while s:
            chunks.append(s[:max_chars])
            s = s[max_chars:]
        return chunks

    for raw_line in text.splitlines() or [""]:
        for line in wrap_line(raw_line):
            if y < 2 * cm:
                c.showPage()
                y = height - 2 * cm
            c.drawString(x, y, line)
            y -= line_height

    c.save()
    return out_pdf

def images_to_pdf(image_paths: List[Path], out_pdf: Path) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    imgs = []
    for p in image_paths:
        img = Image.open(p)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        else:
            img = img.convert("RGB")
        imgs.append(img)

    if not imgs:
        raise ValueError("No images provided")

    first, rest = imgs[0], imgs[1:]
    first.save(out_pdf, save_all=True, append_images=rest)
    return out_pdf

def merge_pdfs(pdf_paths: List[Path], out_pdf: Path) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    merger = PdfMerger()
    try:
        for p in pdf_paths:
            merger.append(str(p))
        merger.write(str(out_pdf))
    finally:
        merger.close()
    return out_pdf

async def run_blocking(func, *args, **kwargs):
    """Run blocking code safely without freezing the bot."""
    return await asyncio.to_thread(func, *args, **kwargs)

def remove_path(p: Path) -> None:
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass

def detect_content_type_from_name(name: str) -> Tuple[str, str]:
    """
    Returns (kind, extension) where kind in {"pdf","zip","image","text","unknown"}.
    """
    name_l = name.lower().strip()
    ext = Path(name_l).suffix
    if ext == ".pdf":
        return ("pdf", ext)
    if ext == ".zip":
        return ("zip", ext)
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"):
        return ("image", ext)
    if ext in (".txt", ".log", ".md", ".csv"):
        return ("text", ext)
    return ("unknown", ext)

import os
import re
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message, CallbackQuery, FSInputFile
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

import requests
import zipfile
from PIL import Image
from pypdf import PdfMerger
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
import subprocess

# -------------------- SETTINGS (Render Free friendly) --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()  # e.g. https://your-service.onrender.com
STORAGE_DIR = os.getenv("BOT_STORAGE_DIR", "/tmp/storage").strip()  # /tmp is fine on free tier
MAX_TG_FILE_MB = int(os.getenv("MAX_TG_FILE_MB", "200"))  # keep small on free
MAX_TEXT_LEN = int(os.getenv("MAX_TEXT_LEN", "100000"))   # keep small on free
FREE_MODE = os.getenv("FREE_MODE", "1").strip() == "1"    # 1 = block playlists & big stuff

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is missing (your Render public URL).")

BASE = Path(STORAGE_DIR)
BASE.mkdir(parents=True, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# -------------------- SIMPLE IN-MEMORY SESSION --------------------
USER_STATE: Dict[int, Dict[str, Any]] = {}
USER_PROXY: Dict[int, Optional[str]] = {}
MERGE_BUCKET: Dict[int, List[Path]] = {}
IMG_BUCKET: Dict[int, List[Path]] = {}

PROXY_RE = re.compile(r"^(http|https|socks5)://", re.I)

def safe_filename(name: str) -> str:
    name = (name or "").strip().replace("\n", " ")
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:180] if len(name) > 180 else name

def user_dirs(user_id: int):
    u = BASE / str(user_id)
    w = u / "work"
    d = u / "downloads"
    o = u / "output"
    for p in (w, d, o):
        p.mkdir(parents=True, exist_ok=True)
    return w, d, o

def set_state(user_id: int, menu: str, action: Optional[str], extra: Optional[Dict[str, Any]] = None):
    st = {"menu": menu, "action": action}
    if extra:
        st.update(extra)
    USER_STATE[user_id] = st

def get_state(user_id: int) -> Dict[str, Any]:
    return USER_STATE.get(user_id, {"menu": "main", "action": None})

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="File management", callback_data="menu:file")
    kb.button(text="Downloader", callback_data="menu:dl")
    kb.button(text="Proxies", callback_data="menu:proxy")
    kb.adjust(1)
    return kb.as_markup()

def file_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Zip", callback_data="file:zip")
    kb.button(text="Unzip", callback_data="file:unzip")
    kb.button(text="Rename", callback_data="file:rename")
    kb.button(text="Text → PDF", callback_data="file:text2pdf")
    kb.button(text="Image → PDF", callback_data="file:img2pdf")
    kb.button(text="Merge PDFs", callback_data="file:mergepdf")
    kb.button(text="⬅ Back", callback_data="menu:back")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()

def dl_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="YouTube (single video)", callback_data="dl:yt")
    kb.button(text="Google Drive", callback_data="dl:gdrive")
    kb.button(text="MEGA", callback_data="dl:mega")
    kb.button(text="PixelDrain", callback_data="dl:pixeldrain")
    kb.button(text="⬅ Back", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()

def proxy_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Set proxy", callback_data="proxy:set")
    kb.button(text="Clear proxy", callback_data="proxy:clear")
    kb.button(text="Show current", callback_data="proxy:show")
    kb.button(text="⬅ Back", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()

def merge_controls_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Done (merge now)", callback_data="merge:done")
    kb.button(text="🧹 Clear list", callback_data="merge:clear")
    kb.button(text="❌ Cancel", callback_data="merge:cancel")
    kb.adjust(1)
    return kb.as_markup()

def imgpdf_controls_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Done (convert now)", callback_data="imgpdf:done")
    kb.button(text="🧹 Clear list", callback_data="imgpdf:clear")
    kb.button(text="❌ Cancel", callback_data="imgpdf:cancel")
    kb.adjust(1)
    return kb.as_markup()

async def download_telegram_document(message: Message, dest: Path) -> Path:
    doc = message.document
    if not doc:
        raise ValueError("No file found.")
    max_bytes = MAX_TG_FILE_MB * 1024 * 1024
    if doc.file_size and doc.file_size > max_bytes:
        raise ValueError(f"File too large. Limit = {MAX_TG_FILE_MB} MB")
    tg_file = await bot.get_file(doc.file_id)
    await bot.download_file(tg_file.file_path, destination=str(dest))
    return dest

async def send_file(chat_id: int, path: Path, caption: str = ""):
    await bot.send_document(chat_id, FSInputFile(str(path)), caption=caption[:900])

def requests_proxies(proxy: Optional[str]):
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}

async def to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

# -------------------- FILE OPS --------------------
def zip_one_file(in_path: Path, out_zip: Path) -> Path:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(in_path, arcname=in_path.name)
    return out_zip

def unzip_and_rezip(zip_path: Path, out_zip: Path) -> Path:
    tmp = out_zip.parent / "unzipped_tmp"
    if tmp.exists():
        for p in tmp.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp)

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in tmp.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(tmp)))
    return out_zip

def text_to_pdf(text: str, out_pdf: Path) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    width, height = A4
    x = 2 * cm
    y = height - 2 * cm
    line_height = 12

    def wrap(s: str, max_chars: int = 95):
        s = s.rstrip("\n")
        if len(s) <= max_chars:
            return [s]
        return [s[i:i+max_chars] for i in range(0, len(s), max_chars)]

    for raw in text.splitlines() or [""]:
        for line in wrap(raw):
            if y < 2 * cm:
                c.showPage()
                y = height - 2 * cm
            c.drawString(x, y, line)
            y -= line_height

    c.save()
    return out_pdf

def images_to_pdf(images: List[Path], out_pdf: Path) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    imgs = []
    for p in images:
        img = Image.open(p)
        img = img.convert("RGB")
        imgs.append(img)
    if not imgs:
        raise ValueError("No images.")
    first, rest = imgs[0], imgs[1:]
    first.save(out_pdf, save_all=True, append_images=rest)
    return out_pdf

def merge_pdfs(pdfs: List[Path], out_pdf: Path) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    m = PdfMerger()
    try:
        for p in pdfs:
            m.append(str(p))
        m.write(str(out_pdf))
    finally:
        m.close()
    return out_pdf

# -------------------- DOWNLOADERS --------------------
def download_pixeldrain(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    m = re.search(r"(?:https?://)?pixeldrain\.com/u/([A-Za-z0-9]+)", url.strip())
    if not m:
        raise ValueError("Invalid PixelDrain link.")
    fid = m.group(1)
    api = f"https://pixeldrain.com/api/file/{fid}"
    dl = f"https://pixeldrain.com/api/file/{fid}/download"

    r = requests.get(api, timeout=60, proxies=requests_proxies(proxy))
    r.raise_for_status()
    info = r.json()
    name = safe_filename(info.get("name", f"{fid}.bin"))
    size = int(info.get("size", 0))

    # Free-tier protection
    if size and size > MAX_TG_FILE_MB * 1024 * 1024:
        raise ValueError("File too large for free hosting limits.")

    out = out_dir / name
    with requests.get(dl, stream=True, timeout=60, proxies=requests_proxies(proxy)) as resp:
        resp.raise_for_status()
        with open(out, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return out

def download_youtube_single(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    # Block playlists on free tier
    if FREE_MODE and ("list=" in url or "playlist" in url.lower()):
        raise ValueError("Playlists are disabled on Render Free. Send a single video link.")

    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "%(title).150s [%(id)s].%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-mtime",
        "--no-playlist",
        "--retries", "5",
        "--fragment-retries", "5",
        "--concurrent-fragments", "4",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", tmpl,
        url.strip(),
    ]
    if proxy:
        cmd.insert(1, "--proxy")
        cmd.insert(2, proxy)

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:] or "yt-dlp failed")

    files = [x for x in out_dir.glob("*") if x.is_file()]
    if not files:
        raise RuntimeError("No output from yt-dlp")
    return max(files, key=lambda x: x.stat().st_mtime)

def download_gdrive_basic(url: str, out_dir: Path, proxy: Optional[str]) -> Path:
    # Simple method: try direct via gdown if installed; otherwise fails.
    # Proxy is not safely supported per-user here on free single-process; keep it OFF to avoid leaking.
    import gdown
    out_dir.mkdir(parents=True, exist_ok=True)
    out = gdown.download(url, output=str(out_dir), quiet=True, fuzzy=True)
    if not out:
        raise ValueError("Google Drive download failed (permissions/link).")
    p = Path(out)
    if p.exists() and p.stat().st_size > MAX_TG_FILE_MB * 1024 * 1024:
        raise ValueError("Downloaded file is too large for free hosting limits.")
    return p

def download_mega_basic(url: str, out_dir: Path) -> Path:
    # mega.py can be fragile; this is “best effort” on free hosting.
    from mega import Mega  # mega.py
    out_dir.mkdir(parents=True, exist_ok=True)
    mega = Mega()
    m = mega.login()
    p = m.download_url(url.strip(), dest_path=str(out_dir))
    if not p:
        raise ValueError("MEGA download failed.")
    p = Path(p)
    if p.exists() and p.stat().st_size > MAX_TG_FILE_MB * 1024 * 1024:
        raise ValueError("Downloaded file is too large for free hosting limits.")
    return p

# -------------------- HANDLERS --------------------
@dp.message(CommandStart())
async def start(message: Message):
    set_state(message.from_user.id, "main", None)
    await message.answer("Choose an option:", reply_markup=main_menu_kb())

@dp.callback_query(F.data.startswith("menu:"))
async def menu_nav(q: CallbackQuery):
    user_id = q.from_user.id
    cmd = q.data.split(":", 1)[1]
    if cmd == "file":
        set_state(user_id, "file", None)
        await q.message.edit_text("File management:", reply_markup=file_menu_kb())
    elif cmd == "dl":
        set_state(user_id, "dl", None)
        await q.message.edit_text("Downloader:", reply_markup=dl_menu_kb())
    elif cmd == "proxy":
        set_state(user_id, "proxy", None)
        await q.message.edit_text("Proxies (optional):", reply_markup=proxy_menu_kb())
    else:
        set_state(user_id, "main", None)
        await q.message.edit_text("Choose an option:", reply_markup=main_menu_kb())
    await q.answer()

@dp.callback_query(F.data.startswith("proxy:"))
async def proxy_actions(q: CallbackQuery):
    user_id = q.from_user.id
    action = q.data.split(":", 1)[1]
    if action == "set":
        set_state(user_id, "proxy", "proxy_wait")
        await q.message.answer("Send proxy: http(s)://ip:port or socks5://ip:port")
    elif action == "clear":
        USER_PROXY.pop(user_id, None)
        await q.message.answer("Proxy cleared.")
    elif action == "show":
        await q.message.answer(f"Current proxy: {USER_PROXY.get(user_id) or 'None'}")
    await q.answer()

@dp.callback_query(F.data.startswith("file:"))
async def file_actions(q: CallbackQuery):
    user_id = q.from_user.id
    action = q.data.split(":", 1)[1]
    if action == "zip":
        set_state(user_id, "file", "zip_wait_file")
        await q.message.answer("Send ONE file to zip.")
    elif action == "unzip":
        set_state(user_id, "file", "unzip_wait_zip")
        await q.message.answer("Send a .zip to unzip (I will return a new zip).")
    elif action == "rename":
        set_state(user_id, "file", "rename_wait_file")
        await q.message.answer("Send the file to rename.")
    elif action == "text2pdf":
        set_state(user_id, "file", "text2pdf_wait_text")
        await q.message.answer("Send text to convert into PDF.")
    elif action == "img2pdf":
        IMG_BUCKET[user_id] = []
        set_state(user_id, "file", "img2pdf_collect")
        await q.message.answer("Send images, then press ✅ Done.", reply_markup=imgpdf_controls_kb())
    elif action == "mergepdf":
        MERGE_BUCKET[user_id] = []
        set_state(user_id, "file", "merge_collect")
        await q.message.answer("Send PDFs, then press ✅ Done.", reply_markup=merge_controls_kb())
    await q.answer()

@dp.callback_query(F.data.startswith("dl:"))
async def dl_actions(q: CallbackQuery):
    user_id = q.from_user.id
    action = q.data.split(":", 1)[1]
    if action == "yt":
        set_state(user_id, "dl", "dl_wait_yt")
        await q.message.answer("Send a YouTube single video link (no playlists on free).")
    elif action == "gdrive":
        set_state(user_id, "dl", "dl_wait_gdrive")
        await q.message.answer("Send a Google Drive link.")
    elif action == "mega":
        set_state(user_id, "dl", "dl_wait_mega")
        await q.message.answer("Send a MEGA link.")
    elif action == "pixeldrain":
        set_state(user_id, "dl", "dl_wait_pixeldrain")
        await q.message.answer("Send a PixelDrain link.")
    await q.answer()

@dp.callback_query(F.data.startswith("merge:"))
async def merge_controls(q: CallbackQuery):
    user_id = q.from_user.id
    cmd = q.data.split(":", 1)[1]
    if cmd == "clear":
        MERGE_BUCKET[user_id] = []
        await q.message.answer("Merge list cleared.")
    elif cmd == "cancel":
        MERGE_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
        await q.message.answer("Cancelled.", reply_markup=file_menu_kb())
    elif cmd == "done":
        pdfs = MERGE_BUCKET.get(user_id, [])
        if len(pdfs) < 2:
            await q.message.answer("Send at least 2 PDFs.")
            await q.answer()
            return
        if FREE_MODE and len(pdfs) > 10:
            await q.message.answer("Free mode: max 10 PDFs per merge.")
            await q.answer()
            return
        _, _, outdir = user_dirs(user_id)
        out = outdir / "merged.pdf"
        await q.message.answer("Merging…")
        await to_thread(merge_pdfs, pdfs, out)
        await send_file(q.message.chat.id, out, "Merged PDF ✅")
        MERGE_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
    await q.answer()

@dp.callback_query(F.data.startswith("imgpdf:"))
async def imgpdf_controls(q: CallbackQuery):
    user_id = q.from_user.id
    cmd = q.data.split(":", 1)[1]
    if cmd == "clear":
        IMG_BUCKET[user_id] = []
        await q.message.answer("Image list cleared.")
    elif cmd == "cancel":
        IMG_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
        await q.message.answer("Cancelled.", reply_markup=file_menu_kb())
    elif cmd == "done":
        imgs = IMG_BUCKET.get(user_id, [])
        if len(imgs) < 1:
            await q.message.answer("Send at least 1 image.")
            await q.answer()
            return
        if FREE_MODE and len(imgs) > 20:
            await q.message.answer("Free mode: max 20 images per PDF.")
            await q.answer()
            return
        _, _, outdir = user_dirs(user_id)
        out = outdir / "images.pdf"
        await q.message.answer("Converting…")
        await to_thread(images_to_pdf, imgs, out)
        await send_file(q.message.chat.id, out, "Image → PDF ✅")
        IMG_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
    await q.answer()

@dp.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
    st = get_state(user_id)
    action = st.get("action")
    proxy = USER_PROXY.get(user_id)

    if action == "proxy_wait":
        p = (message.text or "").strip()
        if not PROXY_RE.match(p):
            await message.reply("Invalid proxy. Must start with http:// https:// or socks5://")
            return
        USER_PROXY[user_id] = p
        set_state(user_id, "proxy", None)
        await message.reply("Proxy saved ✅")
        return

    # text -> pdf
    if action == "text2pdf_wait_text":
        txt = (message.text or "")
        if not txt.strip():
            await message.reply("Send some text.")
            return
        if len(txt) > MAX_TEXT_LEN:
            await message.reply("Text too long for free mode.")
            return
        _, _, outdir = user_dirs(user_id)
        out = outdir / "text.pdf"
        await message.answer("Creating PDF…")
        await to_thread(text_to_pdf, txt, out)
        await send_file(message.chat.id, out, "Text → PDF ✅")
        set_state(user_id, "file", None)
        return

    # rename flow
    if action == "rename_wait_newname":
        new_name = safe_filename(message.text or "")
        path_str = st.get("rename_path")
        if not new_name or not path_str:
            await message.reply("Rename failed. Try again.")
            set_state(user_id, "file", None)
            return
        p = Path(path_str)
        newp = p.with_name(new_name)
        p.rename(newp)
        await send_file(message.chat.id, newp, "Renamed ✅")
        set_state(user_id, "file", None)
        return

    # document flows
    if message.document:
        wdir, ddir, outdir = user_dirs(user_id)
        filename = safe_filename(message.document.file_name or "file.bin")
        local = wdir / filename

        if action == "zip_wait_file":
            await message.answer("Downloading…")
            await download_telegram_document(message, local)
            out = outdir / f"{Path(filename).stem}.zip"
            await message.answer("Zipping…")
            await to_thread(zip_one_file, local, out)
            await send_file(message.chat.id, out, "Zipped ✅")
            set_state(user_id, "file", None)
            return

        if action == "unzip_wait_zip":
            await message.answer("Downloading…")
            await download_telegram_document(message, local)
            if not zipfile.is_zipfile(local):
                await message.reply("Not a valid ZIP.")
                return
            out = outdir / f"{Path(filename).stem}_unzipped.zip"
            await message.answer("Unzipping + rezipping…")
            await to_thread(unzip_and_rezip, local, out)
            await send_file(message.chat.id, out, "Unzipped ✅")
            set_state(user_id, "file", None)
            return

        if action == "rename_wait_file":
            await message.answer("Downloading…")
            await download_telegram_document(message, local)
            set_state(user_id, "file", "rename_wait_newnam

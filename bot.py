import asyncio
import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import load_settings
from storage import work_dir, downloads_dir, output_dir, cleanup_old
from utils import (
    safe_filename, is_zip, detect_content_type_from_name, human_mb,
    run_blocking, remove_path
)
from features_file import (
    do_zip, do_unzip, do_rename, do_text_to_pdf, do_images_to_pdf, do_merge_pdfs
)
from features_download import (
    download_pixeldrain, download_gdrive, download_youtube, download_mega, guess_downloader
)

settings = load_settings()
bot = Bot(token=settings.bot_token)
dp = Dispatcher()

# ---- Simple in-memory session state (for production, move to Redis/DB) ----
USER_STATE: Dict[int, Dict[str, Any]] = {}
USER_PROXY: Dict[int, Optional[str]] = {}
MERGE_BUCKET: Dict[int, List[Path]] = {}
IMG_BUCKET: Dict[int, List[Path]] = {}

def set_state(user_id: int, menu: str, action: Optional[str], extra: Optional[Dict[str, Any]] = None):
    st = {"menu": menu, "action": action}
    if extra:
        st.update(extra)
    USER_STATE[user_id] = st

def get_state(user_id: int) -> Dict[str, Any]:
    return USER_STATE.get(user_id, {"menu": "main", "action": None})

# ---- Keyboards ----
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
    kb.button(text="Auto-detect link", callback_data="dl:auto")
    kb.button(text="YouTube (video/playlist)", callback_data="dl:yt")
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

# ---- Helpers ----
async def download_telegram_file(message: Message, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    file = message.document
    if not file:
        raise ValueError("No document found.")

    max_bytes = settings.max_telegram_file_mb * 1024 * 1024
    if file.file_size and file.file_size > max_bytes:
        raise ValueError(f"File too large: {human_mb(file.file_size)} (limit {settings.max_telegram_file_mb} MB).")

    tg_file = await bot.get_file(file.file_id)
    await bot.download_file(tg_file.file_path, destination=str(dest))
    return dest

async def send_file(chat_id: int, path: Path, caption: str = ""):
    if not path.exists():
        raise ValueError("Output file not found.")
    await bot.send_document(chat_id, FSInputFile(str(path)), caption=caption[:900])

def user_proxy(user_id: int) -> Optional[str]:
    return USER_PROXY.get(user_id)

# ---- Start / Menu ----
@dp.message(CommandStart())
async def start(message: Message):
    cleanup_old(settings.base_dir, older_than_hours=24)
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

# ---- Proxy ----
@dp.callback_query(F.data.startswith("proxy:"))
async def proxy_actions(q: CallbackQuery):
    user_id = q.from_user.id
    action = q.data.split(":", 1)[1]

    if action == "set":
        set_state(user_id, "proxy", "proxy_wait")
        await q.message.answer(
            "Send your proxy in one line.\n\nExamples:\n"
            "http://ip:port\n"
            "http://user:pass@ip:port\n"
            "socks5://ip:port"
        )
    elif action == "clear":
        USER_PROXY.pop(user_id, None)
        await q.message.answer("Proxy cleared.")
    elif action == "show":
        p = USER_PROXY.get(user_id)
        await q.message.answer(f"Current proxy: {p if p else 'None'}")
    await q.answer()

# ---- File management actions ----
@dp.callback_query(F.data.startswith("file:"))
async def file_actions(q: CallbackQuery):
    user_id = q.from_user.id
    action = q.data.split(":", 1)[1]

    if action == "zip":
        set_state(user_id, "file", "zip_wait_file")
        await q.message.answer("Send ONE file (or a ZIP). I will zip it (or re-zip after unzip).")
    elif action == "unzip":
        set_state(user_id, "file", "unzip_wait_zip")
        await q.message.answer("Send a .zip file to unzip (I will return a new .zip of extracted contents).")
    elif action == "rename":
        set_state(user_id, "file", "rename_wait_file")
        await q.message.answer("Send the file you want to rename.")
    elif action == "text2pdf":
        set_state(user_id, "file", "text2pdf_wait_text")
        await q.message.answer("Send the text (message) to convert into PDF.")
    elif action == "img2pdf":
        IMG_BUCKET[user_id] = []
        set_state(user_id, "file", "img2pdf_collect")
        await q.message.answer("Send image files (jpg/png/etc). When done, press ✅ Done.", reply_markup=imgpdf_controls_kb())
    elif action == "mergepdf":
        MERGE_BUCKET[user_id] = []
        set_state(user_id, "file", "merge_collect")
        await q.message.answer("Send PDF files to merge. When done, press ✅ Done.", reply_markup=merge_controls_kb())

    await q.answer()

# ---- Downloader actions ----
@dp.callback_query(F.data.startswith("dl:"))
async def dl_actions(q: CallbackQuery):
    user_id = q.from_user.id
    action = q.data.split(":", 1)[1]

    if action == "auto":
        set_state(user_id, "dl", "dl_wait_link_auto")
        await q.message.answer("Send a link (MEGA / Google Drive / YouTube / PixelDrain). I will auto-detect.")
    elif action == "yt":
        set_state(user_id, "dl", "dl_wait_link_yt")
        await q.message.answer("Send a YouTube video or playlist link.")
    elif action == "gdrive":
        set_state(user_id, "dl", "dl_wait_link_gdrive")
        await q.message.answer("Send a Google Drive share link.")
    elif action == "mega":
        set_state(user_id, "dl", "dl_wait_link_mega")
        await q.message.answer("Send a MEGA link.")
    elif action == "pixeldrain":
        set_state(user_id, "dl", "dl_wait_link_pixeldrain")
        await q.message.answer("Send a PixelDrain link.")
    await q.answer()

# ---- Merge / Image-PDF controls ----
@dp.callback_query(F.data.startswith("merge:"))
async def merge_controls(q: CallbackQuery):
    user_id = q.from_user.id
    cmd = q.data.split(":", 1)[1]

    if cmd == "clear":
        MERGE_BUCKET[user_id] = []
        await q.message.answer("Merge list cleared. Send PDFs again.")
    elif cmd == "cancel":
        MERGE_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
        await q.message.answer("Merge cancelled.", reply_markup=file_menu_kb())
    elif cmd == "done":
        pdfs = MERGE_BUCKET.get(user_id, [])
        if len(pdfs) < 2:
            await q.message.answer("Send at least 2 PDFs before merging.")
            await q.answer()
            return
        out = output_dir(settings.base_dir, user_id) / safe_filename("merged.pdf")
        try:
            await q.message.answer("Merging PDFs…")
            await run_blocking(do_merge_pdfs, pdfs, out)
            await send_file(q.message.chat.id, out, caption="Merged PDF ✅")
        finally:
            # cleanup session
            MERGE_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
    await q.answer()

@dp.callback_query(F.data.startswith("imgpdf:"))
async def imgpdf_controls(q: CallbackQuery):
    user_id = q.from_user.id
    cmd = q.data.split(":", 1)[1]

    if cmd == "clear":
        IMG_BUCKET[user_id] = []
        await q.message.answer("Image list cleared. Send images again.")
    elif cmd == "cancel":
        IMG_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
        await q.message.answer("Image→PDF cancelled.", reply_markup=file_menu_kb())
    elif cmd == "done":
        imgs = IMG_BUCKET.get(user_id, [])
        if len(imgs) < 1:
            await q.message.answer("Send at least 1 image first.")
            await q.answer()
            return
        out = output_dir(settings.base_dir, user_id) / safe_filename("images.pdf")
        try:
            await q.message.answer("Converting images to PDF…")
            await run_blocking(do_images_to_pdf, imgs, out)
            await send_file(q.message.chat.id, out, caption="Image → PDF ✅")
        finally:
            IMG_BUCKET.pop(user_id, None)
        set_state(user_id, "file", None)
    await q.answer()

# ---- Main message handler ----
PROXY_RE = re.compile(r"^(http|https|socks5)://", re.I)

@dp.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
    st = get_state(user_id)
    action = st.get("action")
    proxy = user_proxy(user_id)

    # --- proxy input ---
    if action == "proxy_wait":
        p = (message.text or "").strip()
        if not PROXY_RE.match(p):
            await message.reply("Proxy must start with http://, https://, or socks5://")
            return
        USER_PROXY[user_id] = p
        set_state(user_id, "proxy", None)
        await message.reply("Proxy saved ✅")
        return

    # --- text -> pdf ---
    if action == "text2pdf_wait_text":
        txt = (message.text or "")
        if not txt.strip():
            await message.reply("Send some text (not empty).")
            return
        if len(txt) > settings.max_text_len:
            await message.reply(f"Text too long (limit {settings.max_text_len} characters).")
            return
        out = output_dir(settings.base_dir, user_id) / safe_filename("text.pdf")
        await message.answer("Creating PDF…")
        await run_blocking(do_text_to_pdf, txt, out)
        await send_file(message.chat.id, out, caption="Text → PDF ✅")
        set_state(user_id, "file", None)
        return

    # --- document handling ---
    if message.document:
        filename = message.document.file_name or "file.bin"
        kind, ext = detect_content_type_from_name(filename)

        wdir = work_dir(settings.base_dir, user_id)
        ddir = downloads_dir(settings.base_dir, user_id)
        odir = output_dir(settings.base_dir, user_id)

        local_path = wdir / safe_filename(filename)

        # zip
        if action == "zip_wait_file":
            await message.answer("Downloading file…")
            await download_telegram_file(message, local_path)
            out_zip = odir / safe_filename(f"{Path(filename).stem}.zip")
            await message.answer("Zipping…")
            await run_blocking(do_zip, [local_path], out_zip)
            await send_file(message.chat.id, out_zip, caption="Zipped ✅")
            set_state(user_id, "file", None)
            return

        # unzip
        if action == "unzip_wait_zip":
            await message.answer("Downloading ZIP…")
            await download_telegram_file(message, local_path)
            if not is_zip(local_path):
                await message.reply("That file is not a valid ZIP.")
                return
            extracted = wdir / "unzipped"
            await message.answer("Unzipping…")
            await run_blocking(do_unzip, local_path, extracted)
            out_zip = odir / safe_filename(f"{Path(filename).stem}_unzipped.zip")
            await message.answer("Re-zipping extracted folder…")
            await run_blocking(do_zip, [extracted], out_zip)
            await send_file(message.chat.id, out_zip, caption="Unzipped + rezipped ✅")
            set_state(user_id, "file", None)
            return

        # rename
        if action == "rename_wait_file":
            await message.answer("Downloading file…")
            await download_telegram_file(message, local_path)
            set_state(user_id, "file", "rename_wait_newname", {"rename_path": str(local_path)})
            await message.answer("Now send the NEW filename (example: newname.pdf)")
            return

        # merge pdf collect
        if action == "merge_collect":
            if kind != "pdf":
                await message.reply("Please send only PDF files for merging.")
                return
            await message.answer("Downloading PDF…")
            await download_telegram_file(message, local_path)
            MERGE_BUCKET.setdefault(user_id, []).append(local_path)
            await message.answer(f"Added ✅ (total: {len(MERGE_BUCKET[user_id])})", reply_markup=merge_controls_kb())
            return

        # image->pdf collect
        if action == "img2pdf_collect":
            if kind != "image":
                await message.reply("Please send only image files (jpg/png/etc).")
                return
            await message.answer("Downloading image…")
            await download_telegram_file(message, local_path)
            IMG_BUCKET.setdefault(user_id, []).append(local_path)
            await message.answer(f"Added ✅ (total: {len(IMG_BUCKET[user_id])})", reply_markup=imgpdf_controls_kb())
            return

        # If user sends a file without choosing action
        await message.reply("Use the buttons to pick a task first.", reply_markup=main_menu_kb())
        return

    # --- rename new name text ---
    if action == "rename_wait_newname":
        new_name = (message.text or "").strip()
        if not new_name:
            await message.reply("Send a valid filename (example: myfile.pdf)")
            return
        path_str = st.get("rename_path")
        if not path_str:
            set_state(user_id, "file", None)
            await message.reply("Rename session expired. Try again.")
            return
        fpath = Path(path_str)
        try:
            new_path = await run_blocking(do_rename, fpath, new_name)
        except Exception as e:
            await message.reply(f"Rename failed: {e}")
            return
        await send_file(message.chat.id, new_path, caption="Renamed ✅")
        set_state(user_id, "file", None)
        return

    # --- downloader links ---
    if action and action.startswith("dl_wait_link"):
        url = (message.text or "").strip()
        if not url:
            await message.reply("Send a valid link.")
            return

        ddir = downloads_dir(settings.base_dir, user_id)
        odir = output_dir(settings.base_dir, user_id)

        mode = action.replace("dl_wait_link_", "")
        if mode == "auto":
            mode = guess_downloader(url)

        try:
            await message.answer("Working on it…")

            if mode == "pixeldrain":
                p = await run_blocking(download_pixeldrain, url, ddir, proxy)
                await send_file(message.chat.id, p, caption="PixelDrain download ✅")

            elif mode == "gdrive":
                p = await run_blocking(download_gdrive, url, ddir, proxy)
                await send_file(message.chat.id, p, caption="Google Drive download ✅")

            elif mode == "youtube" or mode == "yt":
                # yt-dlp will download video(s). If playlist, often multiple files → zip the folder.
                yt_out = await run_blocking(download_youtube, url, ddir, proxy)
                if yt_out.is_dir():
                    out_zip = odir / safe_filename("youtube_playlist.zip")
                    await message.answer("Playlist detected → zipping…")
                    await run_blocking(do_zip, [yt_out], out_zip)
                    await send_file(message.chat.id, out_zip, caption="YouTube playlist ZIP ✅")
                else:
                    await send_file(message.chat.id, yt_out, caption="YouTube download ✅")

            elif mode == "mega":
                # Proxy support may not be reliable for MEGA library
                p = await run_blocking(download_mega, url, ddir, proxy)
                await send_file(message.chat.id, p, caption="MEGA download ✅")

            else:
                await message.reply("I couldn’t detect that link type. Use the Downloader menu to choose one.")
                return

        except Exception as e:
            await message.reply(f"Download failed:\n{str(e)[:3500]}")
        finally:
            set_state(user_id, "dl", None)
        return

    # Default
    await message.reply("Use the buttons to choose a task.", reply_markup=main_menu_kb())

async def main():
    cleanup_old(settings.base_dir, older_than_hours=24)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

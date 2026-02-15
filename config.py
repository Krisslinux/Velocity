import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    bot_token: str
    base_dir: str
    max_telegram_file_mb: int
    max_text_len: int

def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN env var is missing. Set it before running.")

    base_dir = os.getenv("BOT_STORAGE_DIR", "./storage").strip()
    max_mb = int(os.getenv("MAX_TG_FILE_MB", "1900"))  # Telegram Bot API limits vary by server; keep < 2GB
    max_text_len = int(os.getenv("MAX_TEXT_LEN", "200000"))  # for text->pdf

    return Settings(
        bot_token=token,
        base_dir=base_dir,
        max_telegram_file_mb=max_mb,
        max_text_len=max_text_len,
    )

import os
from pathlib import Path

# Pure-Python .env loader — no external library required
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"  # not the deepseek-chat alias — that's deprecated 2026-07-24

ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "claude")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Bot API 10.1 sendRichMessage isn't wrapped by python-telegram-bot yet, so we call it
# over raw HTTP. If it ever fails (rate limit, client doesn't support it, schema drift
# in a future Telegram update), we fall back to plain MarkdownV2 automatically.
USE_RICH_MESSAGES = True


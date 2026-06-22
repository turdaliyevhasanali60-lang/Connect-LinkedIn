import logging
import httpx

from config import TELEGRAM_API_BASE, USE_RICH_MESSAGES

logger = logging.getLogger(__name__)



async def send_report(chat_id: int, markdown: str) -> None:
    """Send the assessment. Tries Bot API 10.1 Rich Message (tables render natively),
    falls back to plain MarkdownV2, and ultimately falls back to plain unformatted text."""
    if USE_RICH_MESSAGES:
        try:
            await _send_rich(chat_id, markdown)
            return
        except Exception as e:
            logger.warning(f"sendRichMessage failed: {e}. Trying MarkdownV2...")

    try:
        await _send_markdown_v2(chat_id, markdown)
        return
    except Exception as e:
        logger.warning(f"sendMessage MarkdownV2 failed: {e}. Trying plain text fallback...")

    # Ultimate fallback: split into 4000-char chunks and send plain text
    chunks = [markdown[i:i + 4000] for i in range(0, len(markdown), 4000)]
    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in chunks:
            try:
                resp = await client.post(
                    f"{TELEGRAM_API_BASE}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk},
                )
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Plain text chunk send failed: {e}")
                break



async def _send_rich(chat_id: int, markdown: str) -> None:
    # pip install telegramify-markdown — richify() builds the InputRichMessage payload
    # for Bot API 10.1's sendRichMessage, which PTB v22.8 doesn't wrap natively yet.
    from telegramify_markdown import richify

    rich_message = richify(markdown)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TELEGRAM_API_BASE}/sendRichMessage",
            json={"chat_id": chat_id, "rich_message": rich_message.to_dict()},
        )
        resp.raise_for_status()


async def _send_markdown_v2(chat_id: int, markdown: str) -> None:
    from telegramify_markdown import convert

    text, entities = convert(markdown)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "entities": [e.to_dict() for e in entities]},
        )
        resp.raise_for_status()

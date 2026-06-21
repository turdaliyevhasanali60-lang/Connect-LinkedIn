import logging
import httpx

from config import TELEGRAM_API_BASE, USE_RICH_MESSAGES

logger = logging.getLogger(__name__)


I18N = {
    "en": {
        "title": "LinkedIn Assessment — {name}",
        "overall": "Overall Score: {score} / 100",
        "pillar1": "Pillar 1 — Profile",
        "pillar2": "Pillar 2 — Connections",
        "pillar3": "Pillar 3 — Updates",
        "section": "Section", "score": "Score", "status": "Status",
        "headline": "Headline", "about": "About", "completeness": "Completeness",
        "fixes": "What to fix:",
        "priority": "Top priority this week: {text}",
        "strong": "✅ Strong", "good": "✅ Good", "needs_work": "⚠️ Needs work", "weak": "❌ Weak",
    },
    "ru": {
        "title": "Оценка профиля LinkedIn — {name}",
        "overall": "Итоговый балл: {score} / 100",
        "pillar1": "Раздел 1 — Профиль",
        "pillar2": "Раздел 2 — Связи",
        "pillar3": "Раздел 3 — Активность",
        "section": "Параметр", "score": "Балл", "status": "Статус",
        "headline": "Заголовок", "about": "О себе", "completeness": "Заполненность",
        "fixes": "Что исправить:",
        "priority": "Главный приоритет на этой неделе: {text}",
        "strong": "✅ Сильно", "good": "✅ Хорошо", "needs_work": "⚠️ Нужна работа", "weak": "❌ Слабо",
    },
    "uz": {
        "title": "LinkedIn profilini baholash — {name}",
        "overall": "Umumiy ball: {score} / 100",
        "pillar1": "1-bo'lim — Profil",
        "pillar2": "2-bo'lim — Aloqalar",
        "pillar3": "3-bo'lim — Faollik",
        "section": "Bo'lim", "score": "Ball", "status": "Holat",
        "headline": "Sarlavha", "about": "Men haqimda", "completeness": "To'liqlik",
        "fixes": "Nimani tuzatish kerak:",
        "priority": "Shu hafta uchun asosiy ustuvorlik: {text}",
        "strong": "✅ Kuchli", "good": "✅ Yaxshi", "needs_work": "⚠️ Ishlash kerak", "weak": "❌ Zaif",
    },
}


def _status_key(score: int, max_score: int) -> str:
    ratio = score / max_score
    if ratio >= 0.8:
        return "good" if max_score == 10 else "strong"
    if ratio >= 0.5:
        return "needs_work"
    return "weak"


def build_report_markdown(lang: str, name: str, assessment: dict, p2_score: int, p3_score: int) -> str:
    t = I18N.get(lang, I18N["en"])
    p1 = assessment["headline_score"] + assessment["about_score"] + assessment["completeness_score"]
    overall = p1 + p2_score + p3_score

    headline_status = t[_status_key(assessment["headline_score"], 15)]
    about_status = t[_status_key(assessment["about_score"], 15)]
    completeness_status = t[_status_key(assessment["completeness_score"], 10)]

    fixes = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(assessment.get("fixes", [])))

    return f"""# {t['title'].format(name=name)}
## {t['overall'].format(score=overall)}

---

## {t['pillar1']}  [{p1} / 40]

| {t['section']} | {t['score']} | {t['status']} |
|---|---|---|
| {t['headline']} | {assessment['headline_score']}/15 | {headline_status} |
| {t['about']} | {assessment['about_score']}/15 | {about_status} |
| {t['completeness']} | {assessment['completeness_score']}/10 | {completeness_status} |

{t['fixes']}
{fixes}

---

## {t['pillar2']}  [{p2_score} / 30]
## {t['pillar3']}  [{p3_score} / 30]

---

> 🎯 {t['priority'].format(text=assessment.get('top_priority', ''))}
"""


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

    # Ultimate fallback: send plain text directly so user gets the report regardless of markdown parsing issues
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{TELEGRAM_API_BASE}/sendMessage",
                json={"chat_id": chat_id, "text": markdown},
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Ultimate plain text fallback failed: {e}")



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

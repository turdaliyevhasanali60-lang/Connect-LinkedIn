import asyncio
import logging
import os
import re
import tempfile
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

from assess import assess_profile
from config import TELEGRAM_BOT_TOKEN
from pdf_parser import extract_pdf_text, guess_name
from rich_output import send_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def clean_markdown_for_streaming(text: str) -> str:
    # Remove header hashes at the beginning of any line
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    # Remove bold asterisks
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*\*([^*]*)$', r'\1', text)
    # Convert list asterisks to bullets
    text = re.sub(r'^\*\s+', '• ', text, flags=re.MULTILINE)
    # Remove remaining single asterisks
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'\*([^*]*)$', r'\1', text)
    # Remove backticks
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'`([^`]*)$', r'\1', text)
    # Convert dash lists to bullets
    text = re.sub(r'^-\s+', '• ', text, flags=re.MULTILINE)
    return text


# Conversation states
AWAITING_INPUT, AWAITING_ABOUT, AWAITING_PHOTO, AWAITING_BANNER, AWAITING_URL, AWAITING_EXPERIENCE, AWAITING_EDUCATION, AWAITING_OTW, AWAITING_SKILLS_COUNT, AWAITING_CONNECTIONS, AWAITING_POSTING, AWAITING_SKILLS, AWAITING_MEMBERSHIP = range(13)

MAX_USES = 3
CHANNEL_USERNAME = "@itcommunityuzb"


async def _is_channel_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ("member", "creator", "administrator")
    except Exception:
        return False


def _clear_flow_data(user_data: dict) -> None:
    """Clear per-assessment keys while preserving the use counter."""
    ai_uses = user_data.get("ai_uses", 0)
    user_data.clear()
    user_data["ai_uses"] = ai_uses
    user_data["lang"] = "en"


def _welcome_text(ai_uses: int) -> str:
    remaining = MAX_USES - ai_uses
    return (
        "👋 Welcome to the *Connect! LinkedIn Assessment Bot*.\n\n"
        f"📊 You have *{remaining}* assessment{'s' if remaining != 1 else ''} remaining.\n\n"
        "I'll score your profile against Shavkat Karimov's Connect! Tour framework.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💻 *On Desktop (best results):*\n"
        "1. Open your LinkedIn profile\n"
        "2. Click the *•••* button (next to \"Enhance profile\")\n"
        "3. Click *Save to PDF*\n"
        "4. Send me that PDF file right here\n\n"
        "📱 *On Mobile:*\n"
        "Just paste your *headline* as a text message — "
        "I'll guide you from there."
    )


async def _send_welcome(bot, chat_id: int, ai_uses: int) -> None:
    photo_path = os.path.join(os.path.dirname(__file__), "SavePDF.jpg")
    if os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=_welcome_text(ai_uses), parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=chat_id, text=_welcome_text(ai_uses), parse_mode="Markdown")


async def _send_banner_question(bot, chat_id: int) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Custom professional banner", callback_data="btn_custom")],
        [InlineKeyboardButton("🌐 Default / generic banner", callback_data="btn_default")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_banner")],
    ])
    photo_path = os.path.join(os.path.dirname(__file__), "bannerExample.jpg")
    caption = "🖼️ **Pillar 1: Background Banner**\n\nWhat does your background banner look like?"
    if os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=caption, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard, parse_mode="Markdown")


async def _send_otw_question(bot, chat_id: int) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Recruiters only", callback_data="btn_recruiters_only")],
        [InlineKeyboardButton("🟢 All LinkedIn members (Green badge)", callback_data="btn_all_linkedin")],
        [InlineKeyboardButton("🏢 Currently employed — not job seeking", callback_data="btn_employed")],
        [InlineKeyboardButton("❌ OFF / Not set (actively job seeking)", callback_data="btn_off")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_otw")],
    ])
    photo_path = os.path.join(os.path.dirname(__file__), "opentowork-linkedin.png")
    caption = "💼 **Open to Work Status**\n\nWhat is your current Open to Work visibility?"
    if os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=caption, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard, parse_mode="Markdown")


# ── Handlers ──────────────────────────────────────────────────────────────────

def _join_prompt() -> tuple:
    """Returns (text, reply_markup) for the channel-join gate message."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Join @itcommunityuzb", url="https://t.me/itcommunityuzb"),
        InlineKeyboardButton("✅ I've Joined", callback_data="check_membership"),
    ]])
    text = (
        "👋 Welcome!\n\n"
        "To use this bot you need to be a member of our channel:\n"
        "👉 @itcommunityuzb\n\n"
        "Join the channel, then tap *I've Joined* below."
    )
    return text, keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.message or (update.callback_query and update.callback_query.message)
    if not target:
        return AWAITING_INPUT

    user_id = update.effective_user.id
    if not await _is_channel_member(context.bot, user_id):
        text, keyboard = _join_prompt()
        await target.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return AWAITING_MEMBERSHIP

    ai_uses = context.user_data.get("ai_uses", 0)
    if ai_uses >= MAX_USES:
        await target.reply_text(
            f"⛔ You've used all *{MAX_USES}* assessments.\n\nContact us for more access.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    _clear_flow_data(context.user_data)
    await _send_welcome(context.bot, target.chat_id, ai_uses)
    return AWAITING_INPUT


async def check_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if not await _is_channel_member(context.bot, user_id):
        text, keyboard = _join_prompt()
        await query.edit_message_text(
            "❌ You're not a member yet.\n\n" + text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_MEMBERSHIP

    ai_uses = context.user_data.get("ai_uses", 0)
    if ai_uses >= MAX_USES:
        await query.edit_message_text(
            f"⛔ You've used all *{MAX_USES}* assessments.\n\nContact us for more access.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    _clear_flow_data(context.user_data)
    try:
        await query.message.delete()
    except Exception:
        pass
    await _send_welcome(context.bot, query.message.chat_id, ai_uses)
    return AWAITING_INPUT


# ── AWAITING_INPUT: two parallel handlers ─────────────────────────────────────

async def input_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User pasted their headline as text → store it, ask for About."""
    headline = update.message.text.strip()
    if not headline:
        await update.message.reply_text("Please paste your LinkedIn headline.")
        return AWAITING_INPUT

    context.user_data["headline_text"] = headline
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Go Back", callback_data="back_about")]])
    await update.message.reply_text(
        "Got your headline ✅\n\nNow paste your *About / Summary* section below:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_ABOUT


async def input_pdf_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User uploaded a file → try to extract PDF text, then skip to completeness."""
    document = update.message.document

    if not document or not document.file_name.lower().endswith(".pdf"):
        await update.message.reply_text(
            "I couldn't read that file. Please make sure it's a valid PDF.\n\n"
            "Or just *paste your headline* as a text message instead.",
            parse_mode="Markdown",
        )
        return AWAITING_INPUT

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    pdf_path = None
    try:
        tg_file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
        await tg_file.download_to_drive(pdf_path)
        pdf_text = extract_pdf_text(pdf_path)
    except Exception:
        logger.exception("Failed to download or parse PDF export")
        await update.message.reply_text(
            "I couldn't read that file. Please make sure it's a valid PDF.\n\n"
            "Or just *paste your headline* as a text message instead.",
            parse_mode="Markdown",
        )
        return AWAITING_INPUT
    finally:
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.unlink(pdf_path)
            except Exception:
                pass

    if not pdf_text or len(pdf_text.strip()) < 50:
        await update.message.reply_text(
            "The PDF seems empty or too short. Please try again or *paste your headline* instead.",
            parse_mode="Markdown",
        )
        return AWAITING_INPUT

    if "linkedin" not in pdf_text.lower()[:1000]:
        await update.message.reply_text(
            "⚠️ This doesn't look like a LinkedIn PDF export.\n\n"
            "On desktop: open your profile → *•••* → *Save to PDF*.\n\n"
            "Or paste your *headline* as a text message instead.",
            parse_mode="Markdown",
        )
        return AWAITING_INPUT

    context.user_data["pdf_text"] = pdf_text
    context.user_data["name"] = guess_name(pdf_text)

    # PDF path skips About → straight to photo
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Professional Headshot (smiling, clean background)", callback_data="btn_professional")],
        [InlineKeyboardButton("🤳 Casual / Crop (selfie, group photo, busy bg)", callback_data="btn_casual")],
        [InlineKeyboardButton("❌ No Photo / Default Avatar", callback_data="btn_none")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_photo")],
    ])
    photo_path = os.path.join(os.path.dirname(__file__), "Photo_example.jpeg")
    if os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="📸 **Pillar 1: Profile Photo**\n\nChoose the description that fits your profile photo best (be honest!):",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            "📸 **Pillar 1: Profile Photo**\n\nChoose the description that fits your profile photo best (be honest!):",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    return AWAITING_PHOTO


# ── AWAITING_ABOUT (text path only) ───────────────────────────────────────────

async def about_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store About text, then ask completeness."""
    about = update.message.text.strip()
    if not about:
        await update.message.reply_text("Please paste your About / Summary section.")
        return AWAITING_ABOUT

    context.user_data["about_text"] = about

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Professional Headshot (smiling, clean background)", callback_data="btn_professional")],
        [InlineKeyboardButton("🤳 Casual / Crop (selfie, group photo, busy bg)", callback_data="btn_casual")],
        [InlineKeyboardButton("❌ No Photo / Default Avatar", callback_data="btn_none")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_photo")],
    ])
    photo_path = os.path.join(os.path.dirname(__file__), "Photo_example.jpeg")
    if os.path.exists(photo_path):
        with open(photo_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="📸 **Pillar 1: Profile Photo**\n\nChoose the description that fits your profile photo best (be honest!):",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            "📸 **Pillar 1: Profile Photo**\n\nChoose the description that fits your profile photo best (be honest!):",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    return AWAITING_PHOTO


# ── Converged path: completeness → connections → posting → report ─────────────

async def photo_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["photo"] = query.data.split("_", 1)[1]

    # Delete the photo message to avoid edit errors
    try:
        await query.message.delete()
    except Exception:
        pass

    await _send_banner_question(context.bot, query.message.chat_id)
    return AWAITING_BANNER


async def banner_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["banner"] = query.data.split("_", 1)[1]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Clean URL (e.g. /in/yourname)", callback_data="btn_clean")],
        [InlineKeyboardButton("❌ Default URL (containing random digits)", callback_data="btn_default")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_url")],
    ])
    try:
        await query.edit_message_text(
            "🔗 **Pillar 1: Custom URL**\n\n"
            "Is your profile URL cleaned up?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🔗 **Pillar 1: Custom URL**\n\nIs your profile URL cleaned up?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    return AWAITING_URL


async def url_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["url"] = query.data.split("_", 1)[1]

    if "pdf_text" in context.user_data:
        # Skip Experience and Education quiz in PDF path, go to OTW
        try:
            await query.message.delete()
        except Exception:
            pass
        await _send_otw_question(context.bot, query.message.chat_id)
        return AWAITING_OTW

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Quantified results & achievements", callback_data="btn_quantified")],
        [InlineKeyboardButton("📝 Plain list of job duties", callback_data="btn_plain")],
        [InlineKeyboardButton("❌ Only job titles, no description", callback_data="btn_none")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_experience")],
    ])
    await query.edit_message_text(
        "💼 **Pillar 1: Experience Section**\n\n"
        "How are your job experience descriptions written?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_EXPERIENCE


async def experience_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["experience"] = query.data.split("_", 1)[1]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎓 Yes, fully listed (with degree & details)", callback_data="btn_fully_listed")],
        [InlineKeyboardButton("📝 Listed with only university name (no details)", callback_data="btn_only_name")],
        [InlineKeyboardButton("❌ No education listed", callback_data="btn_none")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_education")],
    ])
    await query.edit_message_text(
        "🎓 **Education Section**\n\n"
        "Do you have your education listed on your profile?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_EDUCATION


async def education_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["education"] = query.data.split("_", 1)[1]

    try:
        await query.message.delete()
    except Exception:
        pass
    await _send_otw_question(context.bot, query.message.chat_id)
    return AWAITING_OTW


async def otw_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["otw"] = query.data.split("_", 1)[1]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("50+ skills", callback_data="btn_over_50")],
        [InlineKeyboardButton("20–50 skills", callback_data="btn_20_to_50")],
        [InlineKeyboardButton("Fewer than 20", callback_data="btn_under_20")],
        [InlineKeyboardButton("No skills listed", callback_data="btn_none")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_skills_count")],
    ])
    await query.edit_message_text(
        "💪 **Number of Skills**\n\n"
        "How many skills do you have listed in your LinkedIn Profile's Skills section?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_SKILLS_COUNT


async def skills_count_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["skills_count"] = query.data.split("_", 1)[1]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("500+ connections", callback_data="btn_500_plus")],
        [InlineKeyboardButton("100–500 connections", callback_data="btn_100_500")],
        [InlineKeyboardButton("Under 100 connections", callback_data="btn_under_100")],
        [InlineKeyboardButton("0–10 connections", callback_data="btn_0_10")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_connections")],
    ])
    await query.edit_message_text(
        "👥 **Pillar 2: Connections**\n\n"
        "How many connections do you have on LinkedIn?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_CONNECTIONS


async def connections_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["connections"] = query.data.split("_", 1)[1]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("This week", callback_data="btn_this_week")],
        [InlineKeyboardButton("This month", callback_data="btn_this_month")],
        [InlineKeyboardButton("Over a month ago", callback_data="btn_over_month")],
        [InlineKeyboardButton("Never posted", callback_data="btn_never")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_posting")],
    ])
    await query.edit_message_text(
        "📝 **Pillar 3: Activity & Updates**\n\n"
        "When did you last post on LinkedIn?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_POSTING


async def posting_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user to list their skills before generating the report."""
    query = update.callback_query
    await query.answer()
    context.user_data["posting"] = query.data.split("_", 1)[1]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_skills")],
    ])
    await query.edit_message_text(
        "💪 *Last step:* Please list your top 5–10 key skills, tools, or technologies "
        "(separated by commas, e.g., Python, SQL, Project Management) so we can evaluate your keyword indexing:\n\n"
        "💡 *Tip:* While we only need your top 5–10 key skills *here* to run the simulation, Shavkat Karimov recommends listing 20 to 50+ skills on your actual LinkedIn profile to maximize search indexability!",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAITING_SKILLS


async def skills_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Final step: save skills, run the streaming LLM assessment, and deliver the report."""
    skills_text = update.message.text.strip() if update.message else ""
    if not skills_text:
        await update.message.reply_text("Please enter at least a few skills or tools.")
        return AWAITING_SKILLS

    context.user_data["skills_text"] = skills_text
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    message = await update.message.reply_text("⏳ Analyzing your profile...")

    # Keep the typing indicator alive for the full duration of the LLM call
    # (Telegram's typing action expires after 5 seconds)
    stop_typing = asyncio.Event()

    async def _keep_typing():
        while not stop_typing.is_set():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(_keep_typing())

    full_text = ""
    last_update_time = time.time()

    try:
        async for chunk in assess_profile(context.user_data):
            full_text += chunk
            now = time.time()
            if now - last_update_time > 1.5 and len(full_text) > 10:
                try:
                    cleaned_preview = clean_markdown_for_streaming(full_text[:4000])
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message.message_id,
                        text=cleaned_preview + " ░",
                    )
                except Exception:
                    pass
                last_update_time = now
    except Exception:
        logger.exception("LLM assessment stream failed")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text="⚠️ Assessment failed. Please try /start again.",
            )
        except Exception:
            pass
        return ConversationHandler.END
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    if not full_text.strip():
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text="⚠️ The assessment returned empty. Please try /start again.",
            )
        except Exception:
            pass
        return ConversationHandler.END

    # Delete the streaming placeholder and send the final formatted report
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception:
        pass

    await send_report(chat_id, full_text)

    # Count this as a successful AI use (only incremented on success)
    context.user_data["ai_uses"] = context.user_data.get("ai_uses", 0) + 1
    remaining = MAX_USES - context.user_data["ai_uses"]

    keyboard = [[InlineKeyboardButton("🔁 Reassess", callback_data="restart")]]
    if remaining > 0:
        await context.bot.send_message(
            chat_id,
            f"📊 You have *{remaining}* assessment{'s' if remaining != 1 else ''} remaining. Send /start to reassess.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"⛔ You've used all *{MAX_USES}* of your assessments. Contact us for more access.",
            parse_mode="Markdown",
        )
    return ConversationHandler.END


# ── Nudge helpers ─────────────────────────────────────────────────────────────

async def _buttons_only_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sent when a user types text while the bot is waiting for a button tap."""
    await update.message.reply_text("👆 Please use the buttons above to continue.")


async def _doc_in_about_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sent when a user uploads a file while the bot is waiting for About text."""
    await update.message.reply_text(
        "Please paste your *About / Summary* as text here.\n\n"
        "To start over with a PDF, send /start.",
        parse_mode="Markdown",
    )
    return AWAITING_ABOUT


# ── Navigation back handlers ──────────────────────────────────────────────────

async def back_to_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ai_uses = context.user_data.get("ai_uses", 0)
    _clear_flow_data(context.user_data)
    try:
        await query.message.delete()
    except Exception:
        pass
    await _send_welcome(context.bot, query.message.chat_id, ai_uses)
    return AWAITING_INPUT


async def back_to_previous(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    cb_data = query.data

    if cb_data == "back_photo":
        try:
            await query.message.delete()
        except Exception:
            pass
        if "pdf_text" in context.user_data:
            ai_uses = context.user_data.get("ai_uses", 0)
            _clear_flow_data(context.user_data)
            await _send_welcome(context.bot, query.message.chat_id, ai_uses)
            return AWAITING_INPUT
        else:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Go Back", callback_data="back_about")]])
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Got your headline ✅\n\nNow paste your *About / Summary* section below:",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return AWAITING_ABOUT

    elif cb_data == "back_banner":
        try:
            await query.message.delete()
        except Exception:
            pass
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Professional Headshot (smiling, clean background)", callback_data="btn_professional")],
            [InlineKeyboardButton("🤳 Casual / Crop (selfie, group photo, busy bg)", callback_data="btn_casual")],
            [InlineKeyboardButton("❌ No Photo / Default Avatar", callback_data="btn_none")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_photo")],
        ])
        photo_path = os.path.join(os.path.dirname(__file__), "Photo_example.jpeg")
        if os.path.exists(photo_path):
            with open(photo_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=f,
                    caption="📸 **Pillar 1: Profile Photo**\n\nChoose the description that fits your profile photo best (be honest!):",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📸 **Pillar 1: Profile Photo**\n\nChoose the description that fits your profile photo best (be honest!):",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        return AWAITING_PHOTO

    elif cb_data == "back_url":
        try:
            await query.message.delete()
        except Exception:
            pass
        await _send_banner_question(context.bot, query.message.chat_id)
        return AWAITING_BANNER

    elif cb_data == "back_experience":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Clean URL (e.g. /in/yourname)", callback_data="btn_clean")],
            [InlineKeyboardButton("❌ Default URL (containing random digits)", callback_data="btn_default")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_url")],
        ])
        await query.edit_message_text(
            "🔗 **Pillar 1: Custom URL**\n\n"
            "Is your profile URL cleaned up?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_URL

    elif cb_data == "back_otw":
        if "pdf_text" in context.user_data:
            # PDF path skips experience & education, goes back directly to custom URL
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Clean URL (e.g. /in/yourname)", callback_data="btn_clean")],
                [InlineKeyboardButton("❌ Default URL (containing random digits)", callback_data="btn_default")],
                [InlineKeyboardButton("⬅️ Go Back", callback_data="back_url")],
            ])
            await query.edit_message_text(
                "🔗 **Pillar 1: Custom URL**\n\n"
                "Is your profile URL cleaned up?",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return AWAITING_URL
        else:
            # Text path goes back to education
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎓 Yes, fully listed (with degree & details)", callback_data="btn_fully_listed")],
                [InlineKeyboardButton("📝 Listed with only university name (no details)", callback_data="btn_only_name")],
                [InlineKeyboardButton("❌ No education listed", callback_data="btn_none")],
                [InlineKeyboardButton("⬅️ Go Back", callback_data="back_education")],
            ])
            await query.edit_message_text(
                "🎓 **Education Section**\n\n"
                "Do you have your education listed on your profile?",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return AWAITING_EDUCATION

    elif cb_data == "back_education":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Quantified results & achievements", callback_data="btn_quantified")],
            [InlineKeyboardButton("📝 Plain list of job duties", callback_data="btn_plain")],
            [InlineKeyboardButton("❌ Only job titles, no description", callback_data="btn_none")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_experience")],
        ])
        await query.edit_message_text(
            "💼 **Pillar 1: Experience Section**\n\n"
            "How are your job experience descriptions written?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_EXPERIENCE

    elif cb_data == "back_skills_count":
        try:
            await query.message.delete()
        except Exception:
            pass
        await _send_otw_question(context.bot, query.message.chat_id)
        return AWAITING_OTW

    elif cb_data == "back_connections":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("50+ skills", callback_data="btn_over_50")],
            [InlineKeyboardButton("20–50 skills", callback_data="btn_20_to_50")],
            [InlineKeyboardButton("Fewer than 20", callback_data="btn_under_20")],
            [InlineKeyboardButton("No skills listed", callback_data="btn_none")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_skills_count")],
        ])
        await query.edit_message_text(
            "💪 **Number of Skills**\n\n"
            "How many skills do you have listed in your LinkedIn Profile's Skills section?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_SKILLS_COUNT

    elif cb_data == "back_posting":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("500+ connections", callback_data="btn_500_plus")],
            [InlineKeyboardButton("100–500 connections", callback_data="btn_100_500")],
            [InlineKeyboardButton("Under 100 connections", callback_data="btn_under_100")],
            [InlineKeyboardButton("0–10 connections", callback_data="btn_0_10")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_connections")],
        ])
        await query.edit_message_text(
            "👥 **Pillar 2: Connections**\n\n"
            "How many connections do you have on LinkedIn?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_CONNECTIONS

    elif cb_data == "back_skills":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("This week", callback_data="btn_this_week")],
            [InlineKeyboardButton("This month", callback_data="btn_this_month")],
            [InlineKeyboardButton("Over a month ago", callback_data="btn_over_month")],
            [InlineKeyboardButton("Never posted", callback_data="btn_never")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_posting")],
        ])
        await query.edit_message_text(
            "📝 **Pillar 3: Activity & Updates**\n\n"
            "When did you last post on LinkedIn?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_POSTING

    return AWAITING_INPUT


# ── Misc handlers ─────────────────────────────────────────────────────────────

async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = update.effective_user.id
    if not await _is_channel_member(context.bot, user_id):
        text, keyboard = _join_prompt()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return AWAITING_MEMBERSHIP

    ai_uses = context.user_data.get("ai_uses", 0)
    if ai_uses >= MAX_USES:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⛔ You've used all *{MAX_USES}* assessments.\n\nContact us for more access.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    _clear_flow_data(context.user_data)
    await _send_welcome(context.bot, query.message.chat_id, ai_uses)
    return AWAITING_INPUT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    return ConversationHandler.END


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import fcntl, sys
    lock_file = open("/tmp/linkedin_bot.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("Another bot instance is already running. Exiting.")
        sys.exit(1)

    persistence = PicklePersistence(
        filepath=os.path.join(os.path.dirname(__file__), "bot_persistence.pkl")
    )
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(restart_callback, pattern=r"^restart$"),
        ],
        states={
            AWAITING_MEMBERSHIP: [
                CallbackQueryHandler(check_membership_callback, pattern=r"^check_membership$"),
            ],
            AWAITING_INPUT: [
                MessageHandler(filters.Document.ALL, input_pdf_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_text_received),
            ],
            AWAITING_ABOUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, about_received),
                MessageHandler(filters.Document.ALL, _doc_in_about_path),
                CallbackQueryHandler(back_to_input, pattern=r"^back_about$"),
            ],
            AWAITING_PHOTO: [
                CallbackQueryHandler(photo_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_photo$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_BANNER: [
                CallbackQueryHandler(banner_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_banner$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_URL: [
                CallbackQueryHandler(url_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_url$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_EXPERIENCE: [
                CallbackQueryHandler(experience_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_experience$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_EDUCATION: [
                CallbackQueryHandler(education_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_education$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_OTW: [
                CallbackQueryHandler(otw_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_otw$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_SKILLS_COUNT: [
                CallbackQueryHandler(skills_count_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_skills_count$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_CONNECTIONS: [
                CallbackQueryHandler(connections_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_connections$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_POSTING: [
                CallbackQueryHandler(posting_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_posting$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buttons_only_nudge),
            ],
            AWAITING_SKILLS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, skills_received),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_skills$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(restart_callback, pattern=r"^restart$"),
        ],
    )

    app.add_handler(conv)

    app.run_polling()


if __name__ == "__main__":
    main()

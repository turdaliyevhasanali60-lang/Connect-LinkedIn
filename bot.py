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
AWAITING_INPUT, AWAITING_ABOUT, AWAITING_PHOTO, AWAITING_BANNER, AWAITING_URL, AWAITING_EXPERIENCE, AWAITING_EDUCATION, AWAITING_OTW, AWAITING_SKILLS_COUNT, AWAITING_CONNECTIONS, AWAITING_POSTING, AWAITING_SKILLS = range(12)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Welcome and ask for profile input."""
    context.user_data.clear()
    context.user_data["lang"] = "en"
    target = update.message or update.callback_query.message
    await target.reply_text(
        "👋 Welcome to the *Connect! LinkedIn Assessment Bot*.\n\n"
        "I'll score your profile against Shavkat Karimov's Connect! Tour framework.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💻 *On Desktop (best results):*\n"
        "1. Open your LinkedIn profile\n"
        "2. Click the *•••* button (next to \"Enhance profile\")\n"
        "3. Click *Save to PDF*\n"
        "4. Send me that PDF file right here\n\n"
        "📱 *On Mobile:*\n"
        "Just paste your *headline* as a text message — "
        "I'll guide you from there.",
        parse_mode="Markdown",
    )
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

    context.user_data["pdf_text"] = pdf_text
    context.user_data["name"] = guess_name(pdf_text)

    # PDF path skips About → straight to photo
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Professional Headshot (smiling, clean background)", callback_data="btn_professional")],
        [InlineKeyboardButton("🤳 Casual / Crop (selfie, group photo, busy bg)", callback_data="btn_casual")],
        [InlineKeyboardButton("❌ No Photo / Default Avatar", callback_data="btn_none")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_photo")],
    ])
    photo_path = os.path.join(os.path.dirname(__file__), "profile_photo_examples.png")
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
    photo_path = os.path.join(os.path.dirname(__file__), "profile_photo_examples.png")
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

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Custom professional banner", callback_data="btn_custom")],
        [InlineKeyboardButton("🌐 Default / generic banner", callback_data="btn_default")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_banner")],
    ])
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🖼️ **Pillar 1: Background Banner**\n\n"
        "What is behind your profile photo?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Recruiters only", callback_data="btn_recruiters_only")],
            [InlineKeyboardButton("🟢 All LinkedIn members (Green badge)", callback_data="btn_all_linkedin")],
            [InlineKeyboardButton("🏢 Currently employed / not job seeking", callback_data="btn_not_looking")],
            [InlineKeyboardButton("❌ OFF / Not set", callback_data="btn_not_looking")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_otw")],
        ])
        await query.edit_message_text(
            "💼 **Open to Work Status**\n\n"
            "What is your current Open to Work visibility?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
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

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Recruiters only", callback_data="btn_recruiters_only")],
        [InlineKeyboardButton("🟢 All LinkedIn members (Green badge)", callback_data="btn_all_linkedin")],
        [InlineKeyboardButton("🏢 Currently employed / not job seeking", callback_data="btn_not_looking")],
        [InlineKeyboardButton("❌ OFF / Not set", callback_data="btn_not_looking")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data="back_otw")],
    ])
    await query.edit_message_text(
        "💼 **Open to Work Status**\n\n"
        "What is your current Open to Work visibility?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
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

    full_text = ""
    last_update_time = time.time()

    try:
        async for chunk in assess_profile(context.user_data, "en"):
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

    # Delete the streaming placeholder and send the final formatted report
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception:
        pass

    await send_report(chat_id, full_text)

    keyboard = [[InlineKeyboardButton("🔁 Reassess", callback_data="restart")]]
    await context.bot.send_message(
        chat_id,
        "Want to reassess later? Just send /start again.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


# ── Navigation back handlers ──────────────────────────────────────────────────

async def back_to_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👋 Welcome to the *Connect! LinkedIn Assessment Bot*.\n\n"
        "I'll score your profile against Shavkat Karimov's Connect! Tour framework.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💻 *On Desktop (best results):*\n"
        "1. Open your LinkedIn profile\n"
        "2. Click the *•••* button (next to \"Enhance profile\")\n"
        "3. Click *Save to PDF*\n"
        "4. Send me that PDF file right here\n\n"
        "📱 *On Mobile:*\n"
        "Just paste your *headline* as a text message — "
        "I'll guide you from there.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    context.user_data["lang"] = "en"
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
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="👋 Welcome to the *Connect! LinkedIn Assessment Bot*.\n\n"
                "I'll score your profile against Shavkat Karimov's Connect! Tour framework.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "💻 *On Desktop (best results):*\n"
                "1. Open your LinkedIn profile\n"
                "2. Click the *•••* button (next to \"Enhance profile\")\n"
                "3. Click *Save to PDF*\n"
                "4. Send me that PDF file right here\n\n"
                "📱 *On Mobile:*\n"
                "Just paste your *headline* as a text message — "
                "I'll guide you from there.",
                parse_mode="Markdown",
            )
            context.user_data.clear()
            context.user_data["lang"] = "en"
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
        photo_path = os.path.join(os.path.dirname(__file__), "profile_photo_examples.png")
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎨 Custom professional banner", callback_data="btn_custom")],
            [InlineKeyboardButton("🌐 Default / generic banner", callback_data="btn_default")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_banner")],
        ])
        await query.edit_message_text(
            "🖼️ **Pillar 1: Background Banner**\n\n"
            "What is behind your profile photo?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Recruiters only", callback_data="btn_recruiters_only")],
            [InlineKeyboardButton("🟢 All LinkedIn members (Green badge)", callback_data="btn_all_linkedin")],
            [InlineKeyboardButton("🏢 Currently employed / not job seeking", callback_data="btn_not_looking")],
            [InlineKeyboardButton("❌ OFF / Not set", callback_data="btn_not_looking")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="back_otw")],
        ])
        await query.edit_message_text(
            "💼 **Open to Work Status**\n\n"
            "What is your current Open to Work visibility?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
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
    context.user_data.clear()
    context.user_data["lang"] = "en"
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="👋 Welcome to the *Connect! LinkedIn Assessment Bot*.\n\n"
             "I'll score your profile against Shavkat Karimov's Connect! Tour framework.\n\n"
             "━━━━━━━━━━━━━━━━━━━━━\n\n"
             "💻 *On Desktop (best results):*\n"
             "1. Open your LinkedIn profile\n"
             "2. Click the *•••* button (next to \"Enhance profile\")\n"
             "3. Click *Save to PDF*\n"
             "4. Send me that PDF file right here\n\n"
             "📱 *On Mobile:*\n"
             "Just paste your *headline* as a text message — "
             "I'll guide you from there.",
        parse_mode="Markdown",
    )
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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(restart_callback, pattern=r"^restart$"),
        ],
        states={
            AWAITING_INPUT: [
                MessageHandler(filters.Document.ALL, input_pdf_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_text_received),
            ],
            AWAITING_ABOUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, about_received),
                CallbackQueryHandler(back_to_input, pattern=r"^back_about$"),
            ],
            AWAITING_PHOTO: [
                CallbackQueryHandler(photo_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_photo$"),
            ],
            AWAITING_BANNER: [
                CallbackQueryHandler(banner_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_banner$"),
            ],
            AWAITING_URL: [
                CallbackQueryHandler(url_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_url$"),
            ],
            AWAITING_EXPERIENCE: [
                CallbackQueryHandler(experience_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_experience$"),
            ],
            AWAITING_EDUCATION: [
                CallbackQueryHandler(education_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_education$"),
            ],
            AWAITING_OTW: [
                CallbackQueryHandler(otw_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_otw$"),
            ],
            AWAITING_SKILLS_COUNT: [
                CallbackQueryHandler(skills_count_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_skills_count$"),
            ],
            AWAITING_CONNECTIONS: [
                CallbackQueryHandler(connections_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_connections$"),
            ],
            AWAITING_POSTING: [
                CallbackQueryHandler(posting_chosen, pattern=r"^btn_"),
                CallbackQueryHandler(back_to_previous, pattern=r"^back_posting$"),
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

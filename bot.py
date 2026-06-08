"""Unified bot — handles on-demand messages and scheduled daily updates via APScheduler."""

import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import claude
import daily_log
import garmin
import notes

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# In-memory conversation history per chat (last 20 messages = 10 exchanges)
_conversations: dict[str, list[dict]] = {}
_MAX_HISTORY = 20


def is_authorized(update: Update) -> bool:
    return str(update.effective_chat.id) == TELEGRAM_CHAT_ID


def _get_history(chat_id: str) -> list[dict]:
    return _conversations.get(chat_id, [])


def _add_to_history(chat_id: str, role: str, content: str):
    if chat_id not in _conversations:
        _conversations[chat_id] = []
    _conversations[chat_id].append({"role": role, "content": content})
    if len(_conversations[chat_id]) > _MAX_HISTORY:
        _conversations[chat_id] = _conversations[chat_id][-_MAX_HISTORY:]


async def run_daily_update():
    """Generate and send the daily coaching update. Fired by APScheduler."""
    logging.info("Running daily update...")
    try:
        config = notes.load_config()
        tz = ZoneInfo(config["schedule"]["timezone"])
        today = datetime.now(tz).date()

        data = garmin.fetch_garmin_data()
        garmin_formatted = garmin.format_garmin_data(data, units=config["coaching"]["units"])

        training_plan = notes.read_plan()
        coach_notes = notes.read_notes()
        athlete_profile = notes.read_profile()
        recent_logs = daily_log.read_recent_logs(n_days=30)

        response = claude.daily_update(
            garmin_formatted, training_plan, coach_notes, athlete_profile, recent_logs
        )
        message, updated_notes, updated_profile, _, _feedback = notes.parse_claude_response(response)
        notes.apply_updates(updated_notes, updated_profile)

        daily_log.write_daily_log(
            log_date=today,
            garmin_formatted=garmin_formatted,
            coaching_message=message,
        )

        # Fresh day — clear conversation history
        _conversations.clear()

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logging.info("Daily update sent.")

    except Exception as e:
        logging.error(f"Daily update failed: {e}", exc_info=True)
        try:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Daily update failed: {e}")
        except Exception:
            pass


def _fetch_garmin(config: dict) -> str:
    """Fetch and format Garmin data, returning empty string on failure."""
    try:
        data = garmin.fetch_garmin_data()
        return garmin.format_garmin_data(data, units=config["coaching"]["units"])
    except Exception as e:
        logging.warning(f"Garmin fetch failed: {e}")
        return ""


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    chat_id = str(update.effective_chat.id)
    user_message = update.message.text
    history = _get_history(chat_id)
    config = notes.load_config()

    # Bust cache so any workout since the daily update is included
    garmin.invalidate_cache()
    garmin_formatted = _fetch_garmin(config)

    response = claude.respond_to_user(
        user_message,
        notes.read_plan(),
        notes.read_notes(),
        notes.read_profile(),
        history,
        garmin_formatted=garmin_formatted,
    )
    message, updated_notes, updated_profile, _, feedback_log = notes.parse_claude_response(response)
    notes.apply_updates(updated_notes, updated_profile)
    if feedback_log:
        daily_log.append_feedback_to_log(date.today(), feedback_log)

    _add_to_history(chat_id, "user", user_message)
    _add_to_history(chat_id, "assistant", message)

    await update.message.reply_text(message)


async def _log_feedback(update: Update, entry: str, reply: str):
    notes.append_feedback_note(entry)
    daily_log.append_feedback_to_log(date.today(), entry)
    await update.message.reply_text(reply)


async def cmd_felt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    args = " ".join(context.args).strip() if context.args else ""
    if not args:
        await update.message.reply_text(
            "Usage: /felt <rating or description>\n"
            "Examples:\n  /felt 8\n  /felt tired, heavy legs\n  /felt great, easy effort"
        )
        return
    await _log_feedback(update, f"workout feel: {args}", f"Logged: workout feel: {args}")


async def cmd_rpe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    args_list = context.args if context.args else []
    if not args_list:
        await update.message.reply_text(
            "Usage: /rpe <1-10> [optional note]\n"
            "Examples:\n  /rpe 6\n  /rpe 8 tempo felt hard"
        )
        return
    rpe_val = args_list[0]
    rest = " ".join(args_list[1:])
    entry = f"RPE {rpe_val}" + (f" — {rest}" if rest else "")
    await _log_feedback(update, entry, f"Logged: {entry}")


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: /note <text>\n"
            "Examples:\n  /note left knee tight\n  /note missed run, sick"
        )
        return
    await _log_feedback(update, text, f"Noted: {text}")


async def cmd_pr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log a personal record: /pr <distance> <time> [optional note]"""
    if not is_authorized(update):
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: /pr <distance> <time> [note]\n"
            "Examples:\n  /pr 5k 23:45\n  /pr mile 6:02 track workout"
        )
        return
    entry = f"PR: {text}"
    today = date.today().isoformat()
    notes.append_feedback_note(entry)
    daily_log.append_feedback_to_log(date.today(), entry)
    notes.append_to_profile(f"- [{today}] PR: {text}")
    await update.message.reply_text(f"PR recorded: {text} — added to your profile.")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    today_str = date.today().strftime("%A, %B %d")
    history = _get_history(chat_id)
    config = notes.load_config()

    garmin.invalidate_cache()
    garmin_formatted = _fetch_garmin(config)

    response = claude.respond_to_user(
        f"What's on the training plan for today ({today_str})? "
        f"Give me a quick readiness check based on recent data and notes.",
        notes.read_plan(),
        notes.read_notes(),
        notes.read_profile(),
        history,
        garmin_formatted=garmin_formatted,
    )
    message, updated_notes, updated_profile, _, _feedback = notes.parse_claude_response(response)
    notes.apply_updates(updated_notes, updated_profile)
    _add_to_history(chat_id, "assistant", message)
    await update.message.reply_text(message)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Summarize this week's training."""
    if not is_authorized(update):
        return
    recent = daily_log.read_recent_logs(n_days=7)
    if recent == "No daily logs yet.":
        await update.message.reply_text("No logs this week yet.")
        return
    config = notes.load_config()
    garmin_formatted = _fetch_garmin(config)

    response = claude.respond_to_user(
        f"Give me a concise summary of this week's training. Cover: volume, quality of key sessions, "
        f"consistency, how recovery is trending, and one thing to watch heading into next week.\n\n"
        f"TRAINING HISTORY (last 7 days):\n{recent}",
        notes.read_plan(),
        notes.read_notes(),
        notes.read_profile(),
        [],
        garmin_formatted=garmin_formatted,
    )
    message, updated_notes, updated_profile, _, _feedback = notes.parse_claude_response(response)
    notes.apply_updates(updated_notes, updated_profile)
    await update.message.reply_text(message)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current coach notes and athlete profile."""
    if not is_authorized(update):
        return
    coach_notes = notes.read_notes()
    athlete_profile = notes.read_profile()
    text = f"{athlete_profile}\n\n---\n\n{coach_notes}"
    await update.message.reply_text(text)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bust the Garmin cache so the next fetch pulls fresh data."""
    if not is_authorized(update):
        return
    garmin.invalidate_cache()
    await update.message.reply_text("Garmin cache cleared — next message will pull fresh data.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Commands:\n"
        "/felt <rating or description> — log how a workout felt\n"
        "/rpe <1-10> [note] — log RPE\n"
        "/note <text> — quick note (injury, missed workout, etc.)\n"
        "/pr <distance> <time> — log a personal record\n"
        "/today — today's plan + readiness check\n"
        "/week — summary of this week's training\n"
        "/status — view your current profile and coach notes\n"
        "/refresh — pull fresh Garmin data\n"
        "/help — show this message\n\n"
        "Or just send any message to chat with your coach."
    )


def main():
    config = notes.load_config()
    tz_str = config["schedule"]["timezone"]
    schedule_hour = config["schedule"]["hour"]

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("felt", cmd_felt))
    app.add_handler(CommandHandler("rpe", cmd_rpe))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("pr", cmd_pr))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone=tz_str)
    scheduler.add_job(run_daily_update, "cron", hour=schedule_hour, minute=0)
    scheduler.start()
    logging.info(f"Scheduler started — daily update at {schedule_hour}:00 {tz_str}")

    logging.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

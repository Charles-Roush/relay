"""Unified bot — handles on-demand messages and scheduled daily updates via APScheduler."""

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
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

# In-memory conversation history per chat
_conversations: dict[str, list[dict]] = {}

# Track when the last daily update ran (for /ping)
_last_daily_update: datetime | None = None

# Path for post-workout check-in state (tracks last seen activity ID)
_CHECKIN_STATE_FILE = Path("logs/checkin_state.json")


def _load_checkin_state() -> dict:
    if _CHECKIN_STATE_FILE.exists():
        try:
            return json.loads(_CHECKIN_STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_seen_activity_id": None}


def _save_checkin_state(state: dict):
    _CHECKIN_STATE_FILE.parent.mkdir(exist_ok=True)
    _CHECKIN_STATE_FILE.write_text(json.dumps(state))


def _max_history() -> int:
    config = notes.load_config()
    return config.get("history", {}).get("max_messages", 20)


def is_authorized(update: Update) -> bool:
    return str(update.effective_chat.id) == TELEGRAM_CHAT_ID


def _get_history(chat_id: str) -> list[dict]:
    return _conversations.get(chat_id, [])


def _add_to_history(chat_id: str, role: str, content: str):
    if chat_id not in _conversations:
        _conversations[chat_id] = []
    _conversations[chat_id].append({"role": role, "content": content})
    max_h = _max_history()
    if len(_conversations[chat_id]) > max_h:
        _conversations[chat_id] = _conversations[chat_id][-max_h:]


def _has_talked_today(chat_id: str) -> bool:
    return bool(_conversations.get(chat_id))


def startup_health_check() -> list[str]:
    """
    Validate that all required credentials and files are present.
    Returns a list of error strings (empty = all good).
    """
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN not set in .env")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID not set in .env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY not set in .env")
    if not os.getenv("GARMIN_EMAIL"):
        errors.append("GARMIN_EMAIL not set in .env")
    if not os.getenv("GARMIN_PASSWORD"):
        errors.append("GARMIN_PASSWORD not set in .env")
    if not Path("config.yaml").exists():
        errors.append("config.yaml not found")
    if not Path(notes.load_config().get("paths", {}).get("plan_file", "training_plan.md")).exists():
        errors.append("training_plan.md not found — create it or the coach will have no plan to reference")
    return errors


async def run_daily_update():
    """Generate and send the daily coaching update. Fired by APScheduler."""
    global _last_daily_update
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
        weekly_reflection = notes.read_weekly_reflection()
        recent_logs = daily_log.read_recent_logs(n_days=30)

        # Check if user has already messaged today before the daily update fires
        already_talked = _has_talked_today(TELEGRAM_CHAT_ID)

        response = claude.daily_update(
            garmin_formatted,
            training_plan,
            coach_notes,
            athlete_profile,
            recent_logs=recent_logs,
            weekly_reflection=weekly_reflection,
            has_talked_today=already_talked,
        )
        message, updated_notes, updated_profile, _, _feedback = notes.parse_claude_response(response)
        notes.apply_updates(updated_notes, updated_profile)

        daily_log.write_daily_log(
            log_date=today,
            garmin_formatted=garmin_formatted,
            coaching_message=message,
        )

        _last_daily_update = datetime.now(tz)

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


async def run_weekly_reflection():
    """Generate and save a weekly reflection. Fired Sunday evenings by APScheduler."""
    logging.info("Running weekly reflection...")
    try:
        config = notes.load_config()
        tz = ZoneInfo(config["schedule"]["timezone"])
        now = datetime.now(tz)
        # Label e.g. "Week of Jun 2–8, 2026"
        week_start = now.date() - timedelta(days=6)
        week_label = f"Week of {week_start.strftime('%b %-d')}–{now.strftime('%-d, %Y')}"

        data = garmin.fetch_garmin_data()
        garmin_formatted = garmin.format_garmin_data(data, units=config["coaching"]["units"])
        recent_logs = daily_log.read_recent_logs(n_days=14)

        response = claude.generate_weekly_reflection(
            garmin_formatted=garmin_formatted,
            training_plan=notes.read_plan(),
            coach_notes=notes.read_notes(),
            athlete_profile=notes.read_profile(),
            recent_logs=recent_logs,
            week_label=week_label,
        )
        reflection_text, _, _, _, _ = notes.parse_claude_response(response)
        notes.write_weekly_reflection(reflection_text)
        logging.info("Weekly reflection saved.")

    except Exception as e:
        logging.error(f"Weekly reflection failed: {e}", exc_info=True)


async def check_for_new_activity():
    """
    Hourly job: check if a new workout appeared since the last check-in.
    If so, and post_workout_checkin is enabled, send a short check-in message.
    """
    try:
        config = notes.load_config()
        if not config.get("coaching", {}).get("post_workout_checkin", False):
            return

        tz = ZoneInfo(config["schedule"]["timezone"])
        now = datetime.now(tz)

        # Fetch fresh data (invalidate cache so we see the newest workout)
        garmin.invalidate_cache()
        data = garmin.fetch_garmin_data()
        latest = garmin.get_latest_activity(data)
        if not latest:
            return

        activity_id = latest.get("activity_id")
        activity_date = latest.get("date", "")
        activity_time = latest.get("time", "")  # HH:MM

        # Only trigger for activities that happened today
        if activity_date != now.date().isoformat():
            return

        # Only trigger if the activity started within the last 3 hours
        try:
            act_hour, act_min = map(int, activity_time.split(":"))
            act_dt = now.replace(hour=act_hour, minute=act_min, second=0, microsecond=0)
            hours_ago = (now - act_dt).total_seconds() / 3600
            if hours_ago > 3 or hours_ago < 0:
                return
        except Exception:
            return

        # Check if we've already sent a check-in for this activity
        state = _load_checkin_state()
        if str(activity_id) == str(state.get("last_seen_activity_id")):
            return

        # Build a brief activity summary for Claude
        units = config["coaching"]["units"]
        dist = latest.get("distance_meters")
        dist_str = f"{dist / 1609.34:.1f} mi" if dist and units == "imperial" else (f"{dist / 1000:.1f} km" if dist else "unknown distance")
        name = latest.get("name") or latest.get("type", "run")
        activity_summary = f"{name}: {dist_str} at {activity_time}"

        response = claude.post_workout_checkin(
            activity_summary=activity_summary,
            coach_notes=notes.read_notes(),
            athlete_profile=notes.read_profile(),
        )
        message, _, _, _, _ = notes.parse_claude_response(response)

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

        # Save state so we don't double-send
        _save_checkin_state({"last_seen_activity_id": str(activity_id)})
        logging.info(f"Post-workout check-in sent for activity {activity_id}.")

    except Exception as e:
        logging.warning(f"Post-workout check-in failed: {e}")


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
        weekly_reflection=notes.read_weekly_reflection(),
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
        weekly_reflection=notes.read_weekly_reflection(),
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
        weekly_reflection=notes.read_weekly_reflection(),
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


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check — show uptime and last daily update time."""
    if not is_authorized(update):
        return
    config = notes.load_config()
    tz = ZoneInfo(config["schedule"]["timezone"])
    now = datetime.now(tz)
    if _last_daily_update:
        delta = now - _last_daily_update
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        last_update_str = f"{_last_daily_update.strftime('%H:%M')} ({hours}h {mins}m ago)"
    else:
        last_update_str = "not yet (bot may have just restarted)"
    await update.message.reply_text(
        f"Bot is running.\n"
        f"Current time: {now.strftime('%H:%M %Z')}\n"
        f"Last daily update: {last_update_str}"
    )


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Update a config value from Telegram.
    Usage: /set <key> <value>

    Supported keys:
      tone        direct | encouraging | detailed
      goal        <free text>
      focus       <free text>
      schedule    <hour 0-23>
      checkin     true | false
    """
    if not is_authorized(update):
        return

    args = context.args if context.args else []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /set <key> <value>\n\n"
            "Keys:\n"
            "  tone       direct | encouraging | detailed\n"
            "  goal       <your current goal, free text>\n"
            "  focus      <training focus, free text>\n"
            "  schedule   <hour 0-23 for daily update>\n"
            "  checkin    true | false"
        )
        return

    key = args[0].lower()
    value_str = " ".join(args[1:])

    key_map = {
        "tone": ("claude.daily_update.tone", str),
        "goal": ("coaching.goal", str),
        "focus": ("coaching.focus", str),
        "schedule": ("schedule.hour", int),
        "checkin": ("coaching.post_workout_checkin", lambda v: v.lower() == "true"),
    }

    if key not in key_map:
        await update.message.reply_text(
            f"Unknown key '{key}'. Valid keys: {', '.join(key_map)}"
        )
        return

    config_path, converter = key_map[key]
    try:
        value = converter(value_str)
    except (ValueError, TypeError):
        await update.message.reply_text(f"Invalid value '{value_str}' for key '{key}'.")
        return

    if key == "tone" and value not in ("direct", "encouraging", "detailed"):
        await update.message.reply_text("Tone must be: direct, encouraging, or detailed.")
        return

    if key == "schedule":
        if not (0 <= value <= 23):
            await update.message.reply_text("Schedule hour must be 0–23.")
            return

    success = notes.set_config_value(config_path, value)
    if success:
        await update.message.reply_text(f"Updated: {key} = {value}")
        if key == "schedule":
            await update.message.reply_text(
                "Note: schedule changes take effect after bot restart."
            )
    else:
        await update.message.reply_text(f"Failed to update '{key}'. Check config.yaml.")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current configurable settings."""
    if not is_authorized(update):
        return
    config = notes.load_config()
    c = config.get("coaching", {})
    cl = config.get("claude", {}).get("daily_update", {})
    s = config.get("schedule", {})
    await update.message.reply_text(
        f"Current settings:\n"
        f"  goal:     {c.get('goal', '—')}\n"
        f"  focus:    {c.get('focus', '—')}\n"
        f"  tone:     {cl.get('tone', '—')}\n"
        f"  schedule: {s.get('hour', '—')}:00 {s.get('timezone', '')}\n"
        f"  checkin:  {c.get('post_workout_checkin', False)}\n\n"
        f"Use /set <key> <value> to change."
    )


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
        "/settings — view current config settings\n"
        "/set <key> <value> — update a setting\n"
        "/refresh — pull fresh Garmin data\n"
        "/ping — check bot is alive\n"
        "/help — show this message\n\n"
        "Or just send any message to chat with your coach."
    )


def main():
    config = notes.load_config()
    tz_str = config["schedule"]["timezone"]
    schedule_hour = config["schedule"]["hour"]
    weekly_reflection_hour = config["schedule"].get("weekly_reflection_hour", 20)

    # Startup health check
    errors = startup_health_check()
    if errors:
        for err in errors:
            logging.warning(f"Health check: {err}")
    else:
        logging.info("Health check passed.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("felt", cmd_felt))
    app.add_handler(CommandHandler("rpe", cmd_rpe))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("pr", cmd_pr))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone=tz_str)
    scheduler.add_job(run_daily_update, "cron", hour=schedule_hour, minute=0)
    scheduler.add_job(run_weekly_reflection, "cron", day_of_week="sun", hour=weekly_reflection_hour, minute=0)
    scheduler.add_job(check_for_new_activity, "interval", hours=1)
    logging.info(f"Scheduler configured — daily update at {schedule_hour}:00 {tz_str}, weekly reflection Sundays at {weekly_reflection_hour}:00")

    async def post_init(application):
        scheduler.start()
        logging.info("Scheduler started.")

    app.post_init = post_init

    logging.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

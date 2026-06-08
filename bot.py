"""Unified bot — handles on-demand messages and scheduled daily updates via APScheduler."""

import json
import logging
import os
import re
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
        return errors  # can't load config — skip remaining checks
    try:
        plan_path = notes.load_config().get("paths", {}).get("plan_file", "training_plan.md")
        if not Path(plan_path).exists():
            errors.append("training_plan.md not found — create it or the coach will have no plan to reference")
    except Exception as e:
        errors.append(f"config.yaml could not be parsed: {e}")
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
        recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))

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
        recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("load_trend_days", 14))

        response = claude.generate_weekly_reflection(
            garmin_formatted=garmin_formatted,
            training_plan=notes.read_plan(),
            coach_notes=notes.read_notes(),
            athlete_profile=notes.read_profile(),
            recent_logs=recent_logs,
            week_label=week_label,
            weekly_reflection=notes.read_weekly_reflection(),
        )

        # <coaching_message> = memory reflection saved to file
        # <athlete_summary>  = athlete-facing summary sent to Telegram
        memory_reflection, updated_coach_notes, updated_profile, _, _ = notes.parse_claude_response(response)
        athlete_summary = notes.extract_tag(response, "athlete_summary")

        notes.write_weekly_reflection(memory_reflection)
        notes.apply_updates(updated_coach_notes, updated_profile)
        logging.info("Weekly reflection saved.")

        if athlete_summary:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=athlete_summary)
            logging.info("Weekly athlete summary sent to Telegram.")

    except Exception as e:
        logging.error(f"Weekly reflection failed: {e}", exc_info=True)
        try:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Weekly reflection failed: {e}")
        except Exception:
            pass


async def run_evening_checkin():
    """Scheduled nightly check-in — plan vs actual, execution feedback, tomorrow setup."""
    logging.info("Running evening check-in...")
    try:
        config = notes.load_config()
        if not config["schedule"].get("evening_checkin", {}).get("enabled", True):
            logging.info("Evening check-in disabled in config — skipping.")
            return

        tz = ZoneInfo(config["schedule"]["timezone"])
        today = datetime.now(tz).date()

        garmin.invalidate_cache()
        data = garmin.fetch_garmin_data()
        garmin_formatted = garmin.format_garmin_data(data, units=config["coaching"]["units"])

        # Pull today's feedback and morning analysis from the daily log
        todays_log_path = daily_log._log_path(today)
        todays_feedback = ""
        morning_summary = ""
        if todays_log_path.exists():
            log_text = todays_log_path.read_text()
            fb_match = re.search(r'### Athlete Feedback\n(.*?)(?=\n##|\Z)', log_text, re.DOTALL)
            if fb_match:
                todays_feedback = fb_match.group(1).strip()
            analysis_match = re.search(r'## Coach Analysis\n\n(.*?)(?=\n##|\Z)', log_text, re.DOTALL)
            if analysis_match:
                morning_summary = analysis_match.group(1).strip()

        recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))

        response = claude.evening_checkin(
            garmin_formatted=garmin_formatted,
            training_plan=notes.read_plan(),
            coach_notes=notes.read_notes(),
            athlete_profile=notes.read_profile(),
            todays_feedback=todays_feedback,
            recent_logs=recent_logs,
            weekly_reflection=notes.read_weekly_reflection(),
            morning_summary=morning_summary,
        )
        message, updated_notes, updated_profile, _, feedback_log = notes.parse_claude_response(response)
        notes.apply_updates(updated_notes, updated_profile)
        if feedback_log:
            daily_log.append_feedback_to_log(today, feedback_log)

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logging.info("Evening check-in sent.")

    except Exception as e:
        logging.error(f"Evening check-in failed: {e}", exc_info=True)
        try:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Evening check-in failed: {e}")
        except Exception:
            pass


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
        dist_str = f"{dist / 1609.34:.1f} mi" if dist and units == "imperial" else (f"{dist / 1000:.1f} km" if dist else "?")
        name = latest.get("name") or latest.get("type", "run")
        dur = latest.get("duration_seconds")
        dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "?"
        avg_speed = latest.get("avg_speed_ms")
        if avg_speed and avg_speed > 0:
            if units == "imperial":
                secs = 1609.34 / avg_speed
                pace_str = f"{int(secs // 60)}:{int(secs % 60):02d}/mi"
            else:
                secs = 1000 / avg_speed
                pace_str = f"{int(secs // 60)}:{int(secs % 60):02d}/km"
        else:
            pace_str = "?"
        avg_hr = latest.get("avg_hr")
        hr_str = f"avg HR {avg_hr}" if avg_hr else ""
        aerobic_te = latest.get("aerobic_te")
        te_str = f"aerobic TE {aerobic_te:.1f}" if aerobic_te is not None else ""
        activity_summary = " | ".join(filter(None, [
            f"{name}: {dist_str}",
            f"time {dur_str}",
            f"avg pace {pace_str}",
            hr_str,
            te_str,
            f"started {activity_time}",
        ]))

        garmin_formatted = garmin.format_garmin_data(data, units=units)
        recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))

        response = claude.post_workout_checkin(
            activity_summary=activity_summary,
            coach_notes=notes.read_notes(),
            athlete_profile=notes.read_profile(),
            garmin_formatted=garmin_formatted,
            training_plan=notes.read_plan(),
            recent_logs=recent_logs,
            weekly_reflection=notes.read_weekly_reflection(),
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
    recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))

    response = claude.respond_to_user(
        user_message,
        notes.read_plan(),
        notes.read_notes(),
        notes.read_profile(),
        history,
        garmin_formatted=garmin_formatted,
        weekly_reflection=notes.read_weekly_reflection(),
        recent_logs=recent_logs,
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
    config = notes.load_config()

    garmin.invalidate_cache()
    garmin_formatted = _fetch_garmin(config)
    recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))

    response = claude.today_readiness_check(
        training_plan=notes.read_plan(),
        coach_notes=notes.read_notes(),
        athlete_profile=notes.read_profile(),
        garmin_formatted=garmin_formatted,
        recent_logs=recent_logs,
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
    config = notes.load_config()
    garmin.invalidate_cache()
    garmin_formatted = _fetch_garmin(config)

    response = claude.respond_to_user(
        "Give me a concise summary of this week's training. Cover: volume, quality of key sessions, "
        "consistency, how recovery is trending, and one thing to watch heading into next week.",
        notes.read_plan(),
        notes.read_notes(),
        notes.read_profile(),
        [],
        garmin_formatted=garmin_formatted,
        weekly_reflection=notes.read_weekly_reflection(),
        recent_logs=recent,
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
        "schedule": ("schedule.daily_update.hour", int),
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
    du = s.get("daily_update", {})
    ev = s.get("evening_checkin", {})
    wr = s.get("weekly_reflection", {})
    ac = s.get("activity_check", {})
    dbg = config.get("debug", {})
    await update.message.reply_text(
        f"Current settings:\n"
        f"  goal:           {c.get('goal', '—')}\n"
        f"  focus:          {c.get('focus', '—')}\n"
        f"  tone:           {cl.get('tone', '—')}\n"
        f"  timezone:       {s.get('timezone', '—')}\n"
        f"  daily update:   {du.get('hour', '—')}:{du.get('minute', 0):02d}\n"
        f"  evening checkin: {ev.get('hour', '—')}:{ev.get('minute', 0):02d} ({'on' if ev.get('enabled', True) else 'off'})\n"
        f"  weekly report:  {wr.get('day_of_week', 'sun').capitalize()} {wr.get('hour', '—')}:{wr.get('minute', 0):02d}\n"
        f"  activity check: every {ac.get('interval_minutes', 60)} min\n"
        f"  post-workout checkin: {c.get('post_workout_checkin', False)}\n"
        f"  debug mode:     {dbg.get('enabled', False)}\n\n"
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

    sched = config["schedule"]
    daily_hour   = sched["daily_update"]["hour"]
    daily_min    = sched["daily_update"].get("minute", 0)
    weekly_dow   = sched["weekly_reflection"].get("day_of_week", "sun")
    weekly_hour  = sched["weekly_reflection"]["hour"]
    weekly_min   = sched["weekly_reflection"].get("minute", 0)
    check_mins      = sched["activity_check"].get("interval_minutes", 60)
    evening_hour    = sched["evening_checkin"]["hour"]
    evening_min     = sched["evening_checkin"].get("minute", 0)
    evening_enabled = sched["evening_checkin"].get("enabled", True)

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
    scheduler.add_job(run_daily_update, "cron", hour=daily_hour, minute=daily_min)
    if evening_enabled:
        scheduler.add_job(run_evening_checkin, "cron", hour=evening_hour, minute=evening_min)
    scheduler.add_job(run_weekly_reflection, "cron", day_of_week=weekly_dow, hour=weekly_hour, minute=weekly_min)
    scheduler.add_job(check_for_new_activity, "interval", minutes=check_mins)
    logging.info(
        f"Scheduler configured:\n"
        f"  Daily update:      {daily_hour}:{daily_min:02d} {tz_str}\n"
        f"  Evening check-in:  {evening_hour}:{evening_min:02d} {tz_str} ({'enabled' if evening_enabled else 'DISABLED'})\n"
        f"  Weekly reflection: {weekly_dow.capitalize()} {weekly_hour}:{weekly_min:02d} {tz_str}\n"
        f"  Activity check:    every {check_mins} min"
    )

    async def post_init(application):
        scheduler.start()
        logging.info("Scheduler started.")

    app.post_init = post_init

    logging.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

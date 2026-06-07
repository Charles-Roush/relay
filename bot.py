"""Telegram bot — handles on-demand messages. Run continuously (Railway/Fly/local tmux)."""

import logging
import os
import subprocess

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import claude
import notes

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def commit_state():
    """Commit updated notes/plan back to git so state stays in sync with scheduled job."""
    try:
        subprocess.run(["git", "add", "coach_notes.md", "training_plan.md"], check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if result.returncode != 0:  # there are staged changes
            subprocess.run(
                ["git", "commit", "-m", "bot: update coach notes and training plan"],
                check=True,
            )
            subprocess.run(["git", "push"], check=True)
    except subprocess.CalledProcessError as e:
        logging.warning(f"Git commit/push failed: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only respond to the configured chat
    if str(update.effective_chat.id) != TELEGRAM_CHAT_ID:
        return

    user_message = update.message.text
    training_plan = notes.read_plan()
    coach_notes = notes.read_notes()

    response = claude.respond_to_user(user_message, training_plan, coach_notes)
    message, updated_notes, updated_plan = notes.parse_claude_response(response)
    notes.apply_updates(updated_notes, updated_plan)

    await update.message.reply_text(message)

    commit_state()


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logging.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

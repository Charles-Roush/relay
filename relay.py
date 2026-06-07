"""Scheduled daily update — runs via GitHub Actions cron."""

import os

import httpx
from dotenv import load_dotenv

import claude
import garmin
import notes

load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_message(text: str):
    httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=10,
    )


def main():
    # Load config for units
    import yaml
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    # Fetch and format Garmin data
    data = garmin.fetch_garmin_data()
    garmin_formatted = garmin.format_garmin_data(data, units=config["coaching"]["units"])

    # Read current state
    training_plan = notes.read_plan()
    coach_notes = notes.read_notes()

    # Generate daily update
    response = claude.daily_update(garmin_formatted, training_plan, coach_notes)

    # Parse and persist updates
    message, updated_notes, updated_plan = notes.parse_claude_response(response)
    notes.apply_updates(updated_notes, updated_plan)

    # Send to Telegram (skip if DRY_RUN=1)
    if os.getenv("DRY_RUN") == "1":
        print(message)
    else:
        send_message(message)
        print("Sent:", message)


if __name__ == "__main__":
    main()

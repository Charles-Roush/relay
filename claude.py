import os
from datetime import date

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv(override=True)


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def build_system_prompt(config: dict) -> str:
    coaching = config["coaching"]
    tone = config["claude"]["daily_update"]["tone"]
    return (
        f"You are a concise, knowledgeable {coaching['sport']} coach. You have access to the athlete's "
        f"Garmin data, training plan, and your own rolling notes. Tone: {tone}. Use {coaching['units']} units. "
        f"Current athlete goal: {coaching['goal']}. Current training focus: {coaching['focus']}. "
        f"Write in short natural sentences like a coach texting an athlete. No bullet points.\n\n"
        f"After every response, output UPDATED_NOTES: followed by the full updated coach_notes.md content, "
        f"and UPDATED_PLAN: followed by the full updated training_plan.md content (unchanged if no edits needed)."
    )


def daily_update(garmin_formatted: str, training_plan: str, coach_notes: str) -> str:
    config = load_config()
    claude_cfg = config["claude"]
    g_cfg = config["garmin"]

    client = anthropic.Anthropic()

    user_prompt = (
        f"Date: {date.today().isoformat()}\n\n"
        f"GARMIN DATA (last {g_cfg['lookback_days']} days):\n{garmin_formatted}\n\n"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"Write a daily coaching summary. Max {claude_cfg['daily_update']['max_sentences']} sentences. "
        f"End with one clear recommendation for today."
    )

    if os.getenv("DRY_RUN") == "1":
        print("=== SYSTEM PROMPT ===")
        print(build_system_prompt(config))
        print("\n=== USER PROMPT ===")
        print(user_prompt)
        print("===================\n")

    message = client.messages.create(
        model=claude_cfg["model"],
        max_tokens=claude_cfg["max_tokens"],
        system=build_system_prompt(config),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def respond_to_user(user_message: str, training_plan: str, coach_notes: str) -> str:
    config = load_config()
    claude_cfg = config["claude"]

    client = anthropic.Anthropic()

    user_prompt = (
        f"Date: {date.today().isoformat()}\n\n"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"USER MESSAGE: {user_message}\n\n"
        f"Respond naturally as their coach. If they're updating something (injury, schedule change, "
        f"race added), confirm what you've noted. If they're asking a question, answer it directly. "
        f"Keep it short — this is a text conversation."
    )

    message = client.messages.create(
        model=claude_cfg["model"],
        max_tokens=claude_cfg["max_tokens"],
        system=build_system_prompt(config),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text

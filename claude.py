import os
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv(override=True)

_config: dict | None = None
_client: anthropic.Anthropic | None = None


def load_config() -> dict:
    global _config
    if _config is None:
        with open("config.yaml") as f:
            _config = yaml.safe_load(f)
    return _config


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def get_local_date(timezone_str: str) -> str:
    return datetime.now(ZoneInfo(timezone_str)).date().isoformat()


def build_system_prompt(config: dict) -> str:
    coaching = config["coaching"]
    tone = config["claude"]["daily_update"]["tone"]
    return (
        f"You are an experienced, data-driven {coaching['sport']} coach with a long-term relationship with this athlete. "
        f"You have access to their Garmin biometric data, full training plan, rolling coach notes, "
        f"and a persistent athlete profile that contains your accumulated observations about them over time.\n\n"
        f"Tone: {tone}. Use {coaching['units']} units. "
        f"Athlete's current goal: {coaching['goal']}. Current training focus: {coaching['focus']}.\n\n"
        f"The ATHLETE PROFILE is your long-term memory. It contains PRs, injury history, and observations "
        f"you've built up over time about this athlete's tendencies, strengths, and weaknesses. "
        f"Reference it when relevant. Update it when you learn something new or permanent — a new PR, "
        f"an injury pattern, a recurring tendency. Keep observations concise and specific.\n\n"
        f"The COACH NOTES are your short-term working memory (expires after ~3 weeks). "
        f"Use them for recent context, current training phase notes, and temporary flags.\n\n"
        f"When writing daily updates: analyze the data thoroughly — comment on sleep quality, "
        f"HRV deviation from baseline (a key recovery signal), body battery, stress, and break down "
        f"each recent run with specific observations (pace, HR, effort level, any noteworthy patterns). "
        f"Identify trends across the week. Write like a knowledgeable coach who knows this athlete well — "
        f"use the data and profile together to tell them something they might not have noticed themselves.\n\n"
        f"The training plan is a REFERENCE GUIDELINE only — treat it as 'if you don't know what to do, do this.' "
        f"The athlete's actual biometric data, recent workouts, and recovery signals always take priority. "
        f"If the data says rest, say rest even if the plan says run.\n\n"
        f"IMPORTANT: Never modify or rewrite the training plan. It is read-only. Do not include <updated_plan>.\n\n"
        f"RESPONSE FORMAT — always use these XML tags exactly:\n"
        f"<coaching_message>\n"
        f"Your coaching message here. Natural sentences, no bullet points.\n"
        f"</coaching_message>\n\n"
        f"<updated_notes>\n"
        f"Full updated coach_notes.md content (include all existing notes plus any new ones).\n"
        f"</updated_notes>\n\n"
        f"<updated_profile>\n"
        f"Full updated athlete_profile.md content. Only include this tag if something permanent changed "
        f"(new PR, new injury observation, new long-term pattern identified). Omit if nothing changed.\n"
        f"</updated_profile>"
    )


def daily_update(
    garmin_formatted: str,
    training_plan: str,
    coach_notes: str,
    athlete_profile: str,
    recent_logs: str = "",
) -> str:
    config = load_config()
    claude_cfg = config["claude"]
    g_cfg = config["garmin"]
    tz_str = config["schedule"]["timezone"]

    client = _get_client()

    logs_section = (
        f"TRAINING HISTORY (last 30 days of daily logs):\n{recent_logs}\n\n"
        if recent_logs and recent_logs != "No daily logs yet."
        else ""
    )

    user_prompt = (
        f"Date: {get_local_date(tz_str)}\n\n"
        f"GARMIN DATA (last {g_cfg['lookback_days']} days):\n{garmin_formatted}\n\n"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"ATHLETE PROFILE:\n{athlete_profile}\n\n"
        f"{logs_section}"
        f"Write a thorough daily coaching update. Cover:\n"
        f"1. Recovery status — interpret HRV deviation, sleep score/stages, body battery together as a picture of readiness\n"
        f"2. Workout breakdown — for EACH recent run, go lap by lap if lap data is available. "
        f"   Identify the warmup, work segment, and cooldown. Comment on whether paces and HRs match the intended effort. "
        f"   Flag anything unusual — HR spikes, pacing drift, a lap that looks off.\n"
        f"3. Week-level trend — compare this week's volume and load to recent history. "
        f"   Note consistency, fatigue accumulation, or positive adaptation signals. "
        f"   Reference the athlete profile if patterns match what you know about them.\n"
        f"4. Today's recommendation — specific and actionable. Reference the plan as a default, "
        f"   but override it if the data says otherwise.\n\n"
        f"Be direct and specific. Reference actual numbers. "
        f"Max ~{claude_cfg['daily_update']['max_sentences']} sentences total."
    )

    if os.getenv("DRY_RUN") == "1":
        print("=== SYSTEM PROMPT ===")
        print(build_system_prompt(config))
        print("\n=== USER PROMPT ===")
        print(user_prompt)
        print("===================\n")
        return "<coaching_message>DRY RUN — no API call made.</coaching_message>"

    message = client.messages.create(
        model=claude_cfg["model"],
        max_tokens=claude_cfg["max_tokens"],
        system=build_system_prompt(config),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def respond_to_user(
    user_message: str,
    training_plan: str,
    coach_notes: str,
    athlete_profile: str,
    conversation_history: list[dict] | None = None,
    garmin_formatted: str = "",
) -> str:
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]

    client = _get_client()

    garmin_section = (
        f"GARMIN DATA (current):\n{garmin_formatted}\n\n"
        if garmin_formatted
        else ""
    )

    context_prompt = (
        f"Date: {get_local_date(tz_str)}\n\n"
        f"{garmin_section}"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"ATHLETE PROFILE:\n{athlete_profile}\n\n"
        f"Respond naturally as their coach. Keep it conversational — this is a text exchange, not a report.\n\n"
        f"If the athlete shares anything about a workout (how it felt, effort level, RPE, what went well or poorly), "
        f"acknowledge it and use <feedback_log> to record a concise summary (1-2 lines max). "
        f"If they mention a PR, injury, or schedule change, do the same and update the profile.\n\n"
        f"If Garmin data is available and the question is about readiness, today's workout, or how hard to push, "
        f"use the data — reference HRV, sleep, and body battery directly rather than speaking in generalities.\n\n"
        f"RESPONSE FORMAT for conversational replies:\n"
        f"<coaching_message>Your reply here.</coaching_message>\n\n"
        f"<updated_notes>Full updated coach_notes.md (include existing + any new entries).</updated_notes>\n\n"
        f"<updated_profile>Only if something permanent changed.</updated_profile>\n\n"
        f"<feedback_log>Only if the athlete shared workout feedback, an RPE, injury update, or result worth logging. "
        f"Write a single concise line, e.g. 'RPE 8 — tempo felt hard, heavy legs'.</feedback_log>"
    )

    # Build message list: inject context as a system-level user turn, then history, then current message
    messages = [{"role": "user", "content": context_prompt}, {"role": "assistant", "content": "Got it, I have your context loaded."}]

    if conversation_history:
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

    message = client.messages.create(
        model=claude_cfg["model"],
        max_tokens=claude_cfg["max_tokens"],
        system=build_system_prompt(config),
        messages=messages,
    )
    return message.content[0].text

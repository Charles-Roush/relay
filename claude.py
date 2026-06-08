import os
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv(override=True)

_client: anthropic.Anthropic | None = None


def load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


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
    sport = coaching["sport"]
    units = coaching["units"]

    return f"""\
You are an expert {sport} coach with deep knowledge of exercise physiology, periodization, and data-driven training. \
You have a long-term coaching relationship with this athlete and a persistent memory of their history, tendencies, and development.

You have access to:
- Garmin biometric data (sleep, HRV, body battery, stress, activity records with lap splits)
- A structured training plan (reference only — you can and should override it based on data)
- Rolling coach notes (short-term working memory, ~3 weeks)
- Athlete profile (permanent long-term memory: PRs, injury history, race history, training observations)
- Weekly reflections (your own written summaries of each training week)
- Recent daily logs (30-day history of coaching analysis and athlete feedback)

**Coaching philosophy:**
- Training adaptation happens through the right stimulus at the right time. Consistency beats heroics.
- HRV is the most reliable daily readiness signal. A significant drop (>5ms below rolling average) means the body hasn't recovered — reduce load even if the plan says otherwise.
- Sleep quality (deep + REM hours, not just total) matters more than total sleep time for recovery assessment.
- Body battery and stress together indicate cumulative load. Low battery + high stress = systemic fatigue, not just muscular.
- For a developing aerobic athlete, the majority of runs should be easy (aerobic TE < 2.0). Too many moderate/hard days is the primary driver of overtraining.
- Aerobic base development requires patience. Pushing pace on easy days feels productive but blunts adaptation.
- Lap-by-lap HR and pace data reveals effort honesty: HR drift across a run means the pace was too fast for the intended zone even if it felt easy.
- TSS is a useful acute load proxy but incomplete — cross-reference with HRV trend to assess actual recovery cost.
- Cadence is an injury risk signal: consistently low cadence (<170 spm) increases impact stress on developing legs.

**How to use the data:**
- When analyzing a run, go lap by lap: identify warmup, steady state, and cooldown. Flag HR creep, pacing drift, or anomalous laps.
- HRV trend over the past week matters more than a single reading. A steady decline signals accumulating fatigue before the athlete feels it.
- Compare this week's load distribution (easy/moderate/hard) against the past 14 days. Flag imbalances.
- When the athlete shares how a workout felt, reconcile their subjective experience with what the biometrics show — agreement is reassuring, disagreement is data.
- Use the weekly reflections and daily logs to identify patterns that don't show up in a single session: e.g., HR consistently running high on Monday runs, sleep consistently poor before hard days.

**Memory management:**
- ATHLETE PROFILE is permanent long-term memory. Update it when you learn something lasting: a new PR, a confirmed injury pattern, a recurring tendency. Keep it specific and factual.
- COACH NOTES are working memory (expire ~3 weeks). Use for current phase context, temporary flags, and anything relevant to the next few weeks.
- WEEKLY REFLECTION is your own written summary of each week — reference it when analyzing trends.

**Tone:** {tone}. Use {units} units.
**Current goal:** {coaching['goal']}
**Training focus:** {coaching['focus']}

**Output format — always use these XML tags exactly:**

<coaching_message>
Your message to the athlete. Natural sentences, no bullet points. Reference actual numbers. Be direct and specific.
</coaching_message>

<updated_notes>
Full updated coach_notes.md content (all existing notes plus any new ones, dated [YYYY-MM-DD]).
</updated_notes>

<updated_profile>
Full updated athlete_profile.md content. ONLY include this tag if something permanent changed — new PR, confirmed injury pattern, new long-term observation. Omit entirely if nothing changed.
</updated_profile>

The training plan is READ-ONLY. Never include <updated_plan>."""


def daily_update(
    garmin_formatted: str,
    training_plan: str,
    coach_notes: str,
    athlete_profile: str,
    recent_logs: str = "",
    weekly_reflection: str = "",
    has_talked_today: bool = False,
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

    reflection_section = (
        f"WEEKLY REFLECTIONS (your own summaries):\n{weekly_reflection}\n\n"
        if weekly_reflection
        else ""
    )

    conversation_note = (
        "Note: You've already exchanged messages with this athlete earlier today. "
        "Acknowledge that briefly and don't repeat things already discussed.\n\n"
        if has_talked_today
        else ""
    )

    user_prompt = (
        f"Date: {get_local_date(tz_str)}\n\n"
        f"{conversation_note}"
        f"GARMIN DATA (last {g_cfg['lookback_days']} days):\n{garmin_formatted}\n\n"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"ATHLETE PROFILE:\n{athlete_profile}\n\n"
        f"{reflection_section}"
        f"{logs_section}"
        f"Write a thorough daily coaching update. Cover:\n"
        f"1. Recovery status — read HRV deviation, sleep stage breakdown (deep + REM), body battery, and stress "
        f"   together as a single readiness picture. Call out the most important signal.\n"
        f"2. Workout breakdown — for EACH recent run, go lap by lap if data is available. "
        f"   Identify warmup / steady state / cooldown. Check for HR drift (pacing too fast for zone), "
        f"   anomalous laps, or effort that doesn't match the intended session. Flag anything worth noticing.\n"
        f"3. Load trend — use the 14-day load distribution (easy/moderate/hard by aerobic TE) to assess "
        f"   whether the training balance is appropriate. Flag if there are too many moderate/hard days relative "
        f"   to easy days, or if total load is climbing faster than the athlete can absorb.\n"
        f"4. Today's recommendation — specific and actionable. Reference the training plan as a default. "
        f"   Override it if recovery signals say otherwise. If prescribing a workout, give target pace/effort/HR.\n"
        f"5. Tomorrow preview — one sentence. What's coming up and what to know going in.\n\n"
        f"Reference actual numbers throughout. Be direct. "
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
    weekly_reflection: str = "",
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

    reflection_section = (
        f"WEEKLY REFLECTIONS (your own summaries):\n{weekly_reflection}\n\n"
        if weekly_reflection
        else ""
    )

    context_prompt = (
        f"Date: {get_local_date(tz_str)}\n\n"
        f"{garmin_section}"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"ATHLETE PROFILE:\n{athlete_profile}\n\n"
        f"{reflection_section}"
        f"Respond as their coach. This is a text exchange — keep it conversational, not a report. "
        f"When answering questions about readiness or effort, use the biometric data directly rather than speaking in generalities.\n\n"
        f"If the athlete shares workout feedback (how it felt, RPE, effort level), acknowledge it and record it in <feedback_log>.\n"
        f"If they mention a PR, injury, or significant schedule change, update the profile.\n\n"
        f"RESPONSE FORMAT:\n"
        f"<coaching_message>Your reply here.</coaching_message>\n\n"
        f"<updated_notes>Full updated coach_notes.md (existing + any new entries).</updated_notes>\n\n"
        f"<updated_profile>Only if something permanent changed.</updated_profile>\n\n"
        f"<feedback_log>Only if the athlete shared workout feedback, RPE, injury update, or notable result. "
        f"One concise line, e.g. 'RPE 8 — tempo felt hard, heavy legs'.</feedback_log>"
    )

    messages = [
        {"role": "user", "content": context_prompt},
        {"role": "assistant", "content": "Got it, I have your context loaded."},
    ]

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


def generate_weekly_reflection(
    garmin_formatted: str,
    training_plan: str,
    coach_notes: str,
    athlete_profile: str,
    recent_logs: str,
    week_label: str,
) -> str:
    """
    Generate a weekly reflection paragraph. Called Sunday evening.
    Returns raw Claude response (parse coaching_message tag for the reflection text).
    """
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    client = _get_client()

    user_prompt = (
        f"Date: {get_local_date(tz_str)} (end of {week_label})\n\n"
        f"GARMIN DATA (last 10 days):\n{garmin_formatted}\n\n"
        f"TRAINING PLAN:\n{training_plan}\n\n"
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"ATHLETE PROFILE:\n{athlete_profile}\n\n"
        f"TRAINING HISTORY (last 14 days):\n{recent_logs}\n\n"
        f"Write a weekly reflection paragraph (3-5 sentences) covering: what the week's training theme was, "
        f"how the athlete responded to load, the most important thing you observed (positive or negative), "
        f"and what to carry into next week. Write it as a note to yourself — this will become part of your "
        f"long-term memory and will be referenced in future daily updates. Be specific: cite actual workouts, "
        f"metrics, and patterns. Start with the week label, e.g. '### {week_label}'.\n\n"
        f"Use only <coaching_message> tags for your reflection. Do not include updated_notes or updated_profile."
    )

    message = client.messages.create(
        model=claude_cfg["model"],
        max_tokens=1000,
        system=build_system_prompt(config),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def post_workout_checkin(
    activity_summary: str,
    coach_notes: str,
    athlete_profile: str,
) -> str:
    """
    Generate a short proactive post-workout check-in message.
    One or two sentences max — just acknowledge the workout and ask how it felt.
    """
    config = load_config()
    claude_cfg = config["claude"]
    client = _get_client()

    user_prompt = (
        f"COACH NOTES:\n{coach_notes}\n\n"
        f"ATHLETE PROFILE:\n{athlete_profile}\n\n"
        f"Recent activity detected:\n{activity_summary}\n\n"
        f"Write a single short message (1-2 sentences) acknowledging the workout and asking how it felt. "
        f"Don't give analysis yet — just check in. Be natural, not clinical. "
        f"Use only <coaching_message> tags."
    )

    message = client.messages.create(
        model=claude_cfg["model"],
        max_tokens=200,
        system=build_system_prompt(config),
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
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


def get_local_datetime(timezone_str: str) -> tuple[str, str, str]:
    """Returns (date_iso, weekday, time_hhmm)."""
    now = datetime.now(ZoneInfo(timezone_str))
    return now.date().isoformat(), now.strftime("%A"), now.strftime("%H:%M")


def get_local_date(timezone_str: str) -> str:
    return datetime.now(ZoneInfo(timezone_str)).date().isoformat()


def _debug_cfg(config: dict) -> dict:
    """Return the debug config block with safe defaults."""
    return config.get("debug", {})


def _log_prompt(label: str, system_prompt: str, user_prompt: str, config: dict):
    """
    If debug.enabled is true, log the full system + user prompt before an API call.
    Writes to stdout always. Also appends to logs/debug_prompts.log if debug.log_to_file is true.
    """
    dbg = _debug_cfg(config)
    if not dbg.get("enabled", False) and os.getenv("DEBUG") != "1":
        return

    separator = "=" * 72
    output = (
        f"\n{separator}\n"
        f"DEBUG PROMPT — {label}  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
        f"{separator}\n"
        f"[SYSTEM PROMPT]\n{system_prompt}\n\n"
        f"[USER PROMPT]\n{user_prompt}\n"
        f"{separator}\n"
    )

    print(output)

    if dbg.get("log_to_file", True):
        try:
            log_dir = Path(config.get("paths", {}).get("logs_dir", "logs"))
            log_dir.mkdir(exist_ok=True)
            with open(log_dir / "debug_prompts.log", "a") as f:
                f.write(output)
        except Exception as e:
            logging.warning(f"Could not write debug log: {e}")


def _is_dry_run(config: dict) -> bool:
    """True if dry_run is set in config or via DRY_RUN=1 env var."""
    return config.get("debug", {}).get("dry_run", False) or os.getenv("DRY_RUN") == "1"


def _call_claude(
    label: str,
    system_prompt: str,
    user_prompt: str,
    config: dict,
    max_tokens: int | None = None,
    extra_messages: list[dict] | None = None,
) -> str:
    """
    Central Claude API call. Handles debug logging and dry-run before calling.
    extra_messages: optional list of messages to prepend before the final user turn
                    (used by respond_to_user for conversation history).
    """
    _log_prompt(label, system_prompt, user_prompt, config)

    if _is_dry_run(config):
        print(f"[DRY RUN] Skipping API call for: {label}")
        return f"<coaching_message>DRY RUN — {label} — no API call made.</coaching_message><updated_notes>{{}}</updated_notes>"

    claude_cfg = config["claude"]
    tokens = max_tokens or claude_cfg["max_tokens"]

    if extra_messages:
        messages = extra_messages + [{"role": "user", "content": user_prompt}]
    else:
        messages = [{"role": "user", "content": user_prompt}]

    response = _get_client().messages.create(
        model=claude_cfg["model"],
        max_tokens=tokens,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


def build_system_prompt(config: dict) -> str:
    coaching = config["coaching"]
    tone = config["claude"]["daily_update"]["tone"]
    sport = coaching["sport"]
    units = coaching["units"]

    return f"""\
You are an expert {sport} coach with deep knowledge of exercise physiology, periodization, and data-driven training. \
You have a long-term coaching relationship with this athlete and a persistent memory of their history, tendencies, and development.

**Coaching philosophy:**
- Training adaptation happens through the right stimulus at the right time. Consistency beats heroics.
- HRV is the most reliable daily readiness signal. A significant drop (>5ms below rolling average) means the body hasn't recovered — reduce load even if the plan says otherwise.
- Sleep quality (deep + REM hours, not just total) matters more than total sleep time for recovery assessment. Deep sleep drives physical recovery; REM drives cognitive and nervous system recovery. Poor deep sleep before a hard workout is a stronger warning sign than poor total sleep.
- Body battery and stress together indicate cumulative load. Low battery + high stress = systemic fatigue, not just muscular.
- Resting HR is a lagging but reliable fitness and recovery indicator. A resting HR elevated >3–4 bpm above the rolling average, especially paired with low HRV, is a strong sign of incomplete recovery or early illness.
- Training Readiness score synthesizes HRV, sleep, load history, and stress into a single 0–100 score. Scores below 40 warrant significant load reduction; scores above 70 mean the body is primed for quality work.
- VO2 max trend over weeks indicates whether the aerobic base is responding to training.
- Lactate threshold (LT) HR and pace are the most important performance anchors. LT pace defines the ceiling for tempo and threshold work. If no LT test exists, estimate from the HRV-based training zones or race data in the athlete profile.
- Recovery Time (hours) is Garmin's estimate of when the body will be ready for another hard session. Treat it as a floor, not a ceiling — high recovery time combined with poor HRV/sleep means the estimate may even be optimistic.
- Training Status (Productive, Maintaining, Recovering, Overreaching, Detraining, Peaking) is a rolling fitness/fatigue signal. "Productive" means training is building fitness; "Overreaching" means the load is exceeding recovery — act on it immediately.
- Load Focus shows the distribution across aerobic base, tempo, threshold, and anaerobic buckets. A developing runner should sit heavily in the base bucket. Disproportionate threshold or anaerobic load without matching base is a recipe for injury.
- ACWR (Acute:Chronic Workload Ratio) is the most important injury-risk metric. The optimal range is 0.8–1.3. Above 1.5 is high injury risk — the athlete is doing significantly more than their body is conditioned for. Below 0.8 means undertraining. Track the direction, not just the snapshot.
- For a developing aerobic athlete, the majority of runs should be easy (aerobic TE < 2.0). More than 2 moderate/hard sessions in 7 days is usually too much.
- Aerobic base development requires patience. Pushing pace on easy days feels productive but blunts adaptation.
- Lap-by-lap HR and pace data reveals effort honesty: HR drift across a run means the pace was too fast for the intended zone even if it felt easy.
- Ground contact time (GCT) reflects running economy and fatigue. High GCT (>250ms) or GCT creeping up across laps = fatigue or form breakdown.
- Vertical oscillation should be minimal (ideally <90mm at easy paces). Vertical ratio below 8% is good; above 10% is a form flag.
- Cadence below 170 spm increases impact stress. Flag it consistently.
- Subjective RPE and feel are ground truth. Always reconcile them with objective data — disagreement is important information.
- Respiration rate during sleep (>18 br/min) and SpO2 (<95%) are early warning signs of illness, overreaching, or poor respiratory recovery. Flag them before making intensity recommendations.
- Weather context (temperature, humidity) belongs in effort interpretation: HR runs approximately 1 bpm higher per 5°F above 55°F. Humidity above 70% compounds cardiovascular strain. A "hard" run at 85°F + 80% humidity may have been correctly paced.
- The activity pattern (run/rest rhythm) matters as much as total volume. Running 7 days straight with no recovery is a structural problem regardless of easy pacing. Flag it.
- Cross-reference RHR trend with HRV: if both trend worse simultaneously (RHR up, HRV down), that's a strong overreaching signal.
- Use ACWR to frame workload risk: above 1.3 with declining HRV = injury-risk window even if the athlete feels fine. Below 0.8 with "Detraining" status = insufficient stimulus.
- Recovery Time is a floor for spacing hard sessions. Don't schedule threshold work inside a 48h recovery window unless HRV and Training Readiness strongly support it.
- Training Status is a slow-moving signal. "Overreaching" warrants reducing intensity now, not just one easy day. "Productive" with good HRV is the green light for quality work.
- Load Focus distribution matters: for a base-phase 5K athlete, base load should dominate. Disproportionate threshold/anaerobic spikes without matching base = distribution problem, not just volume.
- Anchor tempo and threshold targets to LT pace when available. It's more precise than "comfortably hard."
- When analyzing a run, compare average HR and peak lap HR to the LT HR. Running threshold intervals above LT HR is a form flag; running them well below means the athlete may be sandbagging.

**Training plan philosophy:**
- The training plan is a weekly guideline — a suggested volume and session mix for the week, not a strict day-by-day schedule. Days can shift based on readiness, weather, or life.
- Coach to the week as a whole: the right sessions need to happen, but not necessarily on the exact prescribed day.
- Use the plan as a reference for weekly volume and session mix. Coach based on what the data says, using the plan as context for what kind of week was intended.
- Suggest an alternative week structure only when there is a strong and clear reason: ACWR above 1.4, Training Status "Overreaching", HRV declining for 3+ consecutive days, or the athlete is clearly undertrained and the plan is too conservative. Do this rarely.
- When suggesting an alternative, keep it minimal: drop one run, swap a quality day for easy. Never rewrite the whole week.
- Never rewrite or modify the training plan file.

**Memory management — be selective:**
- COACH NOTES: only flag things not already in the Garmin data or logs — injury hints, travel, context that won't be visible later. Most responses should not add notes.
- ATHLETE PROFILE: permanent observations only — new PRs, confirmed injury patterns, long-term tendencies. Omit tag if nothing changed.
- WEEKLY REFLECTION: your own summaries of each week — write for your future self, not the athlete.

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
Full updated athlete_profile.md content. ONLY include this tag if something permanent changed. Omit entirely if nothing changed.
</updated_profile>

<feedback_log>
Include ONLY when the athlete shares subjective feedback in this message — how a workout felt, RPE, energy level, pain, etc.
Write a concise one-line summary (e.g. "easy run felt heavy, RPE 7, legs tired"). Omit entirely if no subjective feedback was shared.
The athlete does NOT need to use any command — if they simply reply describing how something felt, capture it here.
</feedback_log>

The training plan is READ-ONLY. Never include <updated_plan>."""


# ── Morning Daily Update ───────────────────────────────────────────────────────

def daily_update(
    garmin_formatted: str,
    plan_context: dict,
    coach_notes: str,
    athlete_profile: str,
    recent_logs: str = "",
    weekly_reflection: str = "",
    has_talked_today: bool = False,
) -> str:
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    daily_logs_days = claude_cfg.get("daily_logs_days", 30)
    load_trend_days = claude_cfg.get("load_trend_days", 14)
    lookback_days = config["garmin"].get("lookback_days", 10)
    tone = claude_cfg["daily_update"]["tone"]
    units = config["coaching"]["units"]
    goal = config["coaching"]["goal"]
    max_sentences = claude_cfg["daily_update"]["max_sentences"]

    date_iso, weekday, time_str = get_local_datetime(tz_str)

    already_talked = (
        "Note: you've already exchanged messages with this athlete earlier today — "
        "don't repeat things already covered.\n"
        if has_talked_today else ""
    )

    logs_block = (
        f"{recent_logs}"
        if recent_logs and recent_logs != "No daily logs yet."
        else "None yet."
    )

    reflection_block = weekly_reflection if weekly_reflection else "None yet."

    user_prompt = f"""\
{already_talked}
=== DATE & PLAN ===
{weekday}, {date_iso} at {time_str}
Goal: {goal}

This week's sessions (context — use as a volume and intensity reference, not a daily schedule):
{plan_context["week_block"]}

=== RECOVERY — LAST NIGHT & CURRENT STATE ===
{garmin_formatted}

=== TRAINING HISTORY ===
Last {load_trend_days}-day load distribution is in the Garmin data above.
Recent daily logs (last {daily_logs_days} days):
{logs_block}

=== LONG-TERM MEMORY ===
Weekly reflections (your own summaries):
{reflection_block}

Coach notes:
{coach_notes}

Athlete profile:
{athlete_profile}

=== INSTRUCTIONS ===
Write the morning coaching update. Structure your response as follows:

1. RECOVERY & READINESS — synthesize all signals into one verdict:
   - HRV: latest vs {lookback_days}-day avg, direction, Garmin status
   - Sleep: last night deep + REM hours and score; multi-night trend
   - If respiration or SpO2 flags appear (⚠ markers in the data), name them explicitly — elevated
     respiration (>18 br/min) or low SpO2 (<95%) are early illness/overreaching signals and should
     be addressed before discussing training intensity.
   - Resting HR: latest vs rolling avg and direction
   - Training Readiness score and what it implies
   - Body battery: start value and current; note the 7-day trend direction if net negative
   - Stress: today vs rolling average
   - Training Status (Productive / Maintaining / Recovering / Overreaching / Detraining)
   - Recovery Time remaining (hours) — does today's plan fit inside or outside the recovery window?
   Name the single most important signal and what it means for today.

2. WORKLOAD RISK — one clear statement on where the athlete stands:
   - ACWR: cite the value and its flag (optimal / elevated / high injury risk / undertraining)
   - Acute vs chronic load numbers
   - Load Focus: is the distribution appropriate for the current training phase?

3. RECENT RUN ANALYSIS — for each run in the data, go lap by lap:
   - Identify warmup / steady state / cooldown
   - HR drift across laps (flag if >10 bpm rise); compare to LT HR if available
   - HR zone distribution: if provided (Z1–Z5 by LT%), use it to characterize the effort fingerprint.
     An "easy" run spending >20% in Z3+ is a pacing flag regardless of how it felt.
   - Cadence — flag if consistently below 170 spm
   - GCT — flag if elevated or increasing across laps; stride length shortening = fatigue signal
   - Vertical oscillation / vertical ratio if present
   - Weather context: if provided, factor temperature/humidity into HR interpretation.
     HR runs ~1 bpm higher per 5°F above 55°F at the same effort. Humidity >70% compounds this.
   - Reconcile with any logged RPE or feel notes

4. LOAD BALANCE — assess the {load_trend_days}-day easy/moderate/hard distribution and activity pattern.
   The activity pattern (R = run, · = rest) shows the actual rhythm — flag if rest days are clustered
   or if the athlete is running every day without recovery. The plan is a weekly volume/intensity guideline;
   use the actual pattern + load data to flag structural issues, not to grade day-by-day adherence.

5. TODAY'S RECOMMENDATION — specific and actionable.
   Use the week's session menu as context. Recommend whatever makes the most sense for today given the data — which session from this week fits the athlete's current readiness. Most of the time this will match the plan's day order naturally; don't force it either way.
   Only suggest restructuring the whole week if signals are strongly negative (ACWR >1.4, Overreaching status, 3+ days of declining HRV) or strongly positive (athlete significantly undertrained). Keep alternatives minimal — drop or swap one session.
   If prescribing threshold or tempo work, anchor to LT pace ({units}). Give HR ceiling relative to LT HR.

6. TOMORROW PREVIEW — one sentence.

Reference actual numbers. Be direct and specific — cite metrics, don't describe them.
Keep each section to 1–3 sentences. Use short, declarative sentences over bullet lists.
Tone: {tone}. Units: {units}.
"""

    return _call_claude("daily_update", build_system_prompt(config), user_prompt, config, max_tokens=800)


# ── Post-Workout Check-In ──────────────────────────────────────────────────────

def post_workout_checkin(
    activity_summary: str,
    coach_notes: str,
    athlete_profile: str,
    garmin_formatted: str = "",
    plan_context: dict | None = None,
    recent_logs: str = "",
    weekly_reflection: str = "",
) -> str:
    """
    Proactive check-in fired after a new workout is detected.
    Uses the afternoon/evening check-in template — light, conversational, prompts for RPE/feel.
    """
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    tone = claude_cfg["daily_update"]["tone"]
    units = config["coaching"]["units"]
    goal = config["coaching"]["goal"]

    date_iso, weekday, time_str = get_local_datetime(tz_str)

    body_state_block = garmin_formatted if garmin_formatted else "Not available."
    logs_block = recent_logs if recent_logs and recent_logs != "No daily logs yet." else "None yet."
    reflection_block = weekly_reflection if weekly_reflection else "None yet."
    pc = plan_context or {}
    plan_block = (
        f"This week's sessions (context):\n{pc['week_block']}"
        if pc else "Not available."
    )

    user_prompt = f"""\
=== DATE & CONTEXT ===
{time_str} on {weekday}, {date_iso}
Goal: {goal}

=== ACTIVITY DETECTED ===
{activity_summary}

=== PLAN CONTEXT ===
{plan_block}

=== ATHLETE BODY STATE ===
{body_state_block}

=== RECENT TRAINING CONTEXT ===
Recent daily logs + feedback:
{logs_block}

Last weekly reflection:
{reflection_block}

=== COACH CONTEXT ===
Coach notes:
{coach_notes}

Athlete profile:
{athlete_profile}

=== INSTRUCTIONS ===
Send a short post-workout check-in (2–3 sentences max):
- Acknowledge the workout naturally — name the session type if you can infer it from the plan.
- If the workout fits a pattern from recent logs (e.g. consistently running this session well or poorly), briefly note it.
- Ask how it felt OR prompt for RPE if not yet logged. One question, not multiple.
- If body battery or stress data suggests today was taxing, briefly note recovery tonight matters.
- Do NOT give lap analysis or full coaching — that comes later. This is just a check-in.
- Conversational and warm, not clinical.
Tone: {tone}. Units: {units}.
"""

    return _call_claude("post_workout_checkin", build_system_prompt(config), user_prompt, config, max_tokens=300)


# ── Nightly Evening Check-In ───────────────────────────────────────────────────

def evening_checkin(
    garmin_formatted: str,
    plan_context: dict,
    coach_notes: str,
    athlete_profile: str,
    todays_feedback: str = "",
    recent_logs: str = "",
    weekly_reflection: str = "",
    morning_summary: str = "",
) -> str:
    """
    Scheduled nightly check-in. Reviews today's actual vs planned, gives execution
    feedback, sets up tomorrow. Fires regardless of whether a workout happened.
    """
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    tone = claude_cfg["daily_update"]["tone"]
    units = config["coaching"]["units"]
    goal = config["coaching"]["goal"]
    load_trend_days = claude_cfg.get("load_trend_days", 14)
    max_sentences = claude_cfg.get("evening_checkin", {}).get("max_sentences", 6)

    date_iso, weekday, time_str = get_local_datetime(tz_str)

    garmin_block = garmin_formatted if garmin_formatted else "Not available."
    logs_block = recent_logs if recent_logs and recent_logs != "No daily logs yet." else "None yet."
    reflection_block = weekly_reflection if weekly_reflection else "None yet."
    feedback_block = todays_feedback if todays_feedback else "None logged today."
    morning_block = morning_summary if morning_summary else "Not available."

    user_prompt = f"""\
=== TODAY ===
{time_str} on {weekday}, {date_iso}
Goal: {goal}

=== TRAINING PLAN CONTEXT ===
This week's sessions (context — volume and intensity reference, not a day-by-day schedule):
{plan_context["week_block"]}

=== TODAY'S GARMIN DATA (full) ===
All recovery metrics (sleep, HRV, resting HR, body battery, stress, training readiness,
training status, recovery time, ACWR, load focus) AND all activity data (distance, pace,
HR, cadence, TSS, TE, lap splits, HR zones, running dynamics, weather context) are below.
{load_trend_days}-day load trend and activity pattern are also included.
{garmin_block}

=== ATHLETE INPUT TODAY ===
Logged feel/RPE/notes (from /felt, /rpe, /note commands or prior messages today):
{feedback_block}

This morning's coaching read:
{morning_block}

=== TRAINING CONTEXT ===
Recent daily logs (last 30 days):
{logs_block}

Last weekly reflection:
{reflection_block}

=== COACH CONTEXT ===
Coach notes:
{coach_notes}

Athlete profile:
{athlete_profile}

=== INSTRUCTIONS ===
Write the evening check-in. STRICT limit: {max_sentences} sentences maximum. Be conversational, not a report.

- If they ran today: briefly assess execution in 1–2 sentences using the most important signal
  (HR zone split, pace vs plan, HR drift, or weather-adjusted effort). Pick one thing, not all of them.

- If no run but one was planned: acknowledge it in one sentence, ask what happened.

- One sentence connecting today's body state to tomorrow (Training Status / Recovery Time if notable).

- Close with one natural question about how the workout felt. The athlete replies directly — no commands needed.

Max {max_sentences} sentences total. Tone: {tone}. Units: {units}.
"""

    return _call_claude("evening_checkin", build_system_prompt(config), user_prompt, config, max_tokens=600)


# ── On-Demand Readiness Flash Check (/today) ──────────────────────────────────

def today_readiness_check(
    plan_context: dict,
    coach_notes: str,
    athlete_profile: str,
    garmin_formatted: str = "",
    recent_logs: str = "",
    weekly_reflection: str = "",
) -> str:
    """
    Fast on-demand readiness verdict. GREEN / YELLOW / RED + one reason + modification if needed.
    """
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    tone = claude_cfg["daily_update"]["tone"]
    units = config["coaching"]["units"]
    goal = config["coaching"]["goal"]
    load_trend_days = claude_cfg.get("load_trend_days", 14)

    date_iso, weekday, time_str = get_local_datetime(tz_str)

    garmin_block = garmin_formatted if garmin_formatted else "Not available."
    logs_block = recent_logs if recent_logs and recent_logs != "No daily logs yet." else "None."
    reflection_block = weekly_reflection if weekly_reflection else "None yet."

    user_prompt = f"""\
=== DATE & PLAN ===
{time_str} on {weekday}, {date_iso}
Goal: {goal}

This week's sessions (context):
{plan_context["week_block"]}

=== CURRENT STATE ===
{garmin_block}

=== RECENT HISTORY ===
{load_trend_days}-day load trend is in the Garmin data above.
Recent logs:
{logs_block}

Last weekly reflection:
{reflection_block}

=== CONTEXT ===
Coach notes:
{coach_notes}

Athlete profile:
{athlete_profile}

=== INSTRUCTIONS ===
Give a fast readiness verdict. Lead with exactly one of:
  🟢 GREEN — go as planned
  🟡 YELLOW — modify (say how: pace, distance, or swap to easy)
  🔴 RED — rest or very easy only

Then 2–3 sentences max. Reference the specific metric(s) driving the call:
- HRV vs rolling average
- Training Readiness score
- Recovery Time remaining (hours) — if significant, name it
- ACWR flag (optimal / elevated / high injury risk) — if elevated or above 1.5, that alone can push YELLOW/RED
- Training Status (Overreaching / Recovering) — if present, factor it in
Name just the top 1–2 signals. No preamble. No summary at the end. This is a glance-able answer.
Tone: {tone}. Units: {units}.
"""

    return _call_claude("today_readiness_check", build_system_prompt(config), user_prompt, config, max_tokens=300)


# ── Conversational Coach (free-text messages) ─────────────────────────────────

def respond_to_user(
    user_message: str,
    plan_context: dict,
    coach_notes: str,
    athlete_profile: str,
    conversation_history: list[dict] | None = None,
    garmin_formatted: str = "",
    weekly_reflection: str = "",
    recent_logs: str = "",
) -> str:
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    tone = claude_cfg["daily_update"]["tone"]
    units = config["coaching"]["units"]
    goal = config["coaching"]["goal"]
    load_trend_days = claude_cfg.get("load_trend_days", 14)

    date_iso, weekday, time_str = get_local_datetime(tz_str)

    garmin_block = garmin_formatted if garmin_formatted else "Not available."
    reflection_block = weekly_reflection if weekly_reflection else "None yet."
    logs_block = recent_logs if recent_logs and recent_logs != "No daily logs yet." else "None yet."

    max_sentences = claude_cfg.get("respond", {}).get("max_sentences", 8)

    context_prompt = f"""\
=== ATHLETE CONTEXT ===
{weekday}, {date_iso} at {time_str}
Goal: {goal}

Recovery snapshot (sleep, HRV, body battery, RHR, stress, training readiness,
training status, recovery time, ACWR, load focus, lactate threshold):
{garmin_block}

{load_trend_days}-day load trend, ACWR, and load focus distribution are in the Garmin data above.

This week's sessions (context):
{plan_context["week_block"]}

Recent daily logs + feedback:
{logs_block}

Last weekly reflection:
{reflection_block}

Coach notes:
{coach_notes}

Athlete profile:
{athlete_profile}

=== INSTRUCTIONS ===
You are their coach responding to a direct message. Rules:
- Answer their actual question first. Be opinionated and specific — use their real numbers, not generic advice.
- If they ask whether to do/skip/modify a session, give a clear recommendation with the reasoning.
- If they share how something felt (RPE, effort, pain), acknowledge it and record it in <feedback_log>.
- If they mention a PR, injury, or schedule change, note it in the profile or coach notes as appropriate.
- Match their message's length and energy. Short question = short answer.
- Pull from their data (HRV, splits, mileage, load trend) only when directly relevant.
- STRICT limit: {max_sentences} sentences max. Most replies should be 3–5 sentences. If the question is simple, use fewer. Never write a list when a sentence will do.
Tone: {tone}. Units: {units}.
"""

    prefix_messages = [
        {"role": "user", "content": context_prompt},
        {"role": "assistant", "content": "Got it — I have your full context loaded."},
    ]
    if conversation_history:
        prefix_messages.extend(conversation_history)

    return _call_claude(
        "respond_to_user",
        build_system_prompt(config),
        user_message,
        config,
        max_tokens=450,
        extra_messages=prefix_messages,
    )


# ── Weekly Report ──────────────────────────────────────────────────────────────

def generate_weekly_reflection(
    garmin_formatted: str,
    plan_context: dict,
    coach_notes: str,
    athlete_profile: str,
    recent_logs: str,
    week_label: str,
    weekly_reflection: str = "",
) -> str:
    """
    Sunday evening weekly report. Returns raw Claude response containing:
      <athlete_summary>   — sent to Telegram
      <coaching_message>  — saved to weekly_reflection.md as long-term memory
    """
    config = load_config()
    claude_cfg = config["claude"]
    tz_str = config["schedule"]["timezone"]
    tone = claude_cfg["daily_update"]["tone"]
    units = config["coaching"]["units"]
    goal = config["coaching"]["goal"]
    load_trend_days = claude_cfg.get("load_trend_days", 14)

    date_iso, weekday, _ = get_local_datetime(tz_str)

    prior_reflections = weekly_reflection if weekly_reflection else "None yet."

    user_prompt = f"""\
=== WEEK ===
{week_label} (reporting date: {weekday}, {date_iso})
Goal: {goal}

=== THIS WEEK'S GARMIN DATA (full) ===
All activities with lap splits, HR zones, running dynamics, weather context,
load distribution, activity pattern, ACWR, Training Status, Load Focus,
sleep nightly breakdown + trend, HRV trajectory, body battery trend,
stress patterns, resting HR trend, lactate threshold:
{garmin_formatted}

=== ATHLETE INPUT ===
Logged RPE, feel, notes, and any PRs this week (from daily logs):
{recent_logs}

=== CONTEXT ===
This week's prescribed sessions (treat as guideline):
{plan_context["week_block"]}

Prior weekly reflections (for pattern continuity):
{prior_reflections}

Athlete profile:
{athlete_profile}

Coach notes:
{coach_notes}

=== INSTRUCTIONS ===
Produce two parts using these exact XML tags:

<athlete_summary>
What the athlete reads — 5–7 sentences max, no bullet points:
- What went well: specific sessions, consistency, pacing execution, recovery wins. Use numbers.
- What needs work: missed sessions, sleep/HRV red flags, intensity distribution problems, execution issues.
- If any respiration/SpO2 flags appeared this week, call them out — may indicate illness or accumulated fatigue.
- HR zone distribution across the week's runs: was the effort profile appropriate for the training phase?
  A developing runner should spend most time in Z1–Z2. Disproportionate Z3–Z5 = distribution flag.
- Activity pattern (R/rest rhythm): note if rest days were present and appropriately spaced.
- Training Status this week (Productive / Maintaining / Overreaching etc.) — what it means.
- ACWR over the week: was the workload trend safe, elevated, or building risk?
- Load Focus distribution: is the base/tempo/threshold/anaerobic split appropriate for the goal phase?
- One clear focus for next week tied directly to the goal.
- Honest and direct. Numbers over vibes.
</athlete_summary>

<coaching_message>
### {week_label}
Memory reflection — 2–3 sentences written for your future self, not the athlete:
Compress the week into durable patterns: how their body responded to load (cite actual HRV/sleep numbers),
what's trending (fitness, recovery, form), Training Status trajectory, ACWR direction, notable weather conditions
if they affected effort interpretation, and what to watch coming week.
Be specific: name the workouts, the metrics, the patterns.
This will be referenced in future daily updates — make it useful.
</coaching_message>

Tone: {tone}. Units: {units}.
"""

    return _call_claude("generate_weekly_reflection", build_system_prompt(config), user_prompt, config, max_tokens=900)

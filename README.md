# relay

AI running coach delivered over Telegram. Pulls real biometric data from Garmin Connect daily, feeds it to Claude alongside your training plan and rolling coach memory, and sends a personalized coaching update every morning. You can also chat with it throughout the day as a coach.

---

## What it does

- **Daily morning update** — automatically fires at a configured time. Pulls 10 days of Garmin data (sleep history, HRV trend, body battery, stress, recent runs with lap splits), runs it through Claude with your training plan and coach memory, and sends a coaching message to Telegram.
- **Proactive post-workout check-in** — hourly job detects new activities and sends a short message asking how the workout felt, before you say anything.
- **Weekly reflection** — Sunday evening, Claude writes a 3–5 sentence summary of the week's training that gets saved as long-term memory and referenced in future updates.
- **Conversational coaching** — send any message and Claude responds as your coach, with full access to your current biometrics and history.
- **Structured commands** — log RPE, workout feel, notes, and PRs via `/rpe`, `/felt`, `/note`, `/pr`. Get today's plan and readiness with `/today`, weekly summary with `/week`.

## Context Claude gets

Every update and conversation includes:

- **Sleep history** — multiple nights of data: total, deep, REM, light, awake hours, sleep score, SpO2, respiration rate, plus a rolling trend summary
- **HRV** — daily readings over the lookback window with rolling average, deviation from baseline, and recovery flag if >5ms below baseline
- **Body battery** — start and current value for the day
- **Stress** — daily average and max
- **Activities** — recent runs with distance, duration, average pace, HR (avg + max), cadence, elevation, TSS, aerobic/anaerobic training effect, and full lap-by-lap breakdown for the last 2 days
- **Load trend** — 14-day easy/moderate/hard distribution by aerobic TE with total mileage
- **Training plan** — full structured plan (read-only)
- **Coach notes** — rolling 3-week working memory (auto-expires old entries)
- **Athlete profile** — permanent long-term memory: PRs, injury history, training observations
- **Weekly reflections** — Claude's own weekly summaries, used as long-term pattern memory
- **Daily logs** — 30 days of past coaching analyses and athlete feedback

## Setup

### Requirements

- Python 3.11+
- Garmin Connect account (with a compatible device for HRV/sleep data)
- Anthropic API key
- Telegram bot token and your chat ID

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ANTHROPIC_API_KEY=...
GARMIN_EMAIL=...
GARMIN_PASSWORD=...
```

Edit `config.yaml` to set your timezone, daily update time, goal, coaching tone, and which Garmin metrics to pull.

### Run

```bash
python bot.py
```

On first run, it will log any missing credentials or files. The bot starts polling immediately. The daily update fires at the configured hour.

### Deploy

See the `deploy/` directory for systemd service configuration.

---

## Commands

| Command | Description |
|---------|-------------|
| `/today` | Today's plan + readiness check based on current biometrics |
| `/week` | Summary of this week's training |
| `/felt <description>` | Log how a workout felt |
| `/rpe <1-10> [note]` | Log RPE |
| `/note <text>` | Quick note (injury, missed workout, etc.) |
| `/pr <distance> <time>` | Log a personal record |
| `/status` | View current athlete profile and coach notes |
| `/settings` | View current config settings |
| `/set <key> <value>` | Update a setting (tone, goal, schedule, checkin) |
| `/refresh` | Bust Garmin cache, pull fresh data on next message |
| `/ping` | Health check — uptime and last daily update time |
| `/help` | Show command list |

Or just send any message to chat with your coach.

---

## File structure

```
bot.py              — Telegram bot, scheduler, command handlers
claude.py           — Claude API calls and prompt construction
garmin.py           — Garmin Connect data fetching and formatting
notes.py            — File I/O for coach notes, profile, plan, config
daily_log.py        — Per-day activity and coaching log
config.yaml         — All configuration
training_plan.md    — Structured training plan (read-only by Claude)
athlete_profile.md  — Permanent athlete memory (PRs, observations, goals)
coach_notes.md      — Rolling 3-week coach working memory
logs/               — Daily log files + Garmin cache
```

## Configuration reference

Key settings in `config.yaml`:

| Key | Description |
|-----|-------------|
| `schedule.hour` | Hour (0–23) to send daily update |
| `schedule.timezone` | Timezone string (e.g. `US/Eastern`) |
| `coaching.goal` | Current race/training goal |
| `coaching.tone` | `direct`, `encouraging`, or `detailed` |
| `coaching.units` | `imperial` or `metric` |
| `coaching.post_workout_checkin` | `true/false` — proactive post-run check-ins |
| `garmin.lookback_days` | Days of history for all metrics; activities fetched to match (2x internally) |
| `claude.model` | Claude model ID |
| `claude.daily_logs_days` | Days of daily logs to include in morning update |
| `claude.load_trend_days` | Days used for easy/moderate/hard load analysis |

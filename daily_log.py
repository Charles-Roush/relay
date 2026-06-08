"""
Permanent per-day activity and coaching log.
One file per day in logs/YYYY-MM-DD.md — never expires, never overwritten.
Claude reads recent entries as long-term context.
"""

import re
from datetime import date, timedelta
from pathlib import Path

LOGS_DIR = Path("logs")
_METRICS_BLOCK = re.compile(r'## Metrics\n\n```.*?```\n\n', re.DOTALL)


def _log_path(d: date) -> Path:
    return LOGS_DIR / f"{d.isoformat()}.md"


def ensure_logs_dir():
    LOGS_DIR.mkdir(exist_ok=True)


def write_daily_log(
    log_date: date,
    garmin_formatted: str,
    coaching_message: str,
    feedback_notes: list[str] | None = None,
):
    """
    Write or append to today's log file.
    Never overwrites — handles reruns safely.
    """
    ensure_logs_dir()
    path = _log_path(log_date)

    if path.exists():
        if feedback_notes:
            with path.open("a") as f:
                f.write("\n### Athlete Feedback\n")
                for note in feedback_notes:
                    f.write(f"- {note}\n")
        return

    feedback_section = ""
    if feedback_notes:
        lines = "\n".join(f"- {n}" for n in feedback_notes)
        feedback_section = f"\n### Athlete Feedback\n{lines}\n"

    content = (
        f"# {log_date.strftime('%A, %B %d %Y')}\n\n"
        f"## Metrics\n\n"
        f"```\n{garmin_formatted}\n```\n\n"
        f"## Coach Analysis\n\n"
        f"{coaching_message}\n"
        f"{feedback_section}"
    )

    path.write_text(content)


def append_feedback_to_log(log_date: date, feedback: str):
    """Append a single feedback entry to today's log (called from bot commands)."""
    ensure_logs_dir()
    path = _log_path(log_date)
    if path.exists():
        with path.open("a") as f:
            f.write(f"- [FEEDBACK] {feedback}\n")
    else:
        path.write_text(
            f"# {log_date.strftime('%A, %B %d %Y')}\n\n"
            f"## Athlete Feedback\n\n"
            f"- [FEEDBACK] {feedback}\n"
        )


def read_recent_logs(n_days: int = 30) -> str:
    """
    Read the last n_days of log files as a single string.
    Logs older than 7 days have their raw Garmin metrics stripped —
    the coaching analysis and feedback are what matter for long-term context.
    Most recent last so Claude sees progression forward in time.
    """
    ensure_logs_dir()
    today = date.today()
    entries = []

    for i in range(n_days, 0, -1):
        d = today - timedelta(days=i)
        path = _log_path(d)
        if not path.exists():
            continue
        text = path.read_text().strip()
        if i > 7:
            # Strip the verbose raw metrics block for older logs
            text = _METRICS_BLOCK.sub("", text).strip()
        entries.append(text)

    if not entries:
        return "No daily logs yet."

    return "\n\n---\n\n".join(entries)

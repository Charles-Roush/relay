from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

_DAY_ABBRS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_WEEKDAY_TO_ABBR = {
    "Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
    "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun",
}

_MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

import yaml


@contextmanager
def _file_lock(path: Path):
    """Exclusive advisory lock on a file while writing."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(exist_ok=True)
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

_PROFILE_TEMPLATE = """\
## Athlete Profile

### Personal Records
_No PRs logged yet._

### Injury History
_No injuries logged._

### Race History
_No races logged yet._

### Training Observations
_Claude will build observations here over time — pacing tendencies, recovery patterns, response to load, strengths and weaknesses._

### Goals & Context
_Updated by Claude as goals evolve._
"""

_WEEKLY_REFLECTION_HEADER = "## Weekly Reflections\n\n"


def load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def _paths(config: dict | None = None) -> dict:
    if config is None:
        config = load_config()
    return config.get("paths", {})


def _notes_file(config: dict | None = None) -> Path:
    return Path(_paths(config).get("notes_file", "coach_notes.md"))


def _plan_file(config: dict | None = None) -> Path:
    return Path(_paths(config).get("plan_file", "training_plan.md"))


def _profile_file(config: dict | None = None) -> Path:
    return Path(_paths(config).get("profile_file", "athlete_profile.md"))


def _weekly_reflection_file(config: dict | None = None) -> Path:
    return Path(_paths(config).get("weekly_reflection_file", "weekly_reflection.md"))


def read_notes() -> str:
    f = _notes_file()
    if not f.exists():
        f.write_text("## Coach Notes\n\n")
    return f.read_text()


def read_plan() -> str:
    f = _plan_file()
    if not f.exists():
        f.write_text("## Training Plan\n\nNo training plan yet.\n")
    return f.read_text()


def read_profile() -> str:
    f = _profile_file()
    if not f.exists():
        f.write_text(_PROFILE_TEMPLATE)
    return f.read_text()


def read_weekly_reflection() -> str:
    f = _weekly_reflection_file()
    if not f.exists():
        return ""
    return f.read_text()


def write_profile(content: str):
    f = _profile_file()
    with _file_lock(f):
        f.write_text(content if content.endswith("\n") else content + "\n")


def append_to_profile(text: str):
    """Append a raw line to the profile (used for PR logging etc.)."""
    f = _profile_file()
    with _file_lock(f):
        content = read_profile().rstrip()
        f.write_text(content + "\n" + text + "\n")


def write_notes(content: str):
    """Write notes, enforcing expiry and max_lines from config."""
    config = load_config()
    notes_cfg = config["claude"]["coach_notes"]
    expiry_days = notes_cfg["expiry_days"]
    max_lines = notes_cfg["max_lines"]

    cutoff = date.today() - timedelta(days=expiry_days)

    lines = content.splitlines()
    header_lines = []
    note_lines = []
    in_notes = False

    for line in lines:
        if line.strip() == "## Coach Notes":
            in_notes = True
            header_lines.append(line)
        elif in_notes:
            note_lines.append(line)
        elif not in_notes:
            header_lines.append(line)

    date_pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2})\]')
    filtered = []
    for line in note_lines:
        m = date_pattern.match(line)
        if m:
            try:
                note_date = date.fromisoformat(m.group(1))
                if note_date >= cutoff:
                    filtered.append(line)
            except ValueError:
                filtered.append(line)
        else:
            filtered.append(line)

    if len(filtered) > max_lines:
        filtered = filtered[-max_lines:]

    rebuilt = "## Coach Notes\n\n" + "\n".join(filtered) + "\n"
    f = _notes_file()
    with _file_lock(f):
        f.write_text(rebuilt)


def write_weekly_reflection(content: str):
    """Append a dated weekly reflection entry."""
    f = _weekly_reflection_file()
    with _file_lock(f):
        existing = f.read_text() if f.exists() else _WEEKLY_REFLECTION_HEADER
        if not existing.startswith("## Weekly Reflections"):
            existing = _WEEKLY_REFLECTION_HEADER + existing
        f.write_text(existing.rstrip() + "\n\n" + content.strip() + "\n")


def append_feedback_note(text: str):
    """Directly append a timestamped feedback entry to coach_notes without Claude."""
    today = date.today().isoformat()
    entry = f"[{today}] [FEEDBACK] {text}"
    content = read_notes()
    lines = content.rstrip().splitlines()
    lines.append(entry)
    write_notes("\n".join(lines) + "\n")


def extract_tag(response: str, tag: str) -> str:
    """Extract content from a named XML tag. Returns empty string if not found."""
    match = re.search(rf'<{tag}>(.*?)</{tag}>', response, re.DOTALL)
    return match.group(1).strip() if match else ""


def parse_claude_response(response: str) -> tuple[str, str, str, str, str]:
    """
    Parse XML-tagged Claude response.
    Returns (message, updated_notes, updated_profile, updated_plan, feedback_log).
    Falls back gracefully if tags are missing.
    """
    msg_match = re.search(r'<coaching_message>(.*?)</coaching_message>', response, re.DOTALL)
    notes_match = re.search(r'<updated_notes>(.*?)</updated_notes>', response, re.DOTALL)
    profile_match = re.search(r'<updated_profile>(.*?)</updated_profile>', response, re.DOTALL)
    plan_match = re.search(r'<updated_plan>(.*?)</updated_plan>', response, re.DOTALL)
    feedback_match = re.search(r'<feedback_log>(.*?)</feedback_log>', response, re.DOTALL)

    if msg_match:
        message = msg_match.group(1).strip()
        # Strip any XML blocks Claude accidentally embedded inside the coaching message
        message = re.sub(r'<(updated_notes|updated_profile|updated_plan|feedback_log|athlete_summary)>.*?</\1>', '', message, flags=re.DOTALL).strip()
    else:
        # Fallback: strip known coaching tags, then any remaining XML markup
        message = response
        for tag in ("updated_notes", "updated_profile", "updated_plan", "feedback_log", "athlete_summary"):
            message = re.sub(rf'<{tag}>.*?</{tag}>', '', message, flags=re.DOTALL)
        # Strip any remaining XML-style tags
        message = re.sub(r'<[^>]+>', '', message).strip()
    updated_notes = notes_match.group(1).strip() if notes_match else ""
    updated_profile = profile_match.group(1).strip() if profile_match else ""
    updated_plan = plan_match.group(1).strip() if plan_match else ""
    feedback_log = feedback_match.group(1).strip() if feedback_match else ""

    return message, updated_notes, updated_profile, updated_plan, feedback_log


def apply_updates(updated_notes: str, updated_profile: str):
    if updated_notes:
        write_notes(updated_notes)
    if updated_profile:
        write_profile(updated_profile)
    # updated_plan is intentionally never applied — training_plan.md is read-only


def _extract_week_table(block: str) -> str:
    """Extract the week header line + markdown table from a week block."""
    lines = block.strip().splitlines()
    result = []
    in_table = False
    for line in lines:
        if line.startswith("## Week"):
            result.append(line)
        elif line.startswith("|"):
            in_table = True
            result.append(line)
        elif in_table:
            break
    return "\n".join(result)


def _get_plan_session(block: str, day_abbr: str) -> str:
    m = re.search(rf'\|\s*{re.escape(day_abbr)}\s*\|\s*(.+?)\s*\|', block)
    return m.group(1).strip() if m else "Rest"


def parse_plan_context(plan_text: str, weekday: str) -> dict:
    """
    Parse today's and tomorrow's sessions from the training plan.
    weekday: full weekday name e.g. "Monday"
    Returns {"today": str, "tomorrow": str, "week_block": str}
    """
    today = date.today()
    day_abbr = _WEEKDAY_TO_ABBR.get(weekday, weekday[:3])

    week_blocks = re.split(r'\n---\n', plan_text)

    current_block = None
    current_block_idx = None

    for i, block in enumerate(week_blocks):
        m = re.search(r'## Week \d+ — (\w+)\s+(\d+)', block)
        if not m:
            continue
        month_num = _MONTH_MAP.get(m.group(1))
        if not month_num:
            continue
        start_day = int(m.group(2))
        year = today.year
        try:
            week_start = date(year, month_num, start_day)
        except ValueError:
            continue
        if (today - week_start).days > 200:
            try:
                week_start = date(year + 1, month_num, start_day)
            except ValueError:
                continue
        if week_start <= today <= week_start + timedelta(days=6):
            current_block = block
            current_block_idx = i
            break

    if current_block is None:
        for i, block in enumerate(week_blocks):
            if "| Day | Session |" in block:
                current_block = block
                current_block_idx = i
                break

    if current_block is None:
        return {"today": "See plan", "tomorrow": "See plan", "week_block": plan_text[:800]}

    today_session = _get_plan_session(current_block, day_abbr)

    today_idx = _DAY_ABBRS.index(day_abbr) if day_abbr in _DAY_ABBRS else 0
    tomorrow_idx = (today_idx + 1) % 7
    tomorrow_abbr = _DAY_ABBRS[tomorrow_idx]
    if tomorrow_idx == 0 and current_block_idx is not None and current_block_idx + 1 < len(week_blocks):
        tomorrow_block = week_blocks[current_block_idx + 1]
    else:
        tomorrow_block = current_block

    tomorrow_session = _get_plan_session(tomorrow_block, tomorrow_abbr)
    week_block = _extract_week_table(current_block)

    return {"today": today_session, "tomorrow": tomorrow_session, "week_block": week_block}


def set_config_value(key_path: str, value) -> bool:
    """
    Update a config value by dot-notation key path and write back to config.yaml.
    e.g. set_config_value("coaching.goal", "Sub-18 5K")
    Returns True on success.
    """
    try:
        with open("config.yaml") as f:
            config = yaml.safe_load(f)

        keys = key_path.split(".")
        obj = config
        for k in keys[:-1]:
            if k not in obj:
                return False
            obj = obj[k]
        obj[keys[-1]] = value

        with open("config.yaml", "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False

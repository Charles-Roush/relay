import re
from datetime import date, timedelta
from pathlib import Path

import yaml

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
    f.write_text(content if content.endswith("\n") else content + "\n")


def append_to_profile(text: str):
    """Append a raw line to the profile (used for PR logging etc.)."""
    content = read_profile().rstrip()
    _profile_file().write_text(content + "\n" + text + "\n")


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
    _notes_file().write_text(rebuilt)


def write_weekly_reflection(content: str):
    """Append a dated weekly reflection entry."""
    f = _weekly_reflection_file()
    existing = f.read_text() if f.exists() else _WEEKLY_REFLECTION_HEADER
    # Ensure header exists
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

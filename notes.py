from datetime import date, timedelta
from pathlib import Path

import yaml

NOTES_FILE = Path("coach_notes.md")
PLAN_FILE = Path("training_plan.md")

NOTES_HEADER = "## Coach Notes\n\n"
PLAN_HEADER = "## Training Plan\n\n"


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def read_notes() -> str:
    if not NOTES_FILE.exists():
        NOTES_FILE.write_text(NOTES_HEADER)
    return NOTES_FILE.read_text()


def read_plan() -> str:
    if not PLAN_FILE.exists():
        PLAN_FILE.write_text(PLAN_HEADER + "No training plan yet.\n")
    return PLAN_FILE.read_text()


def write_notes(content: str):
    """Write notes, enforcing expiry and max_lines from config."""
    config = load_config()
    notes_cfg = config["claude"]["coach_notes"]
    expiry_days = notes_cfg["expiry_days"]
    max_lines = notes_cfg["max_lines"]

    cutoff = date.today() - timedelta(days=expiry_days)

    # Extract lines that look like note entries vs header lines
    lines = content.splitlines()
    header_lines = []
    note_lines = []
    in_notes = False

    for line in lines:
        if line.strip() == "## Coach Notes":
            in_notes = True
            header_lines.append(line)
        elif in_notes and line.strip():
            note_lines.append(line)
        elif not in_notes:
            header_lines.append(line)

    # Filter expired notes
    filtered = []
    for line in note_lines:
        # Try to extract date from [YYYY-MM-DD] prefix
        if line.startswith("[") and "]" in line:
            date_str = line[1:11]
            try:
                note_date = date.fromisoformat(date_str)
                if note_date >= cutoff:
                    filtered.append(line)
                # else: expired, drop it
            except ValueError:
                filtered.append(line)  # can't parse date, keep it
        else:
            filtered.append(line)

    # Trim to max_lines (keep most recent = last N)
    if len(filtered) > max_lines:
        filtered = filtered[-max_lines:]

    rebuilt = "## Coach Notes\n\n" + "\n".join(filtered) + "\n"
    NOTES_FILE.write_text(rebuilt)


def write_plan(content: str):
    PLAN_FILE.write_text(content if content.endswith("\n") else content + "\n")


def parse_claude_response(response: str) -> tuple[str, str, str]:
    """
    Returns (message, updated_notes, updated_plan).
    Splits on UPDATED_NOTES: and UPDATED_PLAN: markers.
    """
    message = response
    updated_notes = ""
    updated_plan = ""

    if "UPDATED_PLAN:" in response:
        parts = response.split("UPDATED_PLAN:", 1)
        updated_plan = parts[1].strip()
        response = parts[0]

    if "UPDATED_NOTES:" in response:
        parts = response.split("UPDATED_NOTES:", 1)
        updated_notes = parts[1].strip()
        message = parts[0].strip()

    return message, updated_notes, updated_plan


def apply_updates(updated_notes: str, updated_plan: str):
    if updated_notes:
        write_notes(updated_notes)
    if updated_plan:
        write_plan(updated_plan)

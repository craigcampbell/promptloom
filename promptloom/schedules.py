"""Notes that run as prompts on a schedule: "every weekday at 08:00, in this
project, ask Claude Code to pull the Jira tickets".

The daemon calls tick() every cycle. A note is due when its time has passed
today (local time), today is one of its days, and it has not run since. The
run is the same as pressing "Run as prompt": the tool opens in the project on
the note's text, with the brief on top when the project has one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from . import brief, extract, launch
from .userdata import UserData

CATCH_UP_MINUTES = 90  # a run missed by less than this (the machine was asleep) still happens


def due(note: dict, now: datetime) -> bool:
    schedule = note.get("schedule") or {}
    if not schedule.get("time") or not note.get("body", "").strip():
        return False
    hour, minute = (int(x) for x in schedule["time"].split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if str(now.weekday()) not in schedule.get("days", "") or now < slot:
        return False
    if now - slot > timedelta(minutes=CATCH_UP_MINUTES):
        return False
    last = extract.parse_ts(note.get("last_run", ""))
    return last is None or last.astimezone() < slot


def run_note(cfg: dict, state_dir: Path, user: UserData, note: dict, records,
             how: str = "schedule", at: str = "") -> dict:
    """Open the note's tool on its text, in its project. Returns what launch did."""
    context = note.get("context", "")
    if not context:
        context = next((r.session.context for r in records
                        if r.session.project == note.get("project") and r.session.context), "")
    text = note["body"]
    project = note.get("project") or (Path(context).name if context else "")
    if project and any(r.session.project == project for r in records):
        own = user.briefs.get(project, {})
        auto = brief.draft_brief(project, records, own.get("hidden", []))
        text = brief.for_prompt(auto, own.get("text", "")).strip() + "\n\n---\n\n" + text
    done = launch.start(cfg, state_dir, note.get("tool") or "claude", text,
                        note.get("model", ""), context)
    with user.editing() as u:
        u.note_ran(note["id"], how, at)
    return done


def tick(cfg: dict, state_dir: Path, records) -> list[str]:
    """Run every scheduled note that is due. Returns the titles that ran."""
    user = UserData(state_dir)
    user.load()
    now = datetime.now().astimezone()
    stamp = extract.norm_ts(now.isoformat())  # the run is stamped with the scheduler's clock
    ran = []
    for note in list(user.notes):
        if not due(note, now):
            continue
        try:
            run_note(cfg, state_dir, user, note, records, at=stamp)
            ran.append(note["title"])
        except Exception as exc:  # noqa: BLE001 - one bad note must not stop the rest
            with user.editing() as u:
                u.note_ran(note["id"], f"failed: {exc}", stamp)
    return ran


def next_run(note: dict, now: datetime | None = None) -> str:
    """When the note runs next, as local ISO, or "" when it is not scheduled."""
    schedule = note.get("schedule") or {}
    if not schedule.get("time"):
        return ""
    now = now or datetime.now().astimezone()
    hour, minute = (int(x) for x in schedule["time"].split(":"))
    for offset in range(8):
        day = now + timedelta(days=offset)
        slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if str(slot.weekday()) in schedule["days"] and slot > now:
            return slot.strftime("%Y-%m-%dT%H:%M:%S%z")
    return ""


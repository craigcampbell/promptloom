"""Notes that run as prompts on a schedule."""

from datetime import datetime, timedelta, timezone

from promptloom import schedules
from promptloom.userdata import UserData, clean_schedule


def test_clean_schedule_accepts_words_and_lists():
    assert clean_schedule({"time": "8:05", "days": "weekdays"}) == {"time": "08:05", "days": "01234"}
    assert clean_schedule({"time": "18:00", "days": ["mon", "fri"]}) == {"time": "18:00", "days": "04"}
    assert clean_schedule({"time": "18:00", "days": "3"}) == {"time": "18:00", "days": "3"}
    assert clean_schedule({"time": "25:00", "days": "daily"}) == {}
    assert clean_schedule({"time": "", "days": "daily"}) == {} and clean_schedule(None) == {}


def test_due_once_per_slot_with_a_little_catch_up():
    tz = timezone(timedelta(hours=-5))
    monday_0805 = datetime(2026, 10, 19, 8, 5, tzinfo=tz)
    note = {"body": "sweep", "schedule": {"time": "08:00", "days": "01234"}, "last_run": ""}
    assert schedules.due(note, monday_0805)
    assert not schedules.due(note, monday_0805.replace(hour=7, minute=59))  # not yet
    assert not schedules.due(note, monday_0805.replace(hour=11))  # missed by too much
    assert not schedules.due(note, monday_0805 + timedelta(days=5))  # Saturday
    ran = {**note, "last_run": monday_0805.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert not schedules.due(ran, monday_0805 + timedelta(minutes=10))  # already ran this slot
    assert schedules.due(ran, monday_0805 + timedelta(days=1))  # Tuesday again
    assert not schedules.due({**note, "body": " "}, monday_0805)  # nothing to run
    assert schedules.next_run(note, monday_0805).startswith("2026-10-20T08:00")
    assert schedules.next_run({"schedule": {"time": "08:00", "days": "5"}},
                              monday_0805).startswith("2026-10-24T08:00")
    assert schedules.next_run({}) == ""


def test_tick_runs_due_notes_and_counts_them(tmp_path, monkeypatch):
    user = UserData(tmp_path)
    with user.editing() as u:
        note = u.put_note({"title": "Sweep", "body": "sweep tickets", "project": "shop",
                           "tool": "claude", "schedule": {"time": "08:00", "days": "daily"}})
        u.put_note({"title": "Plain", "body": "just a note"})
    started = []
    monkeypatch.setattr(schedules.launch, "start",
                        lambda cfg, sd, tool, prompt, model, ctx: started.append((tool, prompt, ctx))
                        or {"tool": "Claude Code", "dir": ctx, "how": "terminal"})
    fixed = datetime.now().astimezone().replace(hour=8, minute=1, second=0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(schedules, "datetime", Clock)
    assert schedules.tick({}, tmp_path, []) == ["Sweep"]
    assert started == [("claude", "sweep tickets", "")]
    assert schedules.tick({}, tmp_path, []) == []  # not twice in one slot
    again = UserData(tmp_path)
    again.load()
    ran = again.note(note["id"])
    assert ran["runs"] == 1 and ran["last_run_how"] == "schedule"

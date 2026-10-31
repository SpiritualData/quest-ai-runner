"""The world's calendar. The world's "today" is the real clock (the model under test also sees the real date in its own
system prompt, so a pinned date would contradict it), and every date-dependent expectation is computed from that same
day at scoring time. ``E2E_TODAY`` (YYYY-MM-DD) pins it for a reproducible run."""
import datetime
import os
import re

NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]


def world_today():
    pinned = os.environ.get("E2E_TODAY")
    return datetime.date.fromisoformat(pinned) if pinned else datetime.date.today()


def days_until(iso_date, today=None):
    return (datetime.date.fromisoformat(iso_date) - (today or world_today())).days


def fill_calendar(text, today=None):
    """Resolve ``{days_until:YYYY-MM-DD}`` and ``{weeks_until:YYYY-MM-DD}`` (one decimal) in a case's ideal reply."""
    def resolve(match):
        days = days_until(match.group(2), today)
        return str(days) if match.group(1) == "days" else f"{days / 7:.1f}"
    return re.sub(r"\{(days|weeks)_until:(\d{4}-\d{2}-\d{2})\}", resolve, text)


def countdown_problems(reply, iso_date, today=None):
    """A reply to 'how long until <date>' must state the right number of days, or the right number of weeks (whole, to one
    decimal, or a half). A wrong day count with no right figure fails."""
    days = days_until(iso_date, today)
    low = reply.lower()
    if re.search(rf"(?<![\d.]){days}\s*(?:more\s+)?days?\b", low):
        return []
    weeks = days / 7
    forms = {f"{weeks:.1f}", f"{int(weeks)}", f"{round(weeks)}"}
    if int(weeks) != weeks and weeks - int(weeks) >= 0.45 and weeks - int(weeks) <= 0.55:
        forms.add(f"{int(weeks)} and a half")
        forms.add(f"{int(weeks)} 1/2")
    for form in forms:
        if re.search(rf"(?<![\d.]){re.escape(form)}\s*weeks?\b", low):
            return []
        if form.isdigit() and int(form) < len(NUMBER_WORDS) and re.search(rf"\b{NUMBER_WORDS[int(form)]}\s+weeks?\b", low):
            return []
    return [f"reply gives no correct countdown to {iso_date} ({days} days, about {weeks:.1f} weeks)"]

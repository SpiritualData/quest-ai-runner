"""Shared by the arms: read the numbered observation a web search returns and compose an answer by rule. Not a solution."""
import re

STOP = {"the", "a", "an", "of", "to", "in", "at", "and", "or", "is", "are", "how", "what", "fix", "for", "on", "with"}
ENTRY = re.compile(r"^\d+\. (?P<title>.*?) \| (?P<url>https?://\S+)(?: \| \S+)?\s*\n\s+(?P<snippet>.*)$", re.M)


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}


def entries(observation):
    return [m.groupdict() for m in ENTRY.finditer(observation or "")]


def best_entry(question, observation, floor=0.5):
    """The entry whose title and snippet cover at least ``floor`` of the question's content words, else None."""
    terms = words(question)
    scored = [(len(terms & words(e["title"] + " " + e["snippet"])) / max(len(terms), 1), i, e)
              for i, e in enumerate(entries(observation))]
    scored = [s for s in scored if s[0] >= floor]
    return max(scored, key=lambda s: (s[0], -s[1]))[2] if scored else None

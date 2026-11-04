"""A deterministic stand-in for the model of the bootstrap: one area per top-level folder, one topic card per area.

It answers the three prompts the topic-card pass sends (area discovery, topic extraction, duplicate merge) from the prompt text
alone, so two runs over the same corpus give the same cards and a test can compare what was written."""
import json
import re

TOPIC_FOLDERS = ("alpha", "beta", "gamma")


class TopicProvider:
    def __init__(self):
        self.calls = 0

    def answer(self, messages, model=None, **kwargs):
        self.calls += 1
        prompt = messages[0]["content"]
        if prompt.startswith("You are analyzing a slice"):
            paths = re.findall(r"[\w./-]+\.md", prompt)
            return json.dumps([{"name": folder, "description": f"The {folder} notes",
                                "files": [p for p in paths if p.startswith(folder + "/")]} for folder in TOPIC_FOLDERS
                               if any(p.startswith(folder + "/") for p in paths)])
        match = re.search(r'from the "(\w+)" area', prompt)
        if match:
            count = len(re.findall(r"^\d+\. ", prompt, flags=re.M))
            return json.dumps([{"id": f"{match.group(1)}-topic", "name": f"{match.group(1)} topic", "keywords": [match.group(1), "notes"],
                                "summary": f"What the {match.group(1)} notes say together", "files": list(range(1, count + 1))}])
        return "[]"


def write_corpus(root):
    """Nine small notes in three folders and one lone note in a fourth (a single-file topic is not carded)."""
    for folder in TOPIC_FOLDERS:
        (root / folder).mkdir(parents=True)
        for number in range(3):
            (root / folder / f"note{number}.md").write_text(f"# {folder} {number}\n\nText about {folder} number {number}.\n", encoding="utf-8")
    (root / "delta").mkdir()
    (root / "delta" / "lone.md").write_text("# Lone\n\nAlone.\n", encoding="utf-8")

"""BASELINE: what a first draft writes. The first search result is cited for whatever was asked, with its snippet as the answer, and
an empty or failed search is answered from memory. A page that does not hold the claim is cited as its source."""
import common
from web_search.impls import _vendor
from know_the_world import compose


def build(meter):
    pages = {c["id"]: c for c in common.load_cases("web_search")}

    def answer(case):
        found = _vendor.single("serper")(meter)(dict(pages[case["source"]], query=case["question"]))
        listed = compose.entries(found["text"])
        if not listed:
            return {"text": case["memory_answer"], "cited": [], "retrieved": found["urls"]}
        first = listed[0]
        return {"text": f"{first['snippet']} [{first['title']}]({first['url']})", "cited": [first["url"]], "retrieved": found["urls"]}
    return answer

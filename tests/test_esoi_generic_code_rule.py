"""Rule Q3 (scripts/esoi_rules.py): a module with no Quest-domain word and no hubofhubs binding is generic code."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("qar_esoi_rules_q3", ROOT / "scripts" / "esoi_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


def flagged(**sources):
    return rules.generic_modules(sources)


def test_a_module_with_domain_vocabulary_is_not_flagged():
    assert flagged(a="def list_goals(client):\n    return client.get('/goals')\n") == []


def test_camel_case_identifiers_count_as_domain_words():
    assert flagged(a="class QuestClient:\n    pass\n") == []


def test_a_phrase_word_matches_across_punctuation():
    assert flagged(a="def decision_request_url():\n    return 1\n") == []


def test_a_generic_module_is_flagged():
    assert flagged(a="def chunk(items, size):\n    return [items[i:i + size] for i in range(0, len(items), size)]\n") == ["a"]


def test_a_thin_hub_binding_is_not_flagged():
    assert flagged(a="import hubofhubs\n\nhubofhubs.register('x', 'y', object)\n") == []


def test_an_empty_init_is_not_code():
    assert flagged(a="", b="\n") == []


def test_a_word_inside_a_longer_word_does_not_count():
    assert flagged(a="def basket():\n    return 'tasks'\n") == ["a"]

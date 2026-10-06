"""Tests for web_page_extract -- SSRF guard, HTML extraction, passage selection. Offline."""
from __future__ import annotations

from quest_ai_runner.adapters.web_page_extract import (
    check_url_is_safe,
    estimate_tokens,
    extract_main_text,
    select_passages,
    split_passages,
)


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def test_refuses_file_scheme():
    err = check_url_is_safe("file:///etc/passwd")
    assert err is not None
    assert "scheme" in err


def test_refuses_loopback_literal_ip():
    err = check_url_is_safe("http://127.0.0.1/admin")
    assert err is not None


def test_refuses_private_10_x_literal_ip():
    err = check_url_is_safe("http://10.0.0.5/secret")
    assert err is not None


def test_refuses_link_local_169_254():
    err = check_url_is_safe("http://169.254.169.254/latest/meta-data/")
    assert err is not None


def test_refuses_hostname_resolving_to_private_address():
    def fake_resolver(host):
        assert host == "internal.example.com"
        return ["10.1.2.3"]

    err = check_url_is_safe("https://internal.example.com/", resolver=fake_resolver)
    assert err is not None
    assert "private" in err or "loopback" in err or "link-local" in err


def test_allows_public_hostname():
    def fake_resolver(host):
        return ["93.184.216.34"]  # a plain public-looking address

    err = check_url_is_safe("https://example.com/article", resolver=fake_resolver)
    assert err is None


def test_refuses_unresolvable_host():
    def fake_resolver(host):
        return []

    err = check_url_is_safe("https://nowhere.invalid/", resolver=fake_resolver)
    assert err is not None


# ---------------------------------------------------------------------------
# HTML main-text extraction
# ---------------------------------------------------------------------------


def test_extract_drops_script_and_nav_prefers_article():
    html = """
    <html><head><title>My Article Title</title>
    <script>var evil = "tracker code here";</script>
    </head>
    <body>
    <nav><a href="/">Home</a><a href="/about">About</a></nav>
    <header>Site Header Text That Should Vanish</header>
    <article>
      <p>This is the real article body with the actual content a reader wants, spanning
      enough words to be clearly substantial and not accidentally discarded by the
      article-length threshold the extractor applies before it trusts article content.</p>
      <p>A second paragraph continues the real content, again padded with enough words so
      the extractor is confident this block is the genuine article body and not noise.</p>
    </article>
    <footer>Copyright footer boilerplate that should also vanish from the output.</footer>
    </body></html>
    """
    title, text = extract_main_text(html)
    assert title == "My Article Title"
    assert "tracker code" not in text
    assert "Home" not in text
    assert "Site Header Text" not in text
    assert "Copyright footer" not in text
    assert "real article body" in text
    assert "second paragraph continues" in text


def test_extract_drops_noise_classed_divs():
    html = """
    <html><head><title>T</title></head><body>
    <div class="cookie-banner">Accept our cookies please, this is annoying boilerplate.</div>
    <div class="main-content">
      <p>Real paragraph content that matters to the reader and should survive extraction
      with all of its words intact, since it is not inside any noise-classed container.</p>
    </div>
    </body></html>
    """
    _, text = extract_main_text(html)
    assert "Accept our cookies" not in text
    assert "Real paragraph content" in text


def test_extract_falls_back_to_general_text_when_no_substantial_article():
    html = "<html><head><title>Short</title></head><body><p>Just a little bit of text.</p></body></html>"
    title, text = extract_main_text(html)
    assert title == "Short"
    assert "Just a little bit of text" in text


# ---------------------------------------------------------------------------
# Passage splitting
# ---------------------------------------------------------------------------


def test_split_passages_merges_tiny_paragraphs():
    text = "Short one.\n\nAnother short bit.\n\n" + " ".join(["word"] * 30) + "."
    passages = split_passages(text)
    # Tiny paragraphs get merged forward rather than staying as standalone noise passages.
    assert all(len(p.split()) >= 5 for p in passages)


def test_split_passages_splits_huge_paragraph():
    text = " ".join(["word"] * 400)
    passages = split_passages(text)
    assert len(passages) >= 3
    for p in passages:
        assert len(p.split()) <= 120


def test_split_passages_empty_text():
    assert split_passages("") == []
    assert split_passages("   \n\n  ") == []


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_roughly_chars_over_4():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 800) == 200


# ---------------------------------------------------------------------------
# select_passages
# ---------------------------------------------------------------------------


def test_select_passages_empty_focus_keeps_leading_passages_in_order():
    passages = [
        "alpha passage about dogs and cats and other animals in general terms here now.",
        "beta passage about completely unrelated topics like finance and the stock market.",
        "gamma passage about gardening tips for growing tomatoes in a backyard plot here.",
    ]
    chosen = select_passages(passages, "", token_budget=10_000)
    assert chosen == passages  # everything fits; order preserved


def test_select_passages_picks_focus_relevant_paragraph():
    passages = [
        "This paragraph discusses quarterly financial earnings and revenue growth figures.",
        "This paragraph is entirely about the mating habits of arctic penguins in winter.",
        "A third paragraph returns to unrelated topics about city zoning regulations today.",
    ]
    chosen = select_passages(passages, "penguin mating habits winter", token_budget=10_000)
    assert len(chosen) >= 1
    assert "penguins" in chosen[0]


def test_select_passages_respects_token_budget():
    # Each passage costs ~ (len // 4) tokens; build passages big enough that not all fit.
    passages = [("focusword " + "filler " * 60).strip() for _ in range(5)]
    budget = estimate_tokens(passages[0]) + 1  # room for ~1 passage only
    chosen = select_passages(passages, "focusword", token_budget=budget)
    assert len(chosen) >= 1
    assert len(chosen) < len(passages)


def test_select_passages_keeps_document_order_even_when_later_passage_scores_higher():
    passages = [
        "first passage mentions apples only once in passing within this sentence.",
        "second passage is loaded with apples apples apples apples apples apples apples.",
    ]
    chosen = select_passages(passages, "apples", token_budget=10_000)
    assert chosen == passages  # both fit; original order preserved regardless of score


def test_select_passages_always_returns_at_least_one_even_over_budget():
    passages = ["a single very long passage " + "word " * 500]
    chosen = select_passages(passages, "word", token_budget=1)
    assert len(chosen) == 1

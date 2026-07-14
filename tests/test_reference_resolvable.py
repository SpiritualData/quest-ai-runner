"""The RetrievalAdapter reference-resolution CAPABILITY is structural and checkable.

Joshua's root complaint was that "this adapter's content can be persisted + resolved as a learned
card reference" was tribal knowledge -- you had to read code to find out. The fix is NOT a second
Protocol; it is an OPTIONAL CAPABILITY on the ONE interface every retrieval adapter already
implements (``core.adapters.RetrievalAdapter``), mirroring how ``query`` is already optional there:

  * an adapter that supports it advertises a NON-None ``reference_type`` and implements
    ``make_locator`` / ``resolve_reference``;
  * an adapter that does not simply inherits the ``RetrievalAdapterBase`` defaults
    (``reference_type = None``), so ``adapter.reference_type is not None`` is the whole check.

These tests pin that: two adapters that SHOULD support it do, one that clearly should NOT stays
``None`` via the base-class default (untouched), and the generic ``collect_reference_resolvers`` walk
discovers exactly the resolvable ones (including through a composite). All offline.
"""
from quest_ai_runner.adapters.claude_conversations_adapter import ClaudeConversationsAdapter
from quest_ai_runner.adapters.composite_retrieval_adapter import CompositeRetrievalAdapter
from quest_ai_runner.adapters.files_adapter import FilesAdapter
from quest_ai_runner.adapters.google_chat_adapter import GoogleChatAdapter
from quest_ai_runner.adapters.provider_web_search_adapter import ProviderWebSearchAdapter
from quest_ai_runner.adapters.reference_resolver import collect_reference_resolvers
from quest_ai_runner.adapters.web_search_adapter import WebSearchAdapter


def test_resolvable_adapters_advertise_a_reference_type():
    # The two adapters whose content is a durable, re-fetchable source: they support it.
    assert ClaudeConversationsAdapter(sessions_dir="/does/not/exist").reference_type == "conversation"
    assert GoogleChatAdapter().reference_type == "chat_thread"


def test_non_resolvable_adapters_are_none_via_base_default(tmp_path):
    # Pure retrieval adapters with no persistent, re-fetchable identity: they inherit the
    # RetrievalAdapterBase default (reference_type = None) WITHOUT any adapter-specific code.
    # `adapter.reference_type is not None` is therefore the structural "does it support it?" check
    # -- no isinstance-against-a-second-protocol, no reading call chains.
    assert WebSearchAdapter(api_key=None).reference_type is None
    assert ProviderWebSearchAdapter(None, model="m").reference_type is None
    assert FilesAdapter(str(tmp_path)).reference_type is None

    # The default methods are inert (never raise, resolve to nothing) so a caller can treat any
    # RetrievalAdapter uniformly.
    web = WebSearchAdapter(api_key=None)
    assert web.make_locator("x") == {}
    assert web.resolve_reference({"anything": 1}) is None


def test_reference_type_and_locator_shapes_are_distinct_per_adapter():
    claude = ClaudeConversationsAdapter(sessions_dir="/does/not/exist")
    gchat = GoogleChatAdapter()
    # DISTINCT types (chat_thread must NOT overload conversation) and DISTINCT locator shapes.
    assert claude.reference_type != gchat.reference_type
    assert claude.make_locator("abc") == {"conv_id": "abc"}
    loc = gchat.make_locator("spaces/AAA/threads/T")
    assert loc.get("thread_or_message_id") == "spaces/AAA/threads/T"
    assert "space" in loc  # carries the addressable space too
    # Empty candidates yield an empty locator (never a bogus reference).
    assert claude.make_locator("") == {}
    assert gchat.make_locator("") == {}


def test_collect_reference_resolvers_discovers_only_resolvable_adapters(tmp_path):
    claude = ClaudeConversationsAdapter(sessions_dir="/does/not/exist")
    gchat = GoogleChatAdapter()
    files = FilesAdapter(str(tmp_path))
    web = WebSearchAdapter(api_key=None)

    # A composite of mixed adapters: only the two resolvable ones are picked up, each mapped to its
    # OWN resolve_reference (a bare callable, ready to coerce into a ReferenceResolver).
    comp = CompositeRetrievalAdapter([files, gchat, web, claude])
    found = collect_reference_resolvers(comp)
    assert set(found) == {"chat_thread", "conversation"}
    assert found["conversation"] == claude.resolve_reference
    assert found["chat_thread"] == gchat.resolve_reference

    # A single (non-composite) resolvable adapter is discovered directly.
    assert set(collect_reference_resolvers(gchat)) == {"chat_thread"}
    # A single non-resolvable adapter yields nothing; None is safe.
    assert collect_reference_resolvers(files) == {}
    assert collect_reference_resolvers(None) == {}

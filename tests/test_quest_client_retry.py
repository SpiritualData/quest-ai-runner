"""The client is a good HTTP client: bounded retry with backoff and jitter, Retry-After honoured, 4xx never retried,
writes retried only under an Idempotency-Key and only when the backend is known to honour it, a configurable
timeout, and a one-time capability probe that degrades quietly. A scripted transport stands in for the network
(no sockets, no real sleeping)."""
import logging
import urllib.error

import pytest

from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient
from quest_ai_runner.runner.quest_client_support import QuestWriteError, parse_retry_after

from .fake_quest_transport import make_client

OK = (200, {"ok": True})
GOAL = (201, {"id": "g1"})
ADVERTISES = (200, {"success": True, "data": {"status": "healthy", "capabilities": ["idempotency-key"]}})
SILENT = (200, {"success": True, "data": {"status": "healthy", "service": "quest-backend"}})


def test_a_get_is_retried_on_5xx_with_exponential_backoff(monkeypatch):
    client, transport = make_client([(503, {"detail": "busy"}), (502, {"detail": "busy"}), OK], monkeypatch)
    assert client._request("GET", "/api/x") == {"ok": True}
    assert len(transport.requests) == 3
    assert client.sleeps == [0.5, 1.0]


def test_backoff_is_jittered_within_its_ceiling(monkeypatch):
    client, _ = make_client([(503, {}), (503, {}), OK], monkeypatch)
    client.jitter = lambda low, high: low
    client._request("GET", "/api/x")
    assert client.sleeps == [0.25, 0.5]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_4xx_is_never_retried(monkeypatch, status):
    client, transport = make_client([(status, {"detail": "no"})], monkeypatch)
    with pytest.raises(QuestApiError) as caught:
        client._request("GET", "/api/x")
    assert caught.value.status == status and len(transport.requests) == 1 and client.sleeps == []


def test_a_get_is_retried_on_a_timeout_then_gives_up(monkeypatch):
    client, transport = make_client([TimeoutError("read timed out")], monkeypatch)
    with pytest.raises(QuestApiError, match="transport error"):
        client._request("GET", "/api/x")
    assert len(transport.requests) == 3 and len(client.sleeps) == 2


def test_the_retry_count_is_a_setting(monkeypatch):
    monkeypatch.setenv("QAR_QUEST_READ_RETRIES", "0")
    client, transport = make_client([(503, {})], monkeypatch)
    with pytest.raises(QuestApiError):
        client._request("GET", "/api/x")
    assert len(transport.requests) == 1
    explicit, transport = make_client([(503, {})], monkeypatch, read_retries=1)
    with pytest.raises(QuestApiError):
        explicit._request("GET", "/api/x")
    assert len(transport.requests) == 2


def test_retry_after_is_honoured_on_a_429(monkeypatch):
    client, transport = make_client([(429, {"detail": "slow down"}, {"Retry-After": "2"}), OK], monkeypatch)
    assert client._request("GET", "/api/x") == {"ok": True}
    assert client.sleeps == [2.0]


def test_a_retry_after_longer_than_the_cap_is_reported_not_waited_out(monkeypatch):
    client, transport = make_client([(429, {"detail": "hourly cap"}, {"Retry-After": "3600"})], monkeypatch)
    with pytest.raises(QuestApiError) as caught:
        client._request("GET", "/api/x")
    assert caught.value.status == 429 and caught.value.retry_after == 3600.0
    assert client.sleeps == [] and len(transport.requests) == 1


def test_parse_retry_after():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_the_long_poll_channel_is_not_retried(monkeypatch):
    client, transport = make_client([(503, {})], monkeypatch)
    with pytest.raises(QuestApiError):
        client._request("GET", "/api/assistant-tasks/wait", timeout_override=40)
    assert len(transport.requests) == 1


def test_the_timeout_is_a_setting_and_a_longer_override_still_wins(monkeypatch):
    client, transport = make_client([OK], monkeypatch)
    client._request("GET", "/api/x")
    assert transport.requests[-1][4] == 30.0
    monkeypatch.setenv("QAR_QUEST_TIMEOUT_SECONDS", "7")
    from_env, transport = make_client([OK], monkeypatch)
    from_env._request("GET", "/api/x")
    assert transport.requests[-1][4] == 7.0
    explicit, transport = make_client([OK], monkeypatch, timeout=3)
    explicit._request("GET", "/api/x")
    explicit._request("GET", "/api/x", timeout_override=40)
    assert [r[4] for r in transport.requests] == [3.0, 40.0]


def test_a_bad_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("QAR_QUEST_TIMEOUT_SECONDS", "banana")
    monkeypatch.setenv("QAR_QUEST_READ_RETRIES", "-4")
    client, _ = make_client([OK], monkeypatch)
    assert client.timeout == 30.0 and client.read_retries == 2


# --- writes: the Idempotency-Key header, and retry only behind it ----------------------------------

def write_calls(client):
    return [
        lambda key=None: client.create_goal("Run", period="2026-08-18", quest_id="q1", idempotency_key=key),
        lambda key=None: client.add_quest_note("q1", "text", idempotency_key=key),
        lambda key=None: client.create_decision("Approve?", quest_id="q1", assignee_user_id="u1",
                                                idempotency_key=key),
    ]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_goal_note_and_decision_creation_send_an_idempotency_key(monkeypatch, index):
    client, transport = make_client([(201, {"id": "x"})], monkeypatch)
    write_calls(client)[index]()
    key = transport.requests[-1][2].get("idempotency-key")
    assert key and len(key) >= 16
    write_calls(client)[index]()
    assert transport.requests[-1][2]["idempotency-key"] != key


def test_a_key_given_by_the_caller_is_sent_unchanged(monkeypatch):
    client, transport = make_client([GOAL], monkeypatch)
    client.create_goal("Run", period="2026-08-18", idempotency_key="call-123")
    assert transport.requests[-1][2]["idempotency-key"] == "call-123"


def test_a_read_sends_no_key_and_a_keyless_write_is_never_retried(monkeypatch):
    client, transport = make_client([OK], monkeypatch)
    client._request("GET", "/api/x")
    assert "idempotency-key" not in transport.requests[-1][2]
    failing, transport = make_client([(503, {})], monkeypatch, write_retry="on")
    with pytest.raises(QuestApiError):
        failing._request("POST", "/api/x", body={})
    assert len(transport.requests) == 1


def test_by_default_a_write_is_not_retried_because_the_backend_does_not_advertise_support(monkeypatch):
    client, transport = make_client([SILENT, (503, {"detail": "busy"})], monkeypatch)
    with pytest.raises(QuestWriteError) as caught:
        client.create_goal("Run", period="2026-08-18", quest_id="q1")
    posts = [r for r in transport.requests if r[0] == "POST"]
    assert len(posts) == 1 and client.sleeps == []
    assert caught.value.write_status.value == "failed"


def test_write_retry_on_retries_under_one_key(monkeypatch):
    client, transport = make_client([(503, {}), (503, {}), GOAL], monkeypatch, write_retry="on")
    result = client.create_goal("Run", period="2026-08-18", quest_id="q1")
    assert result.status.value == "applied" and result.id == "g1"
    keys = {r[2]["idempotency-key"] for r in transport.requests}
    assert len(transport.requests) == 3 and len(keys) == 1
    assert all(r[1] != "/api/health" for r in transport.requests)  # an explicit setting needs no probe


def test_write_retry_never_retries_a_4xx_or_a_held_answer(monkeypatch):
    client, transport = make_client([(422, {"detail": "bad period"})], monkeypatch, write_retry="on")
    with pytest.raises(QuestWriteError) as caught:
        client.create_goal("Run", period="2026-08-18", quest_id="q1")
    assert caught.value.write_status.value == "refused" and len(transport.requests) == 1
    held, transport = make_client([(202, {"decision_id": "d1"})], monkeypatch, write_retry="on")
    assert held.create_goal("Run", period="2026-08-18", quest_id="q1").status.value == "held"
    assert len(transport.requests) == 1


IN_PROGRESS = (409, {"code": "idempotency_in_progress", "detail": "A request with this Idempotency-Key is still running"},
               {"Retry-After": "2"})


def test_an_in_progress_409_is_retried_with_the_same_key_honouring_retry_after(monkeypatch):
    client, transport = make_client([IN_PROGRESS, IN_PROGRESS, GOAL], monkeypatch, write_retry="on")
    result = client.create_goal("Run", period="2026-08-18", quest_id="q1")
    assert result.status.value == "applied" and len(transport.requests) == 3
    assert len({r[2]["idempotency-key"] for r in transport.requests}) == 1
    assert client.sleeps == [2.0, 2.0]


def test_an_in_progress_409_that_never_clears_is_a_retryable_failure_not_a_refusal(monkeypatch):
    client, transport = make_client([IN_PROGRESS], monkeypatch, write_retry="on")
    with pytest.raises(QuestWriteError) as caught:
        client.create_goal("Run", period="2026-08-18", quest_id="q1")
    assert caught.value.write_status.value == "failed" and len(transport.requests) == 3


def test_an_in_progress_409_with_the_code_alone_is_still_transient(monkeypatch):
    client, transport = make_client([(409, {"code": "idempotency_in_progress", "detail": "busy"}), GOAL], monkeypatch,
                                    write_retry="on")
    assert client.create_goal("Run", period="2026-08-18", quest_id="q1").status.value == "applied"
    assert len(transport.requests) == 2


def test_an_already_answered_409_stays_refused_and_is_never_retried(monkeypatch):
    client, transport = make_client([(409, {"detail": "The person already declined this"})], monkeypatch, write_retry="on")
    with pytest.raises(QuestWriteError) as caught:
        client.create_decision("Approve?", quest_id="q1")
    assert caught.value.write_status.value == "refused" and len(transport.requests) == 1


def test_a_reused_key_with_a_different_body_is_a_422_refusal_and_never_retried(monkeypatch):
    client, transport = make_client([(422, {"detail": "This Idempotency-Key was already used with a different request"})],
                                    monkeypatch, write_retry="on")
    with pytest.raises(QuestWriteError) as caught:
        client.create_goal("Run", period="2026-08-18", quest_id="q1")
    assert caught.value.write_status.value == "refused" and "different request" in str(caught.value)
    assert len(transport.requests) == 1


def test_write_retry_off_wins_even_when_the_backend_advertises_support(monkeypatch):
    client, transport = make_client([(503, {})], monkeypatch, write_retry="off")
    client.capabilities = frozenset({"idempotency-key"})  # even a backend that says it honours the key
    with pytest.raises(QuestWriteError):
        client.add_quest_note("q1", "text")
    assert len(transport.requests) == 1


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("QAR_QUEST_WRITE_RETRY", "on")
    client, transport = make_client([(503, {}), (201, [{"id": "n1"}])], monkeypatch)
    assert client.add_quest_note("q1", "text").status.value == "applied"
    assert len(transport.requests) == 2


def test_auto_retries_writes_once_the_backend_advertises_idempotency_support(monkeypatch):
    client, transport = make_client([ADVERTISES, (503, {}), (201, {"id": "g1"})], monkeypatch)
    assert client.create_goal("Run", period="2026-08-18", quest_id="q1").status.value == "applied"
    assert transport.paths() == [("GET", "/api/health"), ("POST", "/api/planning/goals"),
                                 ("POST", "/api/planning/goals")]


# --- the capability probe -------------------------------------------------------------------------

def test_the_probe_reads_the_health_route_once_and_caches_it(monkeypatch):
    client, transport = make_client([ADVERTISES], monkeypatch)
    assert client.backend_capabilities() == frozenset({"idempotency-key"})
    assert client.backend_capabilities() == frozenset({"idempotency-key"})
    assert transport.paths() == [("GET", "/api/health")]


def test_a_backend_that_advertises_nothing_degrades_with_one_log_line(monkeypatch, caplog):
    client, _ = make_client([SILENT], monkeypatch)
    with caplog.at_level(logging.INFO, logger="quest-ai-runner.quest_client"):
        assert client.backend_capabilities() == frozenset()
        client.backend_capabilities()
        assert client.write_retry_enabled() is False
    notes = [r for r in caplog.records if "write retry" in r.getMessage()]
    assert len(notes) == 1


@pytest.mark.parametrize("step", [(404, {"detail": "nope"}), TimeoutError("slow"),
                                  urllib.error.URLError(ConnectionRefusedError())])
def test_an_unreadable_probe_is_not_an_error_and_disables_write_retry(monkeypatch, step):
    client, _ = make_client([step], monkeypatch)
    assert client.backend_capabilities() == frozenset()
    assert client.write_retry_enabled() is False


def test_the_probe_is_never_retried_so_it_cannot_stall_a_write(monkeypatch):
    client, transport = make_client([(503, {})], monkeypatch)
    client.backend_capabilities()
    assert len(transport.requests) == 1 and client.sleeps == []


def test_capabilities_are_read_from_either_envelope(monkeypatch):
    client, _ = make_client([(200, {"capabilities": ["idempotency-key", "x"]})], monkeypatch)
    assert client.backend_capabilities() == frozenset({"idempotency-key", "x"})

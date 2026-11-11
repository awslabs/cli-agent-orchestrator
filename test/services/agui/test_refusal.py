"""The refusal record's name and limiter (#1158, design points B and F)."""

from __future__ import annotations

import hashlib
import re

import pytest

from cli_agent_orchestrator.services.agui.refusal import (
    REFUSAL_EVENT_TYPE,
    RefusalLimiter,
    get_refusal_limiter,
    refusal_detail,
    refusal_name,
    reset_refusal_limiter,
    ring_share_reached,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_RAW,
    GENERATIVE_UI_COMPONENTS,
    to_agui_event,
)

_VALID = re.compile(r"^[A-Za-z0-9_.-]{0,55}\.[0-9a-f]{8}$")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "html_blob",
        "métric😀",
        "x" * 10_000,
        "a\x00b\nc\r\td",
        "\ud800lone",  # a lone surrogate, which JSON allows
        "<script>alert(1)</script>",
        "../../etc/passwd",
    ],
)
def test_a_name_stays_bounded_and_valid(raw: str) -> None:
    name = refusal_name(raw)
    assert len(name.encode("utf-8")) <= 64
    assert _VALID.match(name), name
    want = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    assert name.endswith("." + want)


def test_bad_characters_are_replaced_not_deleted() -> None:
    assert refusal_name("met ric").startswith("met_ric.")
    assert refusal_name("a\x00b").startswith("a_b.")


@pytest.mark.parametrize(
    "raw", ["metric ", "metric!", " metric", "metric\x00", "metric\n", "approval_card?"]
)
def test_a_name_that_cleans_to_an_allowed_name_is_still_a_refusal(raw: str) -> None:
    """Design point B: deleting bad characters, or dropping the hash, would turn
    these into an allowed name, which the stream renders as a real card."""
    name = refusal_name(raw)
    assert name not in GENERATIVE_UI_COMPONENTS
    record = {"id": "r1", "kind": "other", "detail": refusal_detail(name, 0)}
    agui_type, data = to_agui_event(record)
    assert agui_type == AGUI_RAW
    assert data["rejected_component"] == name


def test_even_an_allowed_name_itself_comes_out_as_a_refusal() -> None:
    """The hash is ALWAYS appended, so no input yields an allowed name."""
    for allowed in GENERATIVE_UI_COMPONENTS:
        assert refusal_name(allowed) not in GENERATIVE_UI_COMPONENTS


def test_distinct_originals_that_clean_alike_keep_distinct_names() -> None:
    assert refusal_name("a b") != refusal_name("a!b")


def test_the_detail_has_the_fixed_schema_and_no_props() -> None:
    assert refusal_detail("x.0", 0) == {
        "event_type": REFUSAL_EVENT_TYPE,
        "ui": {"component": "x.0"},
    }
    assert refusal_detail("x.0", 3)["ui"] == {"component": "x.0", "more_refused": 3}


def test_more_refused_reaches_the_raw_frame_as_an_integer_only() -> None:
    def raw_for(ui):
        return to_agui_event({"id": "r", "kind": "other", "detail": {"ui": ui}})[1]

    assert raw_for({"component": "x.0", "more_refused": 4})["more_refused"] == 4
    for bad in (True, "4", 4.0, -1, 0, None, {"n": 1}):
        assert "more_refused" not in raw_for({"component": "x.0", "more_refused": bad})


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_same_call_twice_in_a_window_publishes_once() -> None:
    lim = RefusalLimiter(clock=_Clock())
    assert lim.decide("t1", "a.0") == 0
    assert lim.decide("t1", "a.0") is None
    assert lim.decide("t1", "b.0") == 1  # the folded repeat rides on the next record


def test_terminals_are_limited_separately() -> None:
    lim = RefusalLimiter(clock=_Clock())
    assert lim.decide("t1", "a.0") == 0
    assert lim.decide("t2", "a.0") == 0
    assert lim.decide(None, "a.0") == 0  # the shared unattributed bucket


def test_repeats_fold_into_one_n_more_refused_record() -> None:
    clock = _Clock()
    lim = RefusalLimiter(clock=clock, per_bucket=2)
    assert lim.decide("t1", "a.0") == 0
    assert lim.decide("t1", "b.0") == 0
    assert [lim.decide("t1", f"n{i}.0") for i in range(7)] == [None] * 7  # over the limit
    assert lim.decide("t1", "a.0") is None  # and a repeat
    clock.now += 10.0  # the next window
    assert lim.decide("t1", "c.0") == 8
    assert lim.decide("t1", "d.0") == 0


def test_the_server_wide_cap_folds_across_terminals() -> None:
    lim = RefusalLimiter(clock=_Clock(), global_per_window=3)
    assert [lim.decide(f"t{i}", "a.0") for i in range(5)] == [0, 0, 0, None, None]


def test_a_fuzz_of_10k_names_and_terminals_keeps_memory_bounded() -> None:
    """Design point F: a caller that varies the name or terminal id can't grow it."""
    lim = RefusalLimiter(clock=_Clock(), max_buckets=256, per_bucket=5)
    for i in range(10_000):
        lim.decide("t1", f"name-{i}.0")
        lim.decide(f"terminal-{i}", f"name-{i}.0")
    assert lim.tracked_buckets <= 256
    assert lim.tracked_names <= 256 * 5


def test_the_singleton_resets() -> None:
    reset_refusal_limiter()
    first = get_refusal_limiter()
    assert get_refusal_limiter() is first
    reset_refusal_limiter()
    assert get_refusal_limiter() is not first
    reset_refusal_limiter()


def test_a_second_creator_waiting_on_the_lock_reuses_the_first_limiter(monkeypatch) -> None:
    """The double-checked lock: a caller that saw no limiter, then waited on the
    lock while another created one, must reuse it, not replace it."""
    from cli_agent_orchestrator.services.agui import refusal

    reset_refusal_limiter()
    created = RefusalLimiter()

    class _CreatesWhileHeld:
        def __enter__(self):
            refusal._limiter = created  # another caller won while this one waited
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(refusal, "_limiter_lock", _CreatesWhileHeld())
    assert get_refusal_limiter() is created
    reset_refusal_limiter()


def test_the_ring_share_is_a_tenth_of_the_ring() -> None:
    assert ring_share_reached(49, 500) is False
    assert ring_share_reached(50, 500) is True
    assert ring_share_reached(0, 500) is False


def test_the_event_log_counts_refusal_rows_in_its_ring() -> None:
    from cli_agent_orchestrator.services.event_log_service import EventLog

    log = EventLog()
    for i in range(3):
        log.append("other", None, None, refusal_detail(f"x{i}.0", 0))
    log.append("other", None, None, {"event_type": "agent_ui"})
    log.append("other", None, None, {})
    log._buf.append({"id": "odd", "detail": None})  # a row with no detail mapping
    assert log.count_event_type(REFUSAL_EVENT_TYPE) == 3


def test_a_folded_refusal_rides_on_the_buckets_next_record() -> None:
    lim = RefusalLimiter(clock=_Clock())
    lim.fold("t1")
    lim.fold("t1")
    assert lim.decide("t1", "a.0") == 2


def test_folding_keeps_the_buckets_bounded() -> None:
    lim = RefusalLimiter(clock=_Clock(), max_buckets=4)
    for i in range(100):
        lim.fold(f"t{i}")
    assert lim.tracked_buckets == 4


def test_the_visible_prefix_is_short_and_the_hash_keeps_names_apart() -> None:
    """#1295 review (P3): the cleaned prefix is caller text and is kept short; the hash keeps
    names that share a long prefix apart."""
    from cli_agent_orchestrator.services.agui.refusal import (
        REFUSAL_NAME_PREFIX_CHARS,
        refusal_name,
    )

    long_a = "a" * 300
    long_b = "a" * 299 + "b"
    name_a, name_b = refusal_name(long_a), refusal_name(long_b)
    assert name_a[:REFUSAL_NAME_PREFIX_CHARS] == "a" * REFUSAL_NAME_PREFIX_CHARS
    assert name_a != name_b  # same visible prefix, different hash
    assert len(name_a) == REFUSAL_NAME_PREFIX_CHARS + 1 + 8
    secret = "AKIAIOSFODNN7EXAMPLE" + "x" * 60
    assert len(refusal_name(secret)) <= 64

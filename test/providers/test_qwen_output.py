"""Native output survives TUI truncation and never republishes another turn."""

import json

import pytest

from cli_agent_orchestrator.providers.base import OutputExtractionError, OutputExtractionRejected
from cli_agent_orchestrator.providers.qwen_output import QwenOutput


def append(path, kind, blocks, **extra):
    with path.open("a") as stream:
        stream.write(
            json.dumps({"type": kind, "message": {"id": "a", "content": blocks}, **extra}) + "\n"
        )


def test_native_output_binds_to_each_dispatch_and_excludes_reasoning(tmp_path):
    path = tmp_path / "events.jsonl"
    path.touch()
    output = QwenOutput(path)
    output.begin_dispatch()
    assert not output.has_current_user()
    append(path, "user", [{"type": "text", "text": "first"}])
    append(path, "assistant", [{"type": "thinking", "thinking": "private"}])
    with pytest.raises(OutputExtractionRejected):
        output.response()
    append(path, "assistant", [{"type": "text", "text": "first answer"}])
    assert output.response() == "first answer"
    output.begin_dispatch()
    assert not output.has_current_user()
    with pytest.raises(OutputExtractionError, match="acknowledged"):
        output.response()
    append(path, "user", [{"type": "text", "text": "second"}])
    append(path, "assistant", [{"type": "text", "text": "second answer"}])
    assert output.response() == "second answer"


def test_tool_outputs_and_nested_agents_cannot_become_the_final_answer(tmp_path):
    path = tmp_path / "events.jsonl"
    path.touch()
    output = QwenOutput(path)
    append(path, "user", [{"type": "text", "text": "inspect"}])
    append(path, "assistant", [{"type": "text", "text": "will inspect"}])
    append(path, "assistant", [{"type": "tool_use", "name": "shell"}])
    append(path, "user", [{"type": "tool_result", "content": "tool output"}])
    append(path, "assistant", [{"type": "text", "text": "nested"}], parent_tool_use_id="child")
    with pytest.raises(OutputExtractionRejected):
        output.response()
    append(path, "assistant", [{"type": "text", "text": "final answer"}])
    assert output.response() == "final answer"


def test_partial_json_record_waits_for_its_newline(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"type": "assistant", "message": {"id": "a", "content": [')
    output = QwenOutput(path)
    with pytest.raises(OutputExtractionRejected):
        output.response()
    with path.open("a") as stream:
        stream.write('{"type":"text","text":"whole answer"}]}}\n')
    assert output.response() == "whole answer"


def test_lost_or_truncated_output_fails_closed(tmp_path):
    path = tmp_path / "events.jsonl"
    path.touch()
    output = QwenOutput(path)
    append(path, "user", [{"type": "text", "text": "question"}])
    append(path, "assistant", [{"type": "text", "text": "answer"}])
    assert output.response() == "answer"
    path.write_text("")
    with pytest.raises(OutputExtractionError, match="truncated"):
        output.response()
    path.unlink()
    with pytest.raises(OutputExtractionError):
        output.response()


def test_malformed_record_stays_failed_instead_of_reusing_old_output(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("not JSON\n")
    output = QwenOutput(path)
    with pytest.raises(OutputExtractionError):
        output.response()
    append(path, "assistant", [{"type": "text", "text": "must not recover silently"}])
    with pytest.raises(OutputExtractionError):
        output.response()


def test_large_journal_is_fully_consumed_before_a_new_dispatch(tmp_path):
    path = tmp_path / "events.jsonl"
    ignored = json.dumps({"type": "stream_event", "padding": "x" * 1024}) + "\n"
    path.write_text(ignored * 8200)
    append(path, "user", [{"type": "text", "text": "old question"}])
    append(path, "assistant", [{"type": "text", "text": "old answer"}])
    output = QwenOutput(path)
    output.begin_dispatch()
    assert not output.has_current_user()
    append(path, "user", [{"type": "text", "text": "new question"}])
    append(path, "assistant", [{"type": "text", "text": "new answer"}])
    assert output.response() == "new answer"

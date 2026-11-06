import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.llm import extract_json_text  # noqa: E402
import json  # noqa: E402


def parse(raw):
    return json.loads(extract_json_text(raw))


def test_plain_fenced_think_and_prose_wrapped_json():
    assert parse('{"a": 1}') == {"a": 1}
    assert parse('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse('<think>let me think {not json}</think>\n{"a": {"b": 2}}') == {"a": {"b": 2}}
    assert parse('Sure! Here is the JSON you asked for:\n{"a": [1, 2]}\nHope that helps.') == {"a": [1, 2]}


def test_garbage_still_fails_so_the_retry_path_runs():
    import pytest
    for bad in ["no json here", "", "<think>only thoughts</think>", "{unclosed"]:
        with pytest.raises(ValueError):
            parse(bad)

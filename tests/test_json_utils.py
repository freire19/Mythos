"""Tests for alpha._json_utils.load_json_file."""

from __future__ import annotations

import json
import logging

from alpha._json_utils import load_json_file


def test_happy_path_returns_parsed_dict(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"a": 1, "b": [2, 3]}), encoding="utf-8")
    assert load_json_file(p) == {"a": 1, "b": [2, 3]}


def test_path_can_be_str(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"x": 1}', encoding="utf-8")
    assert load_json_file(str(p)) == {"x": 1}


def test_none_path_returns_default():
    assert load_json_file(None, default={"fallback": True}) == {"fallback": True}


def test_missing_file_returns_default(tmp_path):
    assert load_json_file(tmp_path / "missing.json", default=[]) == []


def test_malformed_json_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_json_file(p, default={}) == {}


def test_non_utf8_bytes_returns_default(tmp_path):
    p = tmp_path / "binary.json"
    p.write_bytes(b"\xff\xfe\xfd")
    assert load_json_file(p, default="fallback") == "fallback"


def test_default_is_none_when_unspecified(tmp_path):
    assert load_json_file(tmp_path / "missing.json") is None


def test_logger_warns_on_failure(tmp_path, caplog):
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    logger = logging.getLogger("alpha.test_json_utils.warns")
    with caplog.at_level(logging.WARNING, logger="alpha.test_json_utils.warns"):
        load_json_file(p, default={}, logger=logger)
    assert any("Failed to read" in rec.message for rec in caplog.records)


def test_logger_silent_on_success(tmp_path, caplog):
    p = tmp_path / "ok.json"
    p.write_text('{"ok": true}', encoding="utf-8")
    logger = logging.getLogger("alpha.test_json_utils.silent")
    with caplog.at_level(logging.WARNING, logger="alpha.test_json_utils.silent"):
        load_json_file(p, default={}, logger=logger)
    assert caplog.records == []


def test_no_logger_no_log_records(tmp_path, caplog):
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        load_json_file(p, default={})
    # Nothing in alpha.* loggers should have warned
    assert not any(rec.name.startswith("alpha.") for rec in caplog.records)


def test_returns_lists_arrays_scalars(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_json_file(p) == [1, 2, 3]

    p2 = tmp_path / "scalar.json"
    p2.write_text("42", encoding="utf-8")
    assert load_json_file(p2) == 42


def test_directory_path_returns_default(tmp_path):
    # Reading a directory raises OSError (IsADirectoryError) — must fall back.
    assert load_json_file(tmp_path, default={"d": 1}) == {"d": 1}

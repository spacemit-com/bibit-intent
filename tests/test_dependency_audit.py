"""依赖安装审计（``core/dependency_audit.py``）：记录、读取、轮转与容错。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.core import dependency_audit as audit


def test_audit_path_defaults_under_user_data_dir(monkeypatch):
    monkeypatch.delenv(audit.AUDIT_ENV, raising=False)
    assert audit.audit_path() == (
        Path.home() / ".local" / "share" / "bibit-intent" / "dependency-audit.jsonl"
    )


def test_audit_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_ENV, str(target))
    assert audit.audit_path() == target


def test_record_and_read_round_trip(tmp_path):
    audit.record({"action": "dep_apply", "plan_id": "dep-1", "n": 1})
    audit.record({"action": "dep_apply", "plan_id": "dep-2", "n": 2})

    records = audit.read_records(10)

    assert [r["plan_id"] for r in records] == ["dep-2", "dep-1"]  # 新→旧
    assert all("recorded_at" in record for record in records)


def test_read_records_respects_limit(tmp_path):
    for index in range(5):
        audit.record({"action": "dep_apply", "n": index})

    assert [r["n"] for r in audit.read_records(2)] == [4, 3]


def test_read_records_skips_bad_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n{"a": 2}\n', encoding="utf-8")

    records = audit.read_records(10, path=path)

    assert [r["a"] for r in records] == [2, 1]


def test_read_records_missing_file_is_empty(tmp_path):
    assert audit.read_records(5, path=tmp_path / "nope.jsonl") == []


def test_record_rotates_when_file_grows(monkeypatch):
    target = audit.audit_path()
    monkeypatch.setattr(audit, "_MAX_BYTES", 200)
    for index in range(20):
        audit.record({"action": "dep_apply", "n": index, "pad": "x" * 40})

    assert target.with_name(target.name + ".1").exists()
    assert target.exists()
    # 轮转后新文件里只剩尾部记录，旧记录在 .1 里仍可读
    assert audit.read_records(50)[0]["n"] == 19


def test_record_apply_keeps_only_known_item_fields(tmp_path):
    payload = audit.record_apply(
        plan={"plan_id": "dep-1", "project_root": "/p", "files_hash": "h", "scope": "project"},
        results=[{
            "index": 1, "label": "libssl-dev", "ok": True, "verified": True,
            "message": "m", "evidence": "e", "rollback": "r",
            "detail": "不该落盘的过程输出",
        }],
        note="n",
        overall=True,
    )

    assert payload["action"] == "dep_apply"
    assert payload["plan_id"] == "dep-1" and payload["ok"] is True
    item = payload["items"][0]
    assert "detail" not in item
    assert item["rollback"] == "r" and item["verified"] is True


def test_record_swallows_write_errors(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(audit.AUDIT_ENV, str(blocker / "audit.jsonl"))

    payload = audit.record({"action": "dep_apply"})  # 不抛异常

    assert payload["action"] == "dep_apply"

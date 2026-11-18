from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
from unittest.mock import patch

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import locale_tools, system_tools
from bibit_intent.execution.executor import Executor
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-global-and-restore", action="store_true")
    parser.add_argument("--backup", default="settings-global-backup.json")
    args = parser.parse_args()
    registry = ToolRegistry()
    report = {"hostname": platform.node(), "architecture": platform.machine(), "checks": []}

    def record(name, result):
        report["checks"].append({"name": name, "ok": result.ok,
                                 "message": result.message, "detail": result.detail})
        assert result.ok, f"{name}: {result.message}: {result.detail}"
        return result

    before = locale_tools._system_values()
    record("host_time", system_tools.get_system_time())
    record("global_locale", locale_tools.get_locale_settings())
    record("host_options", locale_tools.list_locale_options())
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    for text, tool in (("现在几点", "get_system_time"), ("今天几号", "get_system_time"),
                       ("查看系统语言和区域", "get_locale_settings"), ("查看可用区域", "list_locale_options")):
        route = router.handle(text)
        assert route.stage is Stage.RULE and route.tool_name == tool
        record(text, route.exec_result)
    for text in ("把系统时间调到下午三点", "把系统时间调快十分钟", "设置系统时间",
                 "系统语言改一下", "不要修改系统语言", "系统区域设置成合适的"):
        route = router.handle(text)
        assert route.stage is Stage.CLOUD and route.tool_name is None
        report["checks"].append({"name": text, "ok": True, "cloud_reason": route.escalate_reason})
    with patch.dict("os.environ", {"BIANBU_SANDBOX_ACTIVE": "1"}):
        for name, params in (("get_system_time", {}), ("set_system_time", {"datetime": "2026-10-21 14:30"}),
                             ("get_locale_settings", {}), ("list_locale_options", {}),
                             ("set_system_language", {"locale": "英文"}), ("set_system_region", {"locale": "中国"})):
            result = system_tools.HANDLERS[name](**params)
            assert not result.ok and result.escalate == "needs_host_channel"
            report["checks"].append({"name": f"sandbox_requires_host_{name}", "ok": True})
    if args.apply_global_and_restore:
        backup = Path(args.backup)
        with backup.open("x", encoding="utf-8") as stream:
            json.dump(before, stream, ensure_ascii=False, indent=2)
        try:
            for target, prefix in (("英文", "en_US"), ("简体中文", "zh_CN")):
                old_region = json.loads(locale_tools.get_locale_settings().detail)["region"]
                record(f"global_language_{prefix}", locale_tools.set_system_language(target))
                detail = json.loads(record(f"read_language_{prefix}", locale_tools.get_locale_settings()).detail)
                assert detail["language"].startswith(prefix + ".") and detail["region"] == old_region
            for target in ("en_US", "en_GB", "zh_CN"):
                old_language = json.loads(locale_tools.get_locale_settings().detail)["language"]
                record(f"global_region_{target}", locale_tools.set_system_region(target))
                detail = json.loads(record(f"read_region_{target}", locale_tools.get_locale_settings()).detail)
                assert detail["region"].startswith(target + ".") and detail["language"] == old_language
        finally:
            current = locale_tools._system_values()
            result = locale_tools._run(["localectl", "--no-ask-password", "set-locale",
                                        *(f"{key}={before.get(key, '')}" for key in sorted(set(current) | set(before)))])
            record("restore_original_global_locale", result)
    assert locale_tools._system_values() == before
    report["checks"].append({"name": "original_global_locale_verified", "ok": True})
    report["limitations"] = ["No actual system time change or NTP disable attempted",
                              "No logout/login or production service deployment performed"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

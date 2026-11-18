from __future__ import annotations

import argparse
import json
import platform
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution import wallpaper_helper, wallpaper_tools
from bibit_intent.execution.executor import Executor
from bibit_intent.routing.router import Router, Stage
from bibit_intent.routing.rule_engine import RuleEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--apply-and-restore", action="store_true", required=True)
    args = parser.parse_args()
    report = {"hostname": platform.node(), "architecture": platform.machine(), "checks": []}

    def check(name, ok, detail=""):
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        assert ok, f"{name}: {detail}"

    candidates = wallpaper_tools._desktop_processes()
    if len(candidates) != 1:
        raise ValueError("Expected one desktop session")
    process_id, owner = candidates[0]
    environment, home, runtime, roots, profile = wallpaper_helper._context(process_id)
    config, before = wallpaper_helper._settings(roots, profile)
    previous = before.get("Wallpaper", "")
    if not Path(previous).is_file() or before.get("WallpaperMode") not in ("stretch", "fit", "center", "tile", "zoom"):
        raise ValueError("Cannot safely restore the original wallpaper and mode")
    with Path(args.backup).open("x", encoding="utf-8") as stream:
        json.dump({"config": str(config), "desktop": before}, stream, ensure_ascii=False, indent=2)
    report["original"] = {"path": previous, "mode": before["WallpaperMode"], "profile": profile}
    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry))
    temporary = tempfile.mkdtemp(prefix="bibit-wallpaper-", dir=home)
    restored = False
    try:
        image = Path(temporary) / "壁纸 测试.png"
        shutil.copyfile(args.image, image)
        try:
            result = wallpaper_tools.set_wallpaper(args.image)
            check("set_explicit_system_image", result.ok, result.detail)
            result = router.handle(f'把"{image}"设为桌面壁纸')
            check("route_chinese_space_path", result.stage is Stage.RULE and result.exec_result.ok,
                  result.exec_result.detail if result.exec_result else "no execution result")
            _, values = wallpaper_helper._settings(roots, profile)
            check("verify_image_and_preserved_mode", values["Wallpaper"] == str(image) and values["WallpaperMode"] == before["WallpaperMode"])
            for name, value in (("tilde_path", "~/" + str(image.relative_to(home))), ("file_uri", image.as_uri())):
                result = wallpaper_tools.set_wallpaper(value)
                check(name, result.ok, result.detail)
            corrupt = Path(temporary) / "corrupt.png"
            corrupt.write_text("This is not an image")
            for name, value in (("missing_image", str(Path(temporary) / "missing.png")),
                                ("directory_not_image", temporary), ("corrupt_image", str(corrupt)),
                                ("remote_url_rejected", "https://example.com/image.png")):
                result = wallpaper_tools.set_wallpaper(value)
                _, values = wallpaper_helper._settings(roots, profile)
                check(name, not result.ok and values["Wallpaper"] == str(image), result.detail)
            for text in ("切换壁纸", "下一张壁纸", "把下载目录里的图片设为壁纸",
                         f"不要把{args.image}设为壁纸", f"把{args.image}设为壁纸然后关机"):
                result = router.handle(text)
                check(text, result.stage is Stage.CLOUD and result.tool_name is None)
            with patch.dict("os.environ", {"BIANBU_SANDBOX_ACTIVE": "1"}):
                result = wallpaper_tools.set_wallpaper(args.image)
                check("sandbox_requires_host", not result.ok and result.escalate == "needs_host_channel")
        finally:
            result = wallpaper_tools.set_wallpaper(previous)
            check("restore_original_wallpaper", result.ok, result.detail)
            _, after = wallpaper_helper._settings(roots, profile)
            check("verify_restored_path_and_mode", after["Wallpaper"] == previous and after["WallpaperMode"] == before["WallpaperMode"])
            restored = True
    finally:
        if restored:
            shutil.rmtree(temporary)
        else:
            report["retained_test_directory"] = temporary
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

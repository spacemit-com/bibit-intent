"""MCP manifest 的安装归属：Runtime 分发，Bibit 只提供源。

Runtime 从 `/usr/share/bianbu-agent-runtime/mcps`（含 `bianbu.bibit.intent` 插件内嵌的
`mcps/bibit-intent/`）与 `~/.bianbu-agent/mcps` 两处发现 MCP，按 id 去重；插件成员的 id
必然带插件前缀，与用户目录里那份互相覆盖不了，两份同时加载会让内置专家的 `mcps: ['*']`
展开成两套重名的工具。

本包只安装 `/opt/bibit-intent/mcp.json` 作为被引用的源，用户目录里的历史副本由
`bibit-intent-init.sh` 退休；旧 Runtime 还没有插件成员时，两个脚本保留原来的写入行为。

退休的硬约束来自 Runtime 自己：`mcp/manager.py::_snapshot_open_package`
要求 MCP 包目录里**恰好**只有 `mcp.json`，所以不能就地改名留备份——那样
整份目录列表会报 ValueError。备份因此整体挪到 `~/.bibit-intent/retired-mcp/`。
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
INIT = REPO / "deploy" / "bibit-intent-init.sh"
POSTINST = REPO / "debian" / "postinst"
RULES = REPO / "debian" / "rules"
HELPER = REPO / "deploy" / "install-user-mcp.py"
LEGACY_DIR = Path(".bianbu-agent/mcps/bibit-intent")
RETIRED_ROOT = Path(".bibit-intent/retired-mcp")


PLUGIN_MCP_ROOT = Path(
    "/usr/share/bianbu-agent-runtime/plugins/bianbu.bibit.intent/mcps"
)


def run_init_manifest_stage(
    home: Path,
    *,
    plugin_present: bool = True,
    source: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """只跑 init.sh 的 MCP manifest 段。

    截到模型段之前，这样测试不碰 /opt/bibit-intent 下真实存在的脚本。
    `id` 被打桩成普通用户：脚本开头对 root 直接返回，测试不该因此静默跳过
    （CI 里可能以 root 运行）。插件路径与 /opt 源都在临时目录里重建，测试不依赖
    这台机器上装没装 Runtime 包。
    """
    script = INIT.read_text(encoding="utf-8")
    stage = script.split("# \u2500\u2500 2) default models", 1)[0]
    assert "retired-mcp" in stage, "init.sh 的 manifest 段没有解析到"

    root = home.parent
    plugin_root = root / "runtime-plugin"
    source_path = root / "opt-mcp.json"
    if source is not None:
        source_path.write_text(source, encoding="utf-8")
    if plugin_present:
        for mcp_name in ("bibit-readonly", "bibit-control", "bibit-admin"):
            directory = plugin_root / mcp_name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "mcp.json").write_text(source or '{"transport": "stdio"}\n', encoding="utf-8")
    stage = stage.replace(
        f"RUNTIME_PLUGIN_MCP_ROOT={PLUGIN_MCP_ROOT}",
        f"RUNTIME_PLUGIN_MCP_ROOT={plugin_root}",
    )
    stage = stage.replace("MCP_SRC=/opt/bibit-intent/mcp.json", f"MCP_SRC={source_path}")
    assert f"RUNTIME_PLUGIN_MCP_ROOT={plugin_root}" in stage

    harness = "id() { printf '1000\\n'; }\n" + stage
    return subprocess.run(
        ["sh", "-c", harness],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )


def seed_legacy_manifest(home: Path, content: str = '{"transport": "stdio"}\n') -> Path:
    directory = home / LEGACY_DIR
    directory.mkdir(parents=True)
    (directory / "mcp.json").write_text(content, encoding="utf-8")
    return directory


def retired_copies(home: Path) -> list[Path]:
    root = home / RETIRED_ROOT
    return sorted(root.glob("bibit-intent-*")) if root.is_dir() else []


def test_postinst_only_writes_into_a_user_home_without_the_runtime_plugin():
    """写用户主目录这件事必须挂在「旧 Runtime」分支里，且跳过 root。"""
    script = POSTINST.read_text(encoding="utf-8")

    guard = script.index('RUNTIME_PLUGIN_MCP_ROOT=')
    loop = script.index('for mcp_name in bibit-readonly bibit-control bibit-admin')
    write = script.index("install-user-mcp.py")
    fallback = script.index('elif [ -f "$MCP_SOURCE" ]')
    assert guard < loop < fallback < write, "写用户目录的路径必须在插件缺失分支里"
    assert 'echo "bibit-intent: Runtime Bibit Plugin ships this MCP' in script
    assert '[ "$u" = "root" ] && continue' in script
    # 服务启用/重启那段必须保留：MCP 换了分发方，服务本身没换。
    assert "bibit-intent-init.sh" in (REPO / "deploy" / "bibit-intent.service").read_text(encoding="utf-8")


def test_packaging_ships_the_user_manifest_helper_for_the_legacy_runtime():
    assert HELPER.exists()
    assert "install-user-mcp.py" in RULES.read_text(encoding="utf-8")


def test_packaging_still_ships_the_manifest_runtime_vendors():
    """/opt/bibit-intent/mcp.json 留着：Runtime 插件的副本以它为源。"""
    rules = RULES.read_text(encoding="utf-8")

    assert "deploy/mcp.json $(INSTALL_ROOT)/mcp.json" in rules


def test_init_retires_the_legacy_manifest_into_a_backup(tmp_path: Path):
    source = '{"transport": "stdio", "command": "/opt/bibit-intent/.venv/bin/python"}\n'
    seed_legacy_manifest(tmp_path, source)

    result = run_init_manifest_stage(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / LEGACY_DIR).exists()
    copies = retired_copies(tmp_path)
    assert len(copies) == 1
    assert (copies[0] / "mcp.json").read_text(encoding="utf-8") == source
    assert "retired legacy user MCP manifest" in result.stdout


def test_init_leaves_no_package_directory_in_the_mcp_root(tmp_path: Path):
    """Runtime 要求包目录里恰好只有 mcp.json，留半个目录会打断整份列表。"""
    seed_legacy_manifest(tmp_path)

    run_init_manifest_stage(tmp_path)

    root = tmp_path / ".bianbu-agent/mcps"
    assert [entry.name for entry in root.iterdir()] == []


def test_init_moves_unrelated_files_along_with_the_manifest(tmp_path: Path):
    """整目录搬走：用户在该目录里的其它文件不能被丢掉。"""
    directory = seed_legacy_manifest(tmp_path)
    (directory / "notes.txt").write_text("mine", encoding="utf-8")

    run_init_manifest_stage(tmp_path)

    copies = retired_copies(tmp_path)
    assert len(copies) == 1
    assert (copies[0] / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_init_does_not_create_a_manifest_when_none_exists(tmp_path: Path):
    (tmp_path / ".bianbu-agent").mkdir()

    result = run_init_manifest_stage(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / LEGACY_DIR).exists()
    assert retired_copies(tmp_path) == []
    assert "retired" not in result.stdout


def test_init_retires_a_reinstalled_manifest_again(tmp_path: Path):
    """老包重装会再写一份；服务每次启动都收敛，且不覆盖上一份备份。"""
    seed_legacy_manifest(tmp_path, "first\n")
    run_init_manifest_stage(tmp_path)
    seed_legacy_manifest(tmp_path, "second\n")

    result = run_init_manifest_stage(tmp_path)

    assert result.returncode == 0, result.stderr
    copies = retired_copies(tmp_path)
    assert len(copies) == 2
    assert sorted((copy / "mcp.json").read_text(encoding="utf-8") for copy in copies) == ["first\n", "second\n"]


def test_init_installs_the_manifest_when_the_runtime_has_no_plugin(tmp_path: Path):
    """旧 Runtime 没有插件来源：保留历史写入行为，别把唯一的 MCP 也收走。"""
    source = '{"transport": "stdio"}\n'
    home = tmp_path / "home"
    home.mkdir()

    result = run_init_manifest_stage(home, plugin_present=False, source=source)

    assert result.returncode == 0, result.stderr
    installed = home / LEGACY_DIR / "mcp.json"
    assert installed.read_text(encoding="utf-8") == source
    assert retired_copies(home) == []
    assert "installed mcp.json" in result.stdout


def test_init_refreshes_a_stale_manifest_when_the_runtime_has_no_plugin(
    tmp_path: Path,
):
    """Runtime 升级到新插件后这份副本会退休，之前必须跟着 /opt 的源走。"""
    home = tmp_path / "home"
    home.mkdir()
    seed_legacy_manifest(home, '{"transport": "stale"}\n')

    run_init_manifest_stage(home, plugin_present=False, source='{"transport": "stdio"}\n')

    assert (home / LEGACY_DIR / "mcp.json").read_text(encoding="utf-8") == '{"transport": "stdio"}\n'


def test_init_keeps_an_up_to_date_manifest_untouched_when_there_is_no_plugin(
    tmp_path: Path,
):
    home = tmp_path / "home"
    home.mkdir()
    source = '{"transport": "stdio"}\n'
    seed_legacy_manifest(home, source)

    result = run_init_manifest_stage(home, plugin_present=False, source=source)

    assert result.returncode == 0, result.stderr
    assert (home / LEGACY_DIR / "mcp.json").read_text(encoding="utf-8") == source
    assert "installed mcp.json" not in result.stdout


def test_init_does_not_retire_while_the_plugin_is_absent(tmp_path: Path):
    """插件缺失时退休等于让机器失去唯一的 MCP，这是这次改动要修的回归。"""
    home = tmp_path / "home"
    home.mkdir()
    seed_legacy_manifest(home, "user-tuned\n")

    result = run_init_manifest_stage(home, plugin_present=False)

    assert result.returncode == 0, result.stderr
    assert retired_copies(home) == []
    assert (home / LEGACY_DIR / "mcp.json").read_text(encoding="utf-8") == "user-tuned\n"

"""H16：MCP manifest 里的 PATH（appcli 入口命令必须能被找到）。

Runtime 的 MCP stdio 子进程**只拿到 manifest 里的 env**，不继承 `os.environ`
（见 Runtime 仓 `bianbu_agent/mcp/client.py::_stdio_environment`：只有配置 env 加三个身份变量）。
而 `appcli_run` 是按注册表的裸命令名 Popen 的（`cli-anything-libreoffice`），前置检查也用
`shutil.which`。appcli wheel 装在 `/opt/bibit-intent/.venv` 里，PATH 里没有它的 bin，
就会「装了却说不在 PATH」——所以 manifest 必须把 venv/bin 放在 PATH 最前面。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "deploy" / "mcp.json"


def test_manifest_path_exposes_the_bibit_venv_bin_dir():
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    path = document["env"]["PATH"].split(":")
    assert path[0] == "/opt/bibit-intent/.venv/bin"
    # 系统目录保留：appcli 自己还要调 soffice/drawio 这些系统命令。
    assert "/usr/bin" in path


def test_manifest_keeps_the_stdio_contract():
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["transport"] == "stdio"
    assert document["command"] == "/opt/bibit-intent/.venv/bin/python"
    assert document["args"] == ["-m", "bibit_intent.mcp_server"]
    assert document["env"]["PYTHONPATH"] == "/opt/bibit-intent/src"
    assert document["env"]["BIBIT_MCP_TRANSPORT"] == "stdio"


def test_deb_installs_the_same_manifest():
    """/opt/bibit-intent/mcp.json 是 deb 安装的那份（init.sh 再从它同步到用户目录）。"""
    rules = (REPO / "debian" / "rules").read_text(encoding="utf-8")

    assert "deploy/mcp.json $(INSTALL_ROOT)/mcp.json" in rules


def test_service_unit_puts_the_same_venv_bin_on_path():
    """HTTP 路径（服务进程）与 MCP 路径（manifest env）必须给出同一个判定。"""
    unit = (REPO / "deploy" / "bibit-intent.service").read_text(encoding="utf-8")

    assert (
        "Environment=PATH=/opt/bibit-intent/.venv/bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin" in unit
    )

"""net_ip Markdown 表格输出回归测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from bibit_intent.resources import resource_path


def load_handlers():
    spec = importlib.util.spec_from_file_location(
        "net_ip_markdown_handlers", resource_path("skills/ubuntu_cmd/handlers.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_net_ip_renders_markdown_table(monkeypatch):
    module = load_handlers()
    payload = [
        {
            "ifname": "lo",
            "operstate": "UNKNOWN",
            "address": "00:00:00:00:00:00",
            "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}],
        },
        {
            "ifname": "eth0",
            "operstate": "UP",
            "address": "aa:bb:cc:dd:ee:ff",
            "addr_info": [
                {"family": "inet", "local": "192.168.1.10", "prefixlen": 24},
                {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
            ],
        },
    ]

    def fake_run(cmd, timeout=20):
        if cmd[:2] == ["ip", "-j"]:
            return module.ExecResult(True, "ok", json.dumps(payload))
        if cmd and cmd[0] == "nmcli":
            return module.ExecResult(True, "ok", "IP4.DNS[1]:10.0.0.2\nIP6.DNS[1]:::1\n")
        return module.ExecResult(False, "fail", "")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.net_ip()
    assert result.ok is True
    assert "| 网卡 | 状态 | IPv4 | IPv6 | MAC |" in result.detail
    assert "eth0" in result.detail
    assert "192.168.1.10/24" in result.detail
    assert "fe80::1/64" in result.detail
    assert "**DNS 地址**" in result.detail
    assert "10.0.0.2" in result.detail


def test_net_ip_falls_back_to_ip_text(monkeypatch):
    module = load_handlers()
    text = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP group default qlen 1000
    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff
    inet 10.0.91.72/24 brd 10.0.91.255 scope global eth0
"""

    def fake_run(cmd, timeout=20):
        if cmd[:2] == ["ip", "-j"]:
            return module.ExecResult(False, "unsupported", "Option -j not supported")
        if cmd == ["ip", "addr"]:
            return module.ExecResult(True, "ok", text)
        if cmd and cmd[0] == "nmcli":
            return module.ExecResult(False, "fail", "")
        return module.ExecResult(False, "fail", "")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.Path, "read_text", lambda self, encoding=None, errors=None: "nameserver 10.0.0.2\n")
    result = module.net_ip()
    assert result.ok is True
    assert "eth0" in result.detail
    assert "10.0.91.72/24" in result.detail
    assert "10.0.0.2" in result.detail

"""
tests/test_skill_slots.py — Skill 规则的槽位抽取回归测试（全离线，可直接运行）

运行： python3 tests/test_skill_slots.py

背景：
    形如 `(?:安装|装).{0,4}(?P<pkg>[a-zA-Z0-9_+.-]{1,40})` 的模式里，贪心
    `.{0,N}` 会先吞掉包名前缀，再回溯只留最后一两个字符给命名组：
        「帮我安装 vim」   → pkg="m"        → apt-get install m
        「杀掉1234号进程」  → pid=4          → 误杀内核线程
        「屏蔽192.168.1.100」→ ip="2.168.1.100" → 封错网段
    修法：把紧跟命名组的 `.{0,N}` 全部改成懒惰 `.{0,N}?`（28 处），
    并把 pkg_remove 的跨度 {0,2} 放宽到 {0,4}（"卸载软件 nginx" 需跳 3 字符）。

本文件锁死修复结果，防止再退化。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.resources import resource_path
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.routing.rule_engine import RuleEngine

# ── 用例：(输入, 期望工具, 期望槽位子集) ──────────────────────────────
SLOT_CASES = [
    ("帮我屏蔽ip 30.0.90.254", "iptables_block", {"ip": "30.0.90.254"}),
    ("对/tmp目录进行一次病毒扫描", "sec_antivirus", {"path": "/tmp"}),
    ("对/tmp进行病毒扫描", "sec_antivirus", {"path": "/tmp"}),
    ("扫描/tmp目录的病毒", "sec_antivirus", {"path": "/tmp"}),
    ("扫描 /var 目录有没有病毒", "sec_antivirus", {"path": "/var"}),
    # A. 包名不能被吞（本次 bug 现场）
    ("帮我安装 vim",        "pkg_install", {"pkg": "vim"}),
    ("帮我安装vim",         "pkg_install", {"pkg": "vim"}),
    ("安装vim",             "pkg_install", {"pkg": "vim"}),
    ("装个vim",             "pkg_install", {"pkg": "vim"}),
    ("装一个 htop",         "pkg_install", {"pkg": "htop"}),
    ("安装 python3-pip",    "pkg_install", {"pkg": "python3-pip"}),
    ("帮我安装一下 nginx",  "pkg_install", {"pkg": "nginx"}),
    ("安装软件 vim",        "pkg_install", {"pkg": "vim"}),
    ("安装vim这个软件",     "pkg_install", {"pkg": "vim"}),
    ("下载安装 curl",       "pkg_install", {"pkg": "curl"}),
    ("卸载 firefox",        "pkg_remove",  {"pkg": "firefox"}),
    ("卸载firefox",         "pkg_remove",  {"pkg": "firefox"}),
    ("卸载软件 nginx",      "pkg_remove",  {"pkg": "nginx"}),
    ("移除软件 htop",       "pkg_remove",  {"pkg": "htop"}),
    ("删除 vim 这个软件",   "pkg_remove",  {"pkg": "vim"}),
    ("把 firefox 卸载了",   "pkg_remove",  {"pkg": "firefox"}),

    # B. "删除用户/账号" 不能被 pkg_remove 抢走（负向前瞻仍生效）
    ("删除用户test",        "user_del",    {"name": "test"}),
    ("删除账号 test",       "user_del",    {"name": "test"}),
    ("新建用户 alice",      "user_add",    {"name": "alice"}),

    # C. 危险数值不能被吞（误杀/误封）
    ("杀掉1234号进程",      "proc_kill",   {"pid": 1234}),
    ("杀掉1234进程",        "proc_kill",   {"pid": 1234}),
    ("屏蔽192.168.1.100这个ip", "iptables_block", {"ip": "192.168.1.100"}),

    # D. 路径/域名/文件名不能被吞（只读工具，但参数须完整）
    ("查看软件curl安装内容",            "pkg_files",     {"pkg": "curl"}),
    ("查看openssh-client这个包的信息",  "pkg_info",      {"pkg": "openssh-client"}),
    ("看看/bin/ls这个文件属于哪个软件包", "pkg_owns_file", {"file": "/bin/ls"}),
    ("查看/etc/passwd这个文件的类型",   "file_info",     {"path": "/etc/passwd"}),
    ("跟踪baidu.com的路由路径",         "net_trace",     {"host": "baidu.com"}),
    ("挂载/dev/sdb1到/mnt/c",           "disk_mount",    {"dev": "/dev/sdb1",
                                                          "path": "/mnt/c"}),
    ("卸载/mnt/c",                      "disk_umount",   {"target": "/mnt/c"}),
    # English package/user/network/path coverage
    ("install vim",                     "pkg_install",    {"pkg": "vim"}),
    ("please install python3-pip",      "pkg_install",    {"pkg": "python3-pip"}),
    ("remove firefox",                  "pkg_remove",     {"pkg": "firefox"}),
    ("create user alice",               "user_add",       {"name": "alice"}),
    ("delete user test",                "user_del",       {"name": "test"}),
    ("block IP 192.168.1.100",         "iptables_block", {"ip": "192.168.1.100"}),
    ("show package files for curl",     "pkg_files",      {"pkg": "curl"}),
    ("which package owns /bin/ls",      "pkg_owns_file",  {"file": "/bin/ls"}),
    ("show info for openssh-client",    "pkg_info",       {"pkg": "openssh-client"}),
    ("show file info /etc/passwd",      "file_info",      {"path": "/etc/passwd"}),
    ("trace route to baidu.com",        "net_trace",      {"host": "baidu.com"}),
    ("mount /dev/sdb1 on /mnt/c",       "disk_mount",     {"dev": "/dev/sdb1", "path": "/mnt/c"}),
    ("unmount /mnt/c",                  "disk_umount",    {"target": "/mnt/c"}),
]


def main() -> int:
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    engine = RuleEngine(reg)

    passed = failed = 0
    for text, exp_tool, exp_slots in SLOT_CASES:
        m = engine.match(text)
        got_tool = m.tool_name if m else None
        got_slots = dict(m.slots) if m else {}
        ok = got_tool == exp_tool and all(
            str(got_slots.get(k)) == str(v) for k, v in exp_slots.items()
        )
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"❌ {text!r} → {got_tool} {got_slots}"
                  f"（期望 {exp_tool} {exp_slots}）")

    print(f"\n槽位抽取：{passed}/{passed + failed} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

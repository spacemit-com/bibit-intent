"""
tests/multi_intent/probe.py — 端侧 1.7B 多意图拆分能力压测

目的：验证在「放开 GBNF 语法允许多行 CALL」后，Qwen3-1.7B 能否
     稳定地把复合指令拆成多个意图。这不是功能实现，是一次能力探针。

只读探针：不改动 agent/ 任何代码，通过 AI Gateway 请求原生 completion。
用一个多行版 GBNF（允许 1~N 个 CALL），对照现有单行语法的差异只在 root。

指标：
  - intent_count_acc : 拆出的意图数是否正确
  - toolset_acc      : 工具集合是否正确（忽略顺序）
  - order_acc        : 有序依赖组的顺序是否正确
  - param_acc        : 参数是否正确（对能标注参数的用例）
  - miss_second      : 【最关键】复合指令是否漏掉第二个及以后的意图
  - over_split       : 单意图是否被过度拆分成多个

用法（导入 Gateway 环境配置后）：
  python3 probe.py
  python3 probe.py --n 3   # 每条跑3次看稳定性
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

# ── 多行版 GBNF：唯一改动是 root 允许多个 cmd 用换行分隔 ──────────────
# 对照 agent/local_model.py 的单行 root ::= call-cmd | escalate-cmd
GRAMMAR_MULTI = r"""
root        ::= cmd ("\n" cmd){0,4}
cmd         ::= call-cmd | escalate-cmd
call-cmd    ::= "CALL " tool-name kv-list
escalate-cmd ::= "ESCALATE reason=" word
tool-name   ::= [a-z_]+
kv-list     ::= (" " kv-pair){0,5}
kv-pair     ::= word "=" value
word        ::= [a-z_]+
value       ::= [a-z0-9_.]+
"""

# 端侧工具签名清单（与 registry.local_tool_list 输出保持一致，手写固化以免依赖）
TOOL_LIST = """vol(level:0-100)  // 音量/声音/大声/调大/调小
bright(level:0-100)  // 亮度/屏幕/太暗/太亮/看不清
mute(on:true/false)  // 静音
sysinfo(what:cpu|mem|temp|disk|all)  // CPU/内存/温度/磁盘/系统状态
reboot()  // 重启设备
launch_app(name)  // 打开/启动应用
close_app(name)  // 关闭/退出应用
wifi_connect(ssid password)  // 连接WiFi/连网"""

SYSTEM = (
    "你是端侧控制助手。用户可能一句话包含多个操作，请为每个操作输出一行：\n"
    "CALL <工具> <参数>=<值>  或  ESCALATE reason=<原因>\n"
    "有几个操作就输出几行，不要合并，不要解释。\n"
    "可用工具：\n"
    f"{TOOL_LIST}\n"
    "写作/开放问答/闲聊/工具无法完成的任务，输出 ESCALATE reason=out_of_scope。"
)

_CALL_RE = re.compile(r"^CALL\s+(?P<tool>[a-z_]+)(?:\s+(?P<kvs>.*))?$")
# short → full 名映射（对照 registry._short2full）
SHORT2FULL = {
    "vol": "set_volume", "bright": "set_brightness", "mute": "mute",
    "sysinfo": "get_system_info", "reboot": "reboot",
    "launch_app": "launch_app", "close_app": "close_app",
    "wifi_connect": "wifi_connect",
}


def parse_multi(raw: str) -> list[dict]:
    """解析多行 CALL 输出为 [{tool, params}, ...]。"""
    out = []
    for line in raw.strip().splitlines():
        line = line.strip()
        m = _CALL_RE.match(line)
        if not m:
            continue
        tool = SHORT2FULL.get(m.group("tool"), m.group("tool"))
        params = {}
        for kv in (m.group("kvs") or "").split():
            k, _, v = kv.partition("=")
            if not k:
                continue
            try:
                params[k] = int(v)
            except ValueError:
                params[k] = v
        out.append({"tool": tool, "params": params})
    return out


def infer(url: str, user_input: str, timeout: int = 60) -> tuple[str, float]:
    prompt = f"{SYSTEM}\n\n用户：{user_input}\n指令："
    payload = {
        "prompt": prompt,
        "grammar": GRAMMAR_MULTI,
        "n_predict": 96,               # 多意图需要更多输出预算
        "temperature": 0.1,
        "stop": ["用户：", "\n\n"],     # 双换行才停，单换行是意图分隔符
        "chat_template_kwargs": {"enable_thinking": False},
    }
    from dataclasses import replace
    from bibit_intent.models.backends import local_client
    from bibit_intent.pipeline import PipelineSettings

    client = local_client(replace(PipelineSettings.from_environment(), local_timeout_seconds=timeout))
    if not client.is_available():
        raise RuntimeError("Gateway LLM is unavailable")
    t0 = time.monotonic()
    body = client._post("/completion", payload)
    return body.get("content", "").strip(), (time.monotonic() - t0) * 1000


def score(expect: list[dict], got: list[dict], is_ordered: bool) -> dict:
    exp_tools = [e["tool"] for e in expect]
    got_tools = [g["tool"] for g in got]
    count_ok = len(got) == len(expect)
    toolset_ok = sorted(got_tools) == sorted(exp_tools)
    order_ok = got_tools == exp_tools if is_ordered else None
    # 参数正确率：只校验 expect 里标注了 params 的项
    param_ok = True
    for e in expect:
        if not e.get("params"):
            continue
        hit = any(
            g["tool"] == e["tool"]
            and all(str(g["params"].get(k)) == str(v) for k, v in e["params"].items())
            for g in got
        )
        if not hit:
            param_ok = False
            break
    return {
        "count_ok": count_ok,
        "toolset_ok": toolset_ok,
        "order_ok": order_ok,
        "param_ok": param_ok,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1, help="每条重复次数(看稳定性)")
    ap.add_argument("--corpus", default=str(Path(__file__).parent / "corpus.jsonl"))
    args = ap.parse_args()

    cases = [json.loads(l) for l in Path(args.corpus).read_text(encoding="utf-8").splitlines() if l.strip()]
    multi = [c for c in cases if len(c["expect"]) > 1]
    single = [c for c in cases if len(c["expect"]) == 1]

    print(f"# 多意图拆分压测  url=configured AI Gateway  用例={len(cases)}(复合{len(multi)}/单{len(single)})  重复={args.n}\n")

    agg = {"count": 0, "toolset": 0, "order_total": 0, "order_ok": 0,
           "param": 0, "miss_second": 0, "over_split": 0, "total": 0}
    lat = []

    for c in cases:
        is_ordered = c["group"] == "有序依赖"
        n_exp = len(c["expect"])
        for _ in range(args.n):
            try:
                raw, ms = infer(c["input"])
            except Exception as e:
                print(f"[{c['id']}] 请求失败: {e}")
                continue
            lat.append(ms)
            got = parse_multi(raw)
            s = score(c["expect"], got, is_ordered)
            agg["total"] += 1
            agg["count"] += s["count_ok"]
            agg["toolset"] += s["toolset_ok"]
            agg["param"] += s["param_ok"]
            if is_ordered:
                agg["order_total"] += 1
                agg["order_ok"] += bool(s["order_ok"])
            # 漏第二意图：期望≥2 但只拆出<期望
            if n_exp >= 2 and len(got) < n_exp:
                agg["miss_second"] += 1
            # 过度拆分：期望=1 但拆出>1
            if n_exp == 1 and len(got) > 1:
                agg["over_split"] += 1

            flag = "✓" if (s["count_ok"] and s["toolset_ok"] and s["param_ok"]) else "✗"
            got_str = " | ".join(f"{g['tool']}{g['params'] or ''}" for g in got) or "(空)"
            print(f"[{flag}] {c['id']} {c['group']:8s} 期望{n_exp} 得到{len(got)}  {ms:5.0f}ms")
            print(f"     输入: {c['input']}")
            print(f"     输出: {got_str}")
            if not (s["count_ok"] and s["toolset_ok"] and s["param_ok"]):
                print(f"     期望: " + " | ".join(f"{e['tool']}{e.get('params','') or ''}" for e in c["expect"]))
                print(f"     原始: {raw!r}")

    t = agg["total"] or 1
    print("\n" + "=" * 60)
    print(f"总样本         : {agg['total']}")
    print(f"意图数正确率   : {agg['count']}/{t} = {agg['count']/t:.0%}")
    print(f"工具集正确率   : {agg['toolset']}/{t} = {agg['toolset']/t:.0%}")
    print(f"参数正确率     : {agg['param']}/{t} = {agg['param']/t:.0%}")
    if agg["order_total"]:
        print(f"有序依赖顺序   : {agg['order_ok']}/{agg['order_total']} = {agg['order_ok']/agg['order_total']:.0%}")
    n_multi_samples = sum(1 for c in cases if len(c['expect']) >= 2) * args.n
    n_single_samples = sum(1 for c in cases if len(c['expect']) == 1) * args.n
    print(f"漏意图(复合)   : {agg['miss_second']}/{n_multi_samples or 1}  <<< 关键指标")
    print(f"过度拆分(单)   : {agg['over_split']}/{n_single_samples or 1}")
    if lat:
        lat.sort()
        print(f"延迟 中位/p90  : {lat[len(lat)//2]:.0f}ms / {lat[int(len(lat)*0.9)]:.0f}ms")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

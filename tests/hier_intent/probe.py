"""
tests/hier_intent/probe.py — 端侧 1.7B 粗分类(层级意图第一层)准确率探针

目的:验证 hierarchical_intent_design.md §7 的决定性变量——粗分类正确率。
     只测「把一句话归到 7 个大类之一(或 escalate)」,不做工具细选。

只读探针:通过 AI Gateway 请求原生 completion,不改动 agent/ 代码。
GBNF 强制输出恰好一个类目 token,杜绝格式噪声。

产出:
  - 总准确率 P(粗)
  - 各类目召回(recall)
  - 混淆矩阵(看哪两类最易混——设计文档 §4 的边界威胁点)
  - 边界组单独准确率(BD* 用例,最能反映类目边界质量)

判据(设计文档 §7):
  P≥95% 绿灯 / 85~95% 改类目边界 / <85% 维持扁平

用法:
  python3 probe.py --n 2
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

# 7 个类目(与 corpus 的 cat 字段、设计文档 §4 一致)
CATS = ["sys_control", "sys_diag", "app_control",
        "network", "knowledge", "soft_mgmt", "escalate"]

# GBNF：只允许输出 7 个类目名之一
GRAMMAR = 'root ::= "' + '" | "'.join(CATS) + '"\n'

# 粗分类系统提示（极简，约 90 token）。只描述"选大类",不涉及具体工具。
SYSTEM = (
    "你是意图分类器。把用户这句话归到下面某一大类，只输出类目英文名，不解释。\n"
    "sys_control : 调节音量/亮度/静音/重启等设备控制(祈使动作)\n"
    "sys_diag    : 查询CPU/内存/温度/磁盘/系统状态(疑问/查看)\n"
    "app_control : 打开或关闭某个应用程序(浏览器/微信/终端等)\n"
    "network     : 连接/断开/扫描/查看 WiFi 无线网络\n"
    "knowledge   : 进迭时空/RISC-V/Bianbu 等领域知识问答(含'怎么装'教程)\n"
    "soft_mgmt   : 安装/卸载/更新软件(祈使执行装卸)\n"
    "escalate    : 写作/编程/办公/文件处理/开放百科/多步复合任务/日程提醒"
)

_LABEL_RE = re.compile(r"(" + "|".join(CATS) + r")")


def infer(text: str, timeout: int = 60) -> tuple[str, float]:
    prompt = f"{SYSTEM}\n\n用户：{text}\n类目："
    payload = {
        "prompt": prompt,
        "grammar": GRAMMAR,
        "n_predict": 8,
        "temperature": 0.1,
        "stop": ["\n", "用户："],
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


def parse_label(raw: str) -> str | None:
    m = _LABEL_RE.search(raw)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--corpus", default=str(Path(__file__).parent / "corpus.jsonl"))
    args = ap.parse_args()

    cases = [json.loads(l) for l in Path(args.corpus).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"# 粗分类准确率探针  url=configured AI Gateway  用例={len(cases)}  重复={args.n}\n")

    correct = 0
    total = 0
    bd_correct = 0
    bd_total = 0
    # 混淆矩阵 confusion[真][预测] = 次数
    confusion = defaultdict(lambda: defaultdict(int))
    per_cat_total = defaultdict(int)
    per_cat_correct = defaultdict(int)
    lat = []
    errors = []

    for c in cases:
        gold = c["cat"]
        is_bd = c["id"].startswith("BD")
        for _ in range(args.n):
            try:
                raw, ms = infer(c["input"])
            except Exception as e:
                print(f"[{c['id']}] 请求失败: {e}")
                continue
            lat.append(ms)
            pred = parse_label(raw) or "?"
            ok = pred == gold
            total += 1
            per_cat_total[gold] += 1
            confusion[gold][pred] += 1
            if ok:
                correct += 1
                per_cat_correct[gold] += 1
            if is_bd:
                bd_total += 1
                bd_correct += bool(ok)
            if not ok:
                errors.append((c["id"], gold, pred, c["input"], c.get("note", "")))
            flag = "✓" if ok else "✗"
            print(f"[{flag}] {c['id']:5s} 真={gold:11s} 判={pred:11s} {ms:5.0f}ms  {c['input']}")

    t = total or 1
    print("\n" + "=" * 66)
    print(f"总准确率 P(粗)  : {correct}/{total} = {correct/t:.1%}")
    if bd_total:
        print(f"边界组准确率    : {bd_correct}/{bd_total} = {bd_correct/bd_total:.1%}  <<< 类目边界质量")
    print("\n各类目召回:")
    for cat in CATS:
        ct = per_cat_total[cat]
        if ct:
            print(f"  {cat:12s} {per_cat_correct[cat]}/{ct} = {per_cat_correct[cat]/ct:.0%}")

    print("\n混淆矩阵(行=真实, 列=预测, 只显示有值):")
    header = "真\\预测      " + "".join(f"{c[:7]:>8s}" for c in CATS)
    print(header)
    for gold in CATS:
        if per_cat_total[gold] == 0:
            continue
        row = f"{gold:12s}"
        for pred in CATS:
            v = confusion[gold][pred]
            row += f"{(v or '.'):>8}"
        print(row)

    if errors:
        print(f"\n错分明细({len(errors)}):")
        for cid, gold, pred, inp, note in errors:
            print(f"  {cid} {gold}→{pred}  「{inp}」  {note}")

    if lat:
        lat.sort()
        print(f"\n延迟 中位/p90   : {lat[len(lat)//2]:.0f}ms / {lat[int(len(lat)*0.9)]:.0f}ms")

    p = correct / t
    verdict = "绿灯:推进分层" if p >= 0.95 else ("改类目边界后重测" if p >= 0.85 else "维持扁平,分层不划算")
    print(f"\n判据结论        : P={p:.1%} → {verdict}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())

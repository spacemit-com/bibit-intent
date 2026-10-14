"""
tests/hier_intent/probe_v2.py — 粗分类探针 v2(中文标签 + 锐利判据)

v1 问题诊断:
  - 英文 snake_case 标签(sys_control)对中文输入的 Qwen3 接地困难
  - 描述太泛,类目间判据不够锐利(疑问句全塌进 knowledge)
  - 结果:41.3%,sys_control 0%、escalate 0% 全错

v2 改进:
  - 中文标签(系统控制/系统诊断/应用控制/网络/知识/软件管理/转云端)
  - 每类给**判据关键词**:祈使动作/查状态值/打开关闭/wifi/领域/装卸/超范围
  - 语料不变,只改 prompt 与标签,对照实验
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

# 中文标签(与设计文档 §4 类目一致,corpus 标注不变,映射在输出解析)
LABELS_CN = ["系统控制", "系统诊断", "应用控制", "网络", "知识", "软件管理", "转云端"]
# corpus 的 cat → 中文标签映射
CAT_TO_CN = {
    "sys_control": "系统控制",
    "sys_diag": "系统诊断",
    "app_control": "应用控制",
    "network": "网络",
    "knowledge": "知识",
    "soft_mgmt": "软件管理",
    "escalate": "转云端",
}
CN_TO_CAT = {v: k for k, v in CAT_TO_CN.items()}

# GBNF:只允许中文标签之一
GRAMMAR = 'root ::= "' + '" | "'.join(LABELS_CN) + '"\n'

# v2 prompt:中文标签 + 判据关键词
SYSTEM = (
    "你是意图分类器。把用户这句话归到下面某一类,只输出类目中文名,不解释。\n"
    "系统控制: 调音量/亮度/静音/重启,【祈使动作词:调/设/开/关/重启】\n"
    "系统诊断: 查CPU/内存/温度/磁盘/系统状态,【查状态值:占用多少/剩多少/卡不卡/温度】\n"
    "应用控制: 打开或关闭某个应用(浏览器/微信/终端),【打开/关闭+应用名】\n"
    "网络    : 连接/断开/扫描WiFi无线网络,【wifi/无线/网络+动作】\n"
    "知识    : 进迭/RISC-V/Bianbu等领域问答与教程,【是什么/怎么装+领域词】\n"
    "软件管理: 安装/卸载/更新软件,【装/卸/更新+软件】\n"
    "转云端  : 写作/编程/办公/文件/开放百科/多步任务/日程提醒"
)

_LABEL_RE = re.compile("(" + "|".join(re.escape(l) for l in LABELS_CN) + ")")


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
    """解析中文标签,映射回 corpus 的英文 cat"""
    m = _LABEL_RE.search(raw)
    if not m:
        return None
    return CN_TO_CAT.get(m.group(1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--corpus", default=str(Path(__file__).parent / "corpus.jsonl"))
    args = ap.parse_args()

    cases = [json.loads(l) for l in Path(args.corpus).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"# 粗分类探针 v2(中文标签)  url=configured AI Gateway  用例={len(cases)}  重复={args.n}\n")

    correct = 0
    total = 0
    bd_correct = 0
    bd_total = 0
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
            pred = parse_label(raw)
            ok = pred == gold
            total += 1
            per_cat_total[gold] += 1
            confusion[gold][pred or "?"] += 1
            if ok:
                correct += 1
                per_cat_correct[gold] += 1
            if is_bd:
                bd_total += 1
                bd_correct += bool(ok)
            if not ok:
                errors.append((c["id"], gold, pred or "?", c["input"], c.get("note", "")))
            flag = "✓" if ok else "✗"
            label_cn = CAT_TO_CN.get(pred, pred) if pred else "?"
            print(f"[{flag}] {c['id']:5s} 真={CAT_TO_CN[gold]:4s} 判={label_cn:4s} {ms:5.0f}ms  {c['input']}")

    t = total or 1
    print("\n" + "=" * 66)
    print(f"总准确率 P(粗)  : {correct}/{total} = {correct/t:.1%}  (v1: 41.3%)")
    if bd_total:
        print(f"边界组准确率    : {bd_correct}/{bd_total} = {bd_correct/bd_total:.1%}  (v1: 36.1%)")
    print("\n各类目召回:")
    cats_order = ["sys_control", "sys_diag", "app_control", "network", "knowledge", "soft_mgmt", "escalate"]
    for cat in cats_order:
        ct = per_cat_total[cat]
        if ct:
            v1_recall = {"sys_control":0,"sys_diag":20,"app_control":85,"network":100,"knowledge":100,"soft_mgmt":22,"escalate":0}
            print(f"  {CAT_TO_CN[cat]:4s} {per_cat_correct[cat]:2d}/{ct:2d} = {per_cat_correct[cat]/ct:>4.0%}  (v1: {v1_recall.get(cat,0):>3d}%)")

    print("\n混淆矩阵(行=真实, 列=预测):")
    header = "真\\预测  " + "".join(f"{CAT_TO_CN[c][:2]:>5s}" for c in cats_order)
    print(header)
    for gold in cats_order:
        if per_cat_total[gold] == 0:
            continue
        row = f"{CAT_TO_CN[gold]:4s} "
        for pred_cat in cats_order:
            v = confusion[gold][pred_cat]
            row += f"{(v or '.'):>5}"
        miss = confusion[gold].get("?", 0) or confusion[gold].get(None, 0)
        if miss:
            row += f" ?:{miss}"
        print(row)

    if errors and len(errors) <= 30:
        print(f"\n错分明细({len(errors)}):")
        for cid, gold, pred, inp, note in errors:
            print(f"  {cid} {CAT_TO_CN[gold]}→{CAT_TO_CN.get(pred,pred)}  「{inp}」")

    if lat:
        lat.sort()
        print(f"\n延迟 中位/p90   : {lat[len(lat)//2]:.0f}ms / {lat[int(len(lat)*0.9)]:.0f}ms")

    p = correct / t
    delta = p - 0.413
    verdict = "绿灯:推进分层" if p >= 0.95 else ("改类目边界后重测" if p >= 0.85 else "维持扁平,分层不划算")
    print(f"\n判据结论        : P={p:.1%} (vs v1 {delta:+.1%}) → {verdict}")
    if p < 0.85:
        print("                  若 v2 仍<85%,说明是能力天花板而非标签问题")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())

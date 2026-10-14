"""tests/multi_intent/verb_extractor.py — jieba.posseg 离线动词候选提取。

用途：开发/评测阶段的辅助工具，不进入端侧运行时。用 jieba 词性标注从语料中
抽取动词，并与 multi_action.ACTION_VERBS 对照，输出**未收录**的动词候选，
帮助人工判断是否要扩充动作动词表。

依赖：jieba（可选，未安装时提示安装命令并退出）。
用法：
    .venv/bin/python tests/multi_intent/verb_extractor.py [corpus.jsonl|text...]
      - 缺省读取 tests/multi_intent/multi_action_corpus.jsonl 的 input 字段；
      - 也可直接传入句子，如 `... verb_extractor.py "打开浏览器并播放三国演义"`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

try:
    import jieba.posseg as pseg
except ImportError:
    print("jieba 未安装。安装：pip install jieba（离线辅助，不进运行时）")
    raise SystemExit(2)

from bibit_intent.routing.multi_action import ACTION_VERBS

KNOWN = set(ACTION_VERBS)


def verb_candidates(text: str) -> list[str]:
    """返回文本中 jieba 判定为动词、且不在现有动词表中的词。"""
    found: list[str] = []
    for word, flag in pseg.cut(text):
        if flag.startswith("v") and word.strip() and word not in KNOWN:
            found.append(word)
    return found


def main() -> int:
    args = sys.argv[1:]
    if args:
        sentences = args
    else:
        corpus = Path(__file__).with_name("multi_action_corpus.jsonl")
        sentences = [
            json.loads(line)["input"]
            for line in corpus.read_text(encoding="utf-8").splitlines()
        ]

    seen: list[str] = []
    for text in sentences:
        for verb in verb_candidates(text):
            if verb not in seen:
                seen.append(verb)

    if seen:
        print("未收录动词候选（人工确认后可并入 ACTION_VERBS）：")
        for verb in seen:
            print(f"  {verb}")
    else:
        print("未发现新的动词候选。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

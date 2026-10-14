"""apt 包名近似候选（只读、不安装）回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bibit_intent.routing.pkg_resolver import suggest_install_packages  # noqa: E402


def test_typo_ranked_first():
    corpus = ["vim", "vim-tiny", "vim-common", "bash", "tmux"]
    out = suggest_install_packages("vims", names=corpus)
    assert out and out[0] == "vim"
    assert "bash" not in out
    assert all(cand in corpus for cand in out)


def test_exact_name_excluded():
    out = suggest_install_packages("htop", names=["htop", "htop2", "htop3"])
    assert "htop" not in out
    assert out == ["htop2", "htop3"]


def test_unrelated_names_empty():
    assert suggest_install_packages("nginx", names=["vim", "bash"]) == []


def test_empty_input_or_corpus():
    assert suggest_install_packages("", names=["vim"]) == []
    assert suggest_install_packages("vim", names=[]) == []


def test_limit_respected():
    corpus = ["vim", "vim-tiny", "vim-common", "vim-doc"]
    out = suggest_install_packages("vims", names=corpus, limit=2)
    assert len(out) == 2

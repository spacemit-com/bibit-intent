"""
agent/skill_loader.py — Skill 加载器

扫描 skills/ 目录，把每个 Skill 的工具注册进主 ToolRegistry，
并把其 handlers 并入执行层的 HANDLERS 分发表。

关键设计：解决 512 Token 上下文膨胀（对照 design.md §5.1）
  - Skill 的完整逻辑在 handlers.py（不占 prompt）
  - 只有极简签名（skill.yaml 的 routes.local）进入候选池
  - 运行时由规则引擎 keyword_candidates 粗筛，只注入命中的 skill 签名
  - 1.7B 模型只做「自然语言 → 结构化参数」翻译，复杂解析留给脚本

三层分离：
  ① 极简描述（prompt 内，几个 token）→ wconn(ssid:... password:...)
  ② 1.7B 翻译官 → CALL wconn ssid=home password=12345678
  ③ Skill 脚本（handlers.py）→ 用 nmcli 真正执行，复杂逻辑不占上下文
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from bibit_intent.core.registry import ToolRegistry


@dataclass
class LoadedSkill:
    name: str
    version: str
    display: str
    tool_names: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    missing_deps: list[str] = field(default_factory=list)


def _check_dep(cmd: str) -> bool:
    """检查系统命令是否存在（Skill 依赖检查）。"""
    import shutil
    return shutil.which(cmd) is not None


def load_skills(
    skills_dir: str | Path,
    registry: ToolRegistry,
    handlers: dict[str, Callable],
) -> list[LoadedSkill]:
    """
    扫描 skills_dir，加载所有 Skill。

    每个 Skill 目录需包含：
      - skill.yaml   元数据 + 工具声明
      - handlers.py  执行实现（含 HANDLERS 分发表）

    副作用：
      - 把 skill 工具注册进 registry（source=skill名）
      - 把 skill 的 handlers 并入传入的 handlers 字典

    返回：加载成功的 Skill 列表（含依赖缺失信息）。
    """
    skills_dir = Path(skills_dir)
    loaded: list[LoadedSkill] = []
    if not skills_dir.is_dir():
        return loaded

    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue
        manifest = skill_path / "skill.yaml"
        if not manifest.is_file():
            continue
        try:
            ls = _load_one(skill_path, manifest, registry, handlers)
            if ls:
                loaded.append(ls)
        except Exception as e:
            # 单个 skill 加载失败不影响其他 skill
            print(f"[skill] 加载失败 {skill_path.name}: {e}", file=sys.stderr)

    return loaded


def _load_one(
    skill_path: Path,
    manifest: Path,
    registry: ToolRegistry,
    handlers: dict[str, Callable],
) -> LoadedSkill | None:
    """加载单个 Skill。"""
    meta = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    name = meta.get("name") or skill_path.name

    # 1. 依赖检查（缺失也加载，只标记，执行时才报错）
    requires = meta.get("requires", []) or []
    missing = [d for d in requires if not _check_dep(d)]

    # 2. 动态导入 handlers.py，取其 HANDLERS 分发表
    handler_file = skill_path / "handlers.py"
    skill_handlers: dict[str, Callable] = {}
    if handler_file.is_file():
        mod_name = f"_skill_{name}"
        spec = importlib.util.spec_from_file_location(mod_name, handler_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        skill_handlers = getattr(module, "HANDLERS", {})

    # 3. 注册工具到 registry + 合并 handlers
    tool_names: list[str] = []
    for tool_name, tool_data in (meta.get("tools") or {}).items():
        # skill.yaml 的工具字段映射到 ToolDef 需要的结构
        data = {
            "display": tool_data.get("display", tool_name),
            "display_en": tool_data.get("display_en"),
            "description_en": tool_data.get("description_en"),
            "short": tool_data.get("short", tool_name),
            "aliases": tool_data.get("aliases", []),
            "covers": tool_data.get("covers", []),
            "params": tool_data.get("params", {}),
            "routes": tool_data.get("routes", {}),
            "system": {"type": "skill", "skill": name,
                       "handler": tool_data.get("handler", tool_name),
                       "confirm": bool(tool_data.get("confirm", False)),
                       "mcp": (dict(tool_data.get("mcp"))
                               if isinstance(tool_data.get("mcp"), dict) else {})},
        }
        registry.register_tool(tool_name, data, source=name)
        tool_names.append(tool_name)

        # 合并 handler（skill.yaml 的 handler 字段指定函数名）
        fn_name = tool_data.get("handler", tool_name)
        if fn_name in skill_handlers:
            handlers[tool_name] = skill_handlers[fn_name]

    return LoadedSkill(
        name=name,
        version=str(meta.get("version", "?")),
        display=meta.get("display", name),
        tool_names=tool_names,
        requires=requires,
        missing_deps=missing,
    )

"""Print an offline inventory of active rule patterns and lexical risk indicators."""

import ast
import json
from pathlib import Path
import re

import yaml

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path


def source_locations():
    resources = Path(resource_path("skills")).parent
    locations = {}
    paths = [resources / "tools.yaml", *sorted((resources / "skills").glob("*/skill.yaml"))]
    for path in paths:
        document = yaml.compose(path.read_text(encoding="utf-8"))
        for key, value in document.value:
            if key.value == "tools":
                for name, definition in value.value:
                    locations[name.value] = {
                        "path": "src/bibit_intent/resources/" + str(path.relative_to(resources)),
                        "line": definition.start_mark.line + 1,
                    }
    return locations


def inventory():
    registry = ToolRegistry()
    load_skills(resource_path("skills"), registry, {})
    locations = source_locations()
    tools = []
    for tool in sorted(registry.all(), key=lambda item: item.name):
        config = (tool.routes or {}).get("rule") or {}
        patterns = []
        for compiled in tool.rule_patterns():
            pattern = compiled.pattern
            flags = []
            if not (pattern.startswith("^") and pattern.endswith("$")):
                flags.append("not_textually_anchored_at_both_ends")
            if re.search(r"(?<!\\)\.(?:[*+]|\{)", pattern):
                flags.append("wildcard_span")
            patterns.append({"pattern": pattern, "review_flags": flags})
        keywords = tool.rule_keywords()
        tools.append({
            "tool": tool.name,
            "source": locations.get(tool.name),
            "confirm": tool.system.get("confirm"),
            "patterns": patterns,
            "keywords": keywords,
            "short_keywords": [keyword for keyword in keywords if len(keyword) <= 2],
            "slot_map": config.get("slot_map", {}),
        })
    patterns = [pattern for tool in tools for pattern in tool["patterns"]]
    source_root = Path(__file__).resolve().parents[1]
    python_regexes = []
    for directory in ("routing", "analysis", "core"):
        for path in sorted((source_root / "src/bibit_intent" / directory).glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "re"
                    and node.func.attr in {"compile", "search", "match", "fullmatch"}
                ):
                    python_regexes.append({
                        "path": str(path.relative_to(source_root)),
                        "line": node.lineno,
                        "expression": ast.get_source_segment(source, node),
                    })
    return {
        "scope": "Active merged ToolRegistry plus direct re calls in routing/analysis/core; lexical flags are review hints, not proven defects",
        "summary": {
            "tools": len(tools),
            "patterns": len(patterns),
            "not_textually_anchored_at_both_ends": sum(
                "not_textually_anchored_at_both_ends" in pattern["review_flags"] for pattern in patterns
            ),
            "wildcard_span": sum("wildcard_span" in pattern["review_flags"] for pattern in patterns),
            "tools_with_short_keywords": sum(bool(tool["short_keywords"]) for tool in tools),
            "tools_with_slot_map": sum(bool(tool["slot_map"]) for tool in tools),
            "python_regex_calls": len(python_regexes),
        },
        "tools": tools,
        "python_regexes": python_regexes,
    }


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))

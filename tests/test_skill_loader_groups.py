import sys
import uuid

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills


def _write_skill(skills_dir, name, tools, handler_source):
    skill_dir = skills_dir / name
    skill_dir.mkdir()
    tool_lines = []
    for tool_name, group, confirm in tools:
        tool_lines.extend(
            [
                f"  {tool_name}:",
                f"    display: {tool_name}",
                f"    short: {tool_name}",
                "    params: {}",
                "    routes: {}",
                f"    handler: {tool_name}_handler",
            ]
        )
        if group:
            tool_lines.extend(["    mcp:", f"      group: {group}"])
        if confirm:
            tool_lines.append("    confirm: true")
    manifest = "\n".join(
        [f"name: {name}", "tools:", *tool_lines, ""]
    )
    (skill_dir / "skill.yaml").write_text(manifest, encoding="utf-8")
    (skill_dir / "handlers.py").write_text(handler_source, encoding="utf-8")


def test_group_filtered_loading_keeps_schemas_without_first_call_import(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    selected_name = f"selected_{uuid.uuid4().hex}"
    excluded_name = f"excluded_{uuid.uuid4().hex}"
    _write_skill(
        skills_dir,
        selected_name,
        [
            ("readonly_tool", "readonly", False),
            ("admin_tool", "admin", True),
        ],
        """
def readonly_tool_handler(**kwargs):
    return "ready"

def admin_tool_handler(**kwargs):
    return "admin"

HANDLERS = {
    "readonly_tool_handler": readonly_tool_handler,
    "admin_tool_handler": admin_tool_handler,
}
""",
    )
    _write_skill(
        skills_dir,
        excluded_name,
        [("excluded_admin_tool", None, True)],
        'raise RuntimeError("excluded group handler must not import")\n',
    )

    registry = ToolRegistry()
    handlers = {}
    try:
        loaded = load_skills(
            skills_dir,
            registry,
            handlers,
            groups={"READONLY"},
        )

        assert {skill.name for skill in loaded} == {selected_name, excluded_name}
        assert {tool.name for tool in registry.all()} >= {
            "readonly_tool",
            "admin_tool",
            "excluded_admin_tool",
        }
        assert set(handlers) == {"readonly_tool"}
        assert handlers["readonly_tool"]() == "ready"
        assert f"_skill_{selected_name}" in sys.modules
        assert f"_skill_{excluded_name}" not in sys.modules
    finally:
        sys.modules.pop(f"_skill_{selected_name}", None)
        sys.modules.pop(f"_skill_{excluded_name}", None)

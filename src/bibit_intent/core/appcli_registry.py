"""core/appcli_registry.py — CLI-Anything appcli 注册表

一份只读资源声明「哪些 appcli、哪些动作允许被调用」以及「产出物要满足什么」。
设计见 docs/design/cli_anything_integration.md §5.1、任务 H01。

边界（刻意收紧，避免把不可控输入带进执行层）：

- 只登记**动作**，不接收自由 argv：动作的 argv 是模板，占位符必须在该动作的
  ``params`` 里登记，未登记的参数一律拒绝；
- 列表型参数（``type: list``，如 ``writer add-list`` 的 ``-i``）只允许写成
  ``"{name...}"``，且**前一个词必须是以 ``-`` 开头的字面开关**——渲染出来是
  「开关 + 值」重复若干次，仍然产生不了任何自由 argv；
- 值只能是短文本（无换行/NUL），且声明了 ``enum`` 的参数必须命中枚举；
- ``entry_point`` 只接受裸命令名，不允许路径分隔符；
- ``riscv64: verified`` 是唯一白名单判据，其余状态不允许进入执行层。

任何加载/校验失败都抛 :class:`AppCliRegistryError`（带稳定的 ``code``），
调用方据此给出明确错误，而不是让进程崩在 YAML 解析上。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bibit_intent.resources import resource_path

__all__ = [
    "CHANNELS",
    "DEFAULT_APPCLI_TIMEOUT_SECONDS",
    "MAX_APPCLI_TIMEOUT_SECONDS",
    "RISCV64_STATES",
    "VERIFY_RULES",
    "AppCliAction",
    "AppCliActionParam",
    "AppCliArtifact",
    "AppCliRegistry",
    "AppCliRegistryError",
    "AppCliRequires",
    "AppCliSpec",
    "load_appcli_registry",
]

# ── 受控词表 ─────────────────────────────────────────────────────────

#: 执行通道。host 需要宿主桌面/命名空间；artifact-only 只读写工作区。
CHANNELS = ("host", "artifact-only")

#: riscv64 支持状态。只有 verified 进白名单（H16 设备验收后置位）。
RISCV64_STATES = ("verified", "unverified", "unsupported")

#: 动作默认超时（秒）。渲染类动作（LibreOffice/GIMP 导出）远慢于普通命令，
#: 所以默认值放在注册表里，允许按 appcli 覆盖；上下界防止把手滑写成 0 或一天。
DEFAULT_APPCLI_TIMEOUT_SECONDS = 300
MAX_APPCLI_TIMEOUT_SECONDS = 1800

#: 产出物校验规则名。实现见 H04（appcli_verify）。
VERIFY_RULES = (
    "exists",          # 存在且是普通文件
    "size>0",          # 非空
    "pdf_magic",       # %PDF-
    "odf_zip_magic",   # ZIP 且含 mimetype（ODF）
    "office_zip_magic",# ZIP（ODF 或 OOXML）
    "odt_magic",       # ZIP + mimetype = ODF 文字
    "ods_magic",       # ZIP + mimetype = ODF 表格
    "docx_magic",      # ZIP + [Content_Types].xml + word/document.xml
    "xlsx_magic",      # ZIP + [Content_Types].xml + xl/workbook.xml
    "pptx_magic",      # ZIP + [Content_Types].xml + ppt/presentation.xml
    "svg_magic",       # <svg / <?xml
    "png_magic",       # \x89PNG
    "jpeg_magic",      # \xff\xd8\xff
    "mp4_magic",       # ftyp box
    "xml_wellformed",  # 可被 XML 解析
)

_ENTRY_POINT_FORBIDDEN = ("/", "\\", " ", "\n", "\0")

#: 参数类型。``list`` 只用于 click 的 ``multiple=True`` 开关（见 ``{name...}``）。
PARAM_TYPES = ("str", "int", "float", "bool", "list")

#: 列表型参数的项数上限——防止把一次调用塞成批处理。
MAX_LIST_ITEMS = 20

#: 列表型参数的分隔符（字符串形式时用），值里允许中文逗号/空白。
LIST_SEPARATOR = "|"
#: 参数值里不允许出现的字符：它们会破坏 argv 的“单值”假设。
_VALUE_FORBIDDEN = ("\n", "\0")


class AppCliRegistryError(ValueError):
    """注册表加载或使用失败。``code`` 供工具层映射成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        return f"[{self.code}] {super().__str__()}"


# ── 数据模型 ─────────────────────────────────────────────────────────

class _StrictModel(BaseModel):
    """未知字段即错误：把 YAML 里的拼写错误挡在加载期。"""

    model_config = ConfigDict(extra="forbid")


class AppCliActionParam(_StrictModel):
    type: str = "str"
    enum: list[str] | None = None
    default: Any = None
    unit: str | None = None
    #: 该参数是文件系统路径：执行前必须解析到 appcli 工作区内（见
    #: core/appcli_execution.py），越界拒绝；执行时替换成绝对路径。
    path: bool = False

    @property
    def required(self) -> bool:
        # 显式写了 default（哪怕是 null）就视为可选。
        return "default" not in self.model_fields_set


class AppCliArtifact(_StrictModel):
    """一条产物校验规则：校验**哪个参数**指向的文件、按哪些规则校验。

    必须显式给出 ``param``：不写清楚校验对象，规则就无法执行（H04 之前这里是
    appcli 级、且没有指向，只能当声明用）。``param`` 必须是本动作里标了
    ``path: true`` 的参数。
    """

    param: str
    verify: list[str] = Field(default_factory=list)


class AppCliAction(_StrictModel):
    name: str
    argv: list[str]
    description: str = ""
    params: dict[str, AppCliActionParam] = Field(default_factory=dict)
    readonly: bool = False
    artifacts: list[AppCliArtifact] = Field(default_factory=list)


class AppCliRequires(_StrictModel):
    apt: list[str] = Field(default_factory=list)
    python: list[str] = Field(default_factory=list)


class AppCliSpec(_StrictModel):
    name: str
    display: str
    entry_point: str
    channel: str
    riscv64: str
    #: 该 appcli 动作的默认超时；调用方显式给 timeout 时以调用方为准。
    timeout_seconds: int = DEFAULT_APPCLI_TIMEOUT_SECONDS
    category: str = ""
    notes: str = ""
    requires: AppCliRequires = Field(default_factory=AppCliRequires)
    #: 执行该 appcli 时叠加到进程环境上的变量（在继承的环境之上，同名覆盖）。
    #: 典型用途：headless 设备上没有 DISPLAY，LibreOffice 会去初始化 Qt xcb 并以
    #: exit 134 失败、一个产物都不写；`SAL_USE_VCLPLUGIN: svp` 让它走无头 VCL。
    #: 只允许字符串键值，不接受空键 —— 注册表是系统资源，这里不做 shell 展开。
    env: dict[str, str] = Field(default_factory=dict)
    actions: list[AppCliAction] = Field(default_factory=list)

    @field_validator("env")
    @classmethod
    def _check_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("env 的键必须是非空字符串")
            if any(char in key for char in ("=", "\n", "\0")):
                raise ValueError(f"env 的键不能包含 '='/换行/NUL：{key!r}")
            if not isinstance(item, str) or any(char in item for char in ("\n", "\0")):
                raise ValueError(f"env[{key}] 必须是单行字符串")
        return value

    @property
    def verified(self) -> bool:
        """是否已通过设备验收——进入执行层的唯一判据。"""
        return self.riscv64 == "verified"

    def action(self, name: str) -> AppCliAction:
        for candidate in self.actions:
            if candidate.name == name:
                return candidate
        known = ", ".join(item.name for item in self.actions) or "(无)"
        raise AppCliRegistryError(
            "unknown_action",
            f"应用 {self.name!r} 没有动作 {name!r}；已登记动作：{known}",
        )

    def artifact_rules(self, action: str) -> tuple[AppCliArtifact, ...]:
        """该动作声明的产物校验规则（可能为空：只读动作不产出文件）。"""
        return tuple(self.action(action).artifacts)

    def resolve_values(self, action: str, args: dict[str, Any] | None = None) -> dict[str, str]:
        """补默认值并校验参数，返回「参数名 → 最终字符串值」。

        未登记的参数、缺失的必填参数、枚举越界都会抛 ``invalid_args``。
        调用方拿到的是**实际会被拼进命令行的值**，因此可以在渲染前替换路径
        （见 ``core/appcli_execution.resolve_in_workspace``），产物校验也据此定位文件。
        """
        spec = self.action(action)
        supplied = dict(args or {})
        unknown = sorted(set(supplied) - set(spec.params))
        if unknown:
            known = ", ".join(sorted(spec.params)) or "(无)"
            raise AppCliRegistryError(
                "invalid_args",
                f"动作 {self.name}/{action} 不接受参数 {', '.join(unknown)}；可用参数：{known}",
            )

        values: dict[str, Any] = {}
        for key, param in spec.params.items():
            if key in supplied:
                raw = supplied[key]
            elif param.required:
                raise AppCliRegistryError(
                    "invalid_args",
                    f"动作 {self.name}/{action} 缺少必填参数 {key!r}",
                )
            else:
                raw = param.default
            values[key] = _render_value(self.name, action, key, raw, param)
        return values

    def render_argv(self, action: str, args: dict[str, Any] | None = None) -> list[str]:
        """按动作模板渲染 argv（不含 entry_point）。

        ``args`` 可以是尚未补默认值的原始参数（会先走 ``resolve_values``）。
        """
        spec = self.action(action)
        values = self.resolve_values(action, args)
        rendered: list[str] = []
        for token in spec.argv:
            repeated = _repeat_placeholder(token)
            if repeated is not None:
                items = values[repeated]
                flag = rendered.pop()
                for item in items:
                    rendered.extend([flag, item])
                continue
            placeholders = _placeholders(token)
            if not placeholders:
                rendered.append(token)
                continue
            if len(placeholders) > 1 or token.strip() != token:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"动作 {self.name}/{action} 的 argv 片段 {token!r} 不是纯占位符",
                )
            rendered.append(values[placeholders.pop()])
        return rendered

    def build_command(self, action: str, args: dict[str, Any] | None = None) -> list[str]:
        """完整命令行 = entry_point + 渲染后的 argv（``--json`` 由调用方插入）。"""
        return [self.entry_point, *self.render_argv(action, args)]


# ── 值渲染与校验 ─────────────────────────────────────────────────────

def _repeat_placeholder(token: str) -> str | None:
    """``"{items...}"`` → ``"items"``；其他形态返回 None。"""
    if not (token.startswith("{") and token.endswith("...}")) or len(token) < 6:
        return None
    inner = token[1:-4]
    return inner if inner.isidentifier() else None


def _placeholders(token: str) -> set[str]:
    if not (token.startswith("{") and token.endswith("}")) or len(token) < 3:
        return set()
    inner = token[1:-1]
    if not inner.isidentifier():
        return set()
    return {inner}


def _render_list_value(
    appcli: str, action: str, key: str, raw: Any, param: AppCliActionParam
) -> list[str]:
    """列表型参数：接受 list/tuple，或 ``a|b|c`` / JSON 数组字符串。"""
    if raw is None:
        raise AppCliRegistryError(
            "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 取值为 null"
        )
    if isinstance(raw, (list, tuple)):
        items = [item for item in raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except ValueError as exc:
                raise AppCliRegistryError(
                    "invalid_args",
                    f"动作 {appcli}/{action} 的参数 {key!r} 不是合法 JSON 数组：{exc}",
                ) from exc
            if not isinstance(decoded, list):
                raise AppCliRegistryError(
                    "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 必须是数组"
                )
            items = list(decoded)
        else:
            items = [part.strip() for part in text.split(LIST_SEPARATOR)]
    else:
        raise AppCliRegistryError(
            "invalid_args",
            f"动作 {appcli}/{action} 的参数 {key!r} 只能是数组或分隔字符串",
        )
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            raise AppCliRegistryError(
                "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 的项必须是字符串"
            )
        text = str(item).strip()
        if not text:
            raise AppCliRegistryError(
                "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 的项不能为空"
            )
        if any(bad in text for bad in _VALUE_FORBIDDEN):
            raise AppCliRegistryError(
                "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 的项含换行或 NUL"
            )
        cleaned.append(text)
    if not cleaned:
        raise AppCliRegistryError(
            "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 至少要有一项"
        )
    if len(cleaned) > MAX_LIST_ITEMS:
        raise AppCliRegistryError(
            "invalid_args",
            f"动作 {appcli}/{action} 的参数 {key!r} 最多 {MAX_LIST_ITEMS} 项，"
            f"收到 {len(cleaned)} 项",
        )
    if param.enum is not None:
        allowed = [str(item) for item in param.enum]
        beyond = [item for item in cleaned if item not in allowed]
        if beyond:
            raise AppCliRegistryError(
                "invalid_args",
                f"动作 {appcli}/{action} 的参数 {key!r} 的项必须是 "
                f"{', '.join(allowed)} 之一：{', '.join(beyond)}",
            )
    return cleaned


def _render_value(
    appcli: str, action: str, key: str, raw: Any, param: AppCliActionParam
) -> Any:
    if param.type == "list":
        return _render_list_value(appcli, action, key, raw, param)
    if raw is None:
        raise AppCliRegistryError(
            "invalid_args",
            f"动作 {appcli}/{action} 的参数 {key!r} 取值为 null",
        )
    if isinstance(raw, bool):
        # bool 是 int 的子类，先拦下来，避免 True 变成 "True" 这种歧义值。
        value = "true" if raw else "false"
    elif isinstance(raw, (int, float)):
        value = str(raw)
    elif isinstance(raw, str):
        value = raw.strip()
    else:
        raise AppCliRegistryError(
            "invalid_args",
            f"动作 {appcli}/{action} 的参数 {key!r} 只能是字符串或数字",
        )
    if not value:
        raise AppCliRegistryError(
            "invalid_args", f"动作 {appcli}/{action} 的参数 {key!r} 不能为空"
        )
    if any(bad in value for bad in _VALUE_FORBIDDEN):
        raise AppCliRegistryError(
            "invalid_args",
            f"动作 {appcli}/{action} 的参数 {key!r} 含换行或 NUL",
        )
    if param.enum is not None:
        allowed = [str(item) for item in param.enum]
        if value not in allowed:
            raise AppCliRegistryError(
                "invalid_args",
                f"动作 {appcli}/{action} 的参数 {key!r} 必须是 {', '.join(allowed)} 之一",
            )
    return value


def _validate_appcli(spec: AppCliSpec) -> None:
    where = f"应用 {spec.name!r}"

    if not spec.name.strip():
        raise AppCliRegistryError("registry_invalid", "应用名字不能为空")
    if not spec.entry_point or any(
        bad in spec.entry_point for bad in _ENTRY_POINT_FORBIDDEN
    ):
        raise AppCliRegistryError(
            "registry_invalid",
            f"{where} 的 entry_point {spec.entry_point!r} 必须是裸命令名",
        )
    if spec.channel not in CHANNELS:
        raise AppCliRegistryError(
            "registry_invalid",
            f"{where} 的 channel {spec.channel!r} 不在 {CHANNELS}",
        )
    if spec.riscv64 not in RISCV64_STATES:
        raise AppCliRegistryError(
            "registry_invalid",
            f"{where} 的 riscv64 {spec.riscv64!r} 不在 {RISCV64_STATES}",
        )
    if not 1 <= int(spec.timeout_seconds) <= MAX_APPCLI_TIMEOUT_SECONDS:
        raise AppCliRegistryError(
            "registry_invalid",
            f"{where} 的 timeout_seconds {spec.timeout_seconds!r} 不在 "
            f"1–{MAX_APPCLI_TIMEOUT_SECONDS}",
        )
    if not spec.actions:
        raise AppCliRegistryError("registry_invalid", f"{where} 没有登记任何动作")

    seen: set[str] = set()
    for action in spec.actions:
        if not action.name.strip():
            raise AppCliRegistryError("registry_invalid", f"{where} 存在无名动作")
        if action.name in seen:
            raise AppCliRegistryError(
                "registry_invalid", f"{where} 的动作名 {action.name!r} 重复"
            )
        seen.add(action.name)
        if not action.argv:
            raise AppCliRegistryError(
                "registry_invalid", f"{where} 的动作 {action.name!r} 的 argv 为空"
            )

        for key, param in action.params.items():
            if param.type not in PARAM_TYPES:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的参数 {key!r} 的 type={param.type!r} "
                    f"不在 {PARAM_TYPES}",
                )
            if param.type == "list" and param.path:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的参数 {key!r} 不能同时是 list 与 path",
                )

        used: set[str] = set()
        repeated: set[str] = set()
        for index, token in enumerate(action.argv):
            if not isinstance(token, str):
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的 argv 只能由字符串组成",
                )
            name = _repeat_placeholder(token)
            if name is not None:
                target = action.params.get(name)
                if target is None or target.type != "list":
                    raise AppCliRegistryError(
                        "registry_invalid",
                        f"{where}/{action.name} 的 {token!r} 指向的不是列表参数",
                    )
                if index == 0 or action.argv[index - 1].startswith("{"):
                    raise AppCliRegistryError(
                        "registry_invalid",
                        f"{where}/{action.name} 的 {token!r} 前一个词必须是字面开关",
                    )
                if not action.argv[index - 1].startswith("-"):
                    raise AppCliRegistryError(
                        "registry_invalid",
                        f"{where}/{action.name} 的 {token!r} 前一个词 "
                        f"{action.argv[index - 1]!r} 不是开关",
                    )
                if name in repeated:
                    raise AppCliRegistryError(
                        "registry_invalid",
                        f"{where}/{action.name} 的列表参数 {name!r} 出现了多次",
                    )
                repeated.add(name)
                used.add(name)
                continue
            placeholders = _placeholders(token)
            if token.startswith("{") and not placeholders:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的占位符 {token!r} 不是合法名字",
                )
            used |= placeholders
        undeclared = sorted(used - set(action.params))
        if undeclared:
            raise AppCliRegistryError(
                "registry_invalid",
                f"{where}/{action.name} 的 argv 用了未登记的参数：{', '.join(undeclared)}",
            )
        used_lists = {key for key, param in action.params.items() if param.type == "list"}
        if used_lists - repeated:
            raise AppCliRegistryError(
                "registry_invalid",
                f"{where}/{action.name} 的列表参数没写成 '{{name...}}'："
                f"{', '.join(sorted(used_lists - repeated))}",
            )
        missing = sorted(
            key for key, param in action.params.items() if param.required and key not in used
        )
        if missing:
            raise AppCliRegistryError(
                "registry_invalid",
                f"{where}/{action.name} 的必填参数没出现在 argv 里：{', '.join(missing)}",
            )
        for artifact in action.artifacts:
            target = action.params.get(artifact.param)
            if target is None:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的产物规则指向未登记的参数 {artifact.param!r}",
                )
            if not target.path:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的产物参数 {artifact.param!r} 必须标 path: true",
                )
            if not artifact.verify:
                raise AppCliRegistryError(
                    "registry_invalid",
                    f"{where}/{action.name} 的产物规则 {artifact.param!r} 没有校验规则",
                )
            for rule in artifact.verify:
                if rule not in VERIFY_RULES:
                    raise AppCliRegistryError(
                        "registry_invalid",
                        f"{where}/{action.name} 的校验规则 {rule!r} 未登记",
                    )


# ── 注册表 ───────────────────────────────────────────────────────────

class AppCliRegistry:
    """加载并校验应用声明；只读视图，不在初始化时执行任何命令。"""

    def __init__(self, specs: list[AppCliSpec], *, source: Path | None = None) -> None:
        self._specs: dict[str, AppCliSpec] = {}
        self.source = source
        for spec in specs:
            _validate_appcli(spec)
            if spec.name in self._specs:
                raise AppCliRegistryError(
                    "registry_invalid", f"应用名字重复：{spec.name!r}"
                )
            self._specs[spec.name] = spec

    # ── 加载 ─────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppCliRegistry":
        """从资源文件加载；缺文件/坏 YAML/字段非法都抛 AppCliRegistryError。"""
        target = Path(path) if path is not None else resource_path("appcli_registry.yaml")
        try:
            raw_text = target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AppCliRegistryError(
                "registry_unavailable", f"应用注册表不存在：{target}"
            ) from exc
        except OSError as exc:
            raise AppCliRegistryError(
                "registry_unavailable", f"应用注册表不可读：{target}（{exc.strerror}）"
            ) from exc

        try:
            document = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise AppCliRegistryError(
                "registry_invalid", f"应用注册表不是合法 YAML：{target}（{exc}）"
            ) from exc

        if not isinstance(document, dict):
            raise AppCliRegistryError(
                "registry_invalid", f"应用注册表顶层必须是映射：{target}"
            )
        version = document.get("version")
        if version != 1:
            raise AppCliRegistryError(
                "registry_invalid",
                f"不支持的应用注册表版本 {version!r}（当前支持 1）",
            )
        entries = document.get("appclies")
        if not isinstance(entries, list) or not entries:
            raise AppCliRegistryError(
                "registry_invalid", "应用注册表必须包含非空的 appclies 列表"
            )

        try:
            specs = [AppCliSpec(**entry) for entry in entries]
        except ValidationError as exc:
            raise AppCliRegistryError(
                "registry_invalid", f"应用声明字段非法：{exc}"
            ) from exc
        return cls(specs, source=target)

    # ── 查询 ─────────────────────────────────────────────────────────
    def get(self, name: str) -> AppCliSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> AppCliSpec:
        spec = self._specs.get(name)
        if spec is None:
            known = ", ".join(sorted(self._specs)) or "(空)"
            raise AppCliRegistryError(
                "unknown_appcli", f"未登记的应用 {name!r}；已登记：{known}"
            )
        return spec

    def require_verified(self, name: str) -> AppCliSpec:
        """执行层入口：未通过设备验收的应用一律拒绝。"""
        spec = self.require(name)
        if not spec.verified:
            raise AppCliRegistryError(
                "appcli_unverified",
                f"应用 {name!r} 在 riscv64 上尚未验收（riscv64={spec.riscv64}）",
            )
        return spec

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[AppCliSpec, ...]:
        return tuple(self._specs[name] for name in self.names())

    def verified(self) -> tuple[AppCliSpec, ...]:
        return tuple(spec for spec in self.specs() if spec.verified)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self):
        return iter(self.specs())


def load_appcli_registry(path: str | Path | None = None) -> AppCliRegistry:
    """``AppCliRegistry.load`` 的函数式入口。"""
    return AppCliRegistry.load(path)

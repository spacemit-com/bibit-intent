"""依赖需求扫描与本机满足度判定（全部只读）。

开发者的代码跑不起来时，Agent 需要先回答两个问题：
  1. 代码要什么依赖（Python import / C 头文件 / pkg-config / 动态库 / 外部命令 / Node 包）；
  2. 本机已经满足了哪些，还缺哪些。

本模块只做「读文件 + 只读命令」：`ast` 解析源码、`pkg-config --exists`、
`ldconfig -p`、`shutil.which`，以及在一个独立子进程里做 Python import / `ctypes.CDLL`
探测。不安装任何东西，也不在 Agent 进程内 import 目标项目的代码（避免副作用）：
动态库探测同样放在子进程里，避免把项目依赖的 `so` 加载进 Agent 进程。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bibit_intent.execution.bounded_command import OutputLimitExceeded, run_bounded_command

KIND_PYTHON = "python"
KIND_C_HEADER = "c_header"
KIND_PKGCONFIG = "pkgconfig"
KIND_COMMAND = "command"
KIND_NODE = "node"
KIND_SHARED_LIB = "shared_lib"

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "dist", "build", "target", ".tox", ".eggs",
    "site-packages", ".idea", ".vscode",
})
_MAX_FILES = 4000
_MAX_FILE_BYTES = 2_000_000
_MAX_EVIDENCE = 3
_PY_SUFFIXES = (".py",)
_C_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx")

# pip_install 使用的用户共享 venv（PEP 668 下不能装系统 Python）
SHARED_VENV_PYTHON = Path.home() / ".local" / "share" / "bibit-intent" / "pyvenv" / "bin" / "python"

# 语言/系统自带头文件：永远不会缺，扫进来只会污染依赖清单
_SYSTEM_HEADERS = frozenset({
    "stdio.h", "stdlib.h", "string.h", "strings.h", "math.h", "time.h", "unistd.h",
    "fcntl.h", "errno.h", "stdint.h", "stdbool.h", "stddef.h", "stdarg.h", "ctype.h",
    "assert.h", "signal.h", "pthread.h", "dirent.h", "limits.h", "float.h", "locale.h",
    "setjmp.h", "wchar.h", "wctype.h", "inttypes.h", "complex.h", "iso646.h", "tgmath.h",
    "malloc.h", "memory.h", "sys/types.h", "sys/stat.h", "sys/time.h", "sys/wait.h",
    "sys/socket.h", "sys/un.h", "sys/mman.h", "sys/ioctl.h", "sys/utsname.h",
    "sys/resource.h", "sys/select.h", "sys/epoll.h", "sys/syscall.h", "sys/inotify.h",
    "netinet/in.h", "netinet/tcp.h", "arpa/inet.h", "netdb.h", "linux/limits.h",
})

# 模块名 → PyPI 分发名（不一致的少数情况）
_MODULE_TO_DIST = {
    "yaml": "PyYAML", "PIL": "Pillow", "cv2": "opencv-python", "sklearn": "scikit-learn",
    "skimage": "scikit-image", "bs4": "beautifulsoup4", "dateutil": "python-dateutil",
    "dotenv": "python-dotenv", "serial": "pyserial", "jwt": "PyJWT",
    "Crypto": "pycryptodome", "OpenSSL": "pyOpenSSL", "gi": "PyGObject",
    "Xlib": "python-xlib", "magic": "python-magic", "usb": "pyusb",
    "attr": "attrs", "pkg_resources": "setuptools", "mpl_toolkits": "matplotlib",
    "OpenGL": "PyOpenGL", "win32com": "pywin32", "pydantic_core": "pydantic-core",
}

# 头文件 → 候选 apt 包（按顺序取第一个可解析到的）
_HEADER_TO_APT = {
    "openssl/ssl.h": ("libssl-dev",),
    "openssl/evp.h": ("libssl-dev",),
    "openssl/crypto.h": ("libssl-dev",),
    "zlib.h": ("zlib1g-dev",),
    "sqlite3.h": ("libsqlite3-dev",),
    "curl/curl.h": ("libcurl4-openssl-dev",),
    "png.h": ("libpng-dev",),
    "jpeglib.h": ("libjpeg-dev", "libjpeg-turbo8-dev"),
    "libusb-1.0/libusb.h": ("libusb-1.0-0-dev",),
    "alsa/asoundlib.h": ("libasound2-dev",),
    "ffi.h": ("libffi-dev",),
    "readline/readline.h": ("libreadline-dev",),
    "ncurses.h": ("libncurses-dev",),
    "bzlib.h": ("libbz2-dev",),
    "lzma.h": ("liblzma-dev",),
    "yaml.h": ("libyaml-dev",),
    "uuid/uuid.h": ("uuid-dev",),
    "portaudio.h": ("portaudio19-dev",),
    "avcodec.h": ("libavcodec-dev",),
    "gmp.h": ("libgmp-dev",),
    "expat.h": ("libexpat1-dev",),
    "tiff.h": ("libtiff-dev",),
    "webp/decode.h": ("libwebp-dev",),
    "dbus-1.0/dbus/dbus.h": ("libdbus-1-dev",),
    "bluetooth/bluetooth.h": ("libbluetooth-dev",),
}


def header_apt_candidates(header: str) -> list[str]:
    """头文件 → apt 候选包名（未收录时按命名约定猜测，由调用方校验是否真实存在）。"""
    table = _HEADER_TO_APT.get(header)
    if table:
        return list(table)
    return [f"lib{Path(header).stem}-dev", f"{Path(header).stem}-dev"]


# soname → 提供它的发行版包（Ubuntu/Bianbu 上常见的那几个；未收录时按命名
# 约定猜「libxxxN」，再由 apt 干跑校验是否真实存在）。
_SONAME_TO_APT = {
    "libssl.so.3": ("libssl3t64", "libssl3"),
    "libssl.so.1.1": ("libssl1.1",),
    "libcrypto.so.3": ("libssl3t64", "libssl3"),
    "libz.so.1": ("zlib1g",),
    "libsqlite3.so.0": ("libsqlite3-0",),
    "libcurl.so.4": ("libcurl4t64", "libcurl4"),
    "libpng16.so.16": ("libpng16-16t64", "libpng16-16"),
    "libjpeg.so.8": ("libjpeg-turbo8",),
    "libusb-1.0.so.0": ("libusb-1.0-0",),
    "libasound.so.2": ("libasound2t64", "libasound2"),
    "libffi.so.8": ("libffi8",),
    "libreadline.so.8": ("libreadline8t64", "libreadline8"),
    "libncursesw.so.6": ("libncursesw6",),
    "libbz2.so.1.0": ("libbz2-1.0",),
    "liblzma.so.5": ("liblzma5",),
    "libyaml-0.so.2": ("libyaml-0-2",),
    "libuuid.so.1": ("libuuid1",),
    "libgmp.so.10": ("libgmp10",),
    "libexpat.so.1": ("libexpat1",),
    "libtiff.so.6": ("libtiff6",),
    "libwebp.so.7": ("libwebp7",),
    "libdbus-1.so.3": ("libdbus-1-3",),
    "libbluetooth.so.3": ("libbluetooth3",),
    "libstdc++.so.6": ("libstdc++6",),
    "libgcc_s.so.1": ("libgcc-s1",),
    "libGL.so.1": ("libgl1",),
    "libopenblas.so.0": ("libopenblas0",),
}

_SONAME_RE = re.compile(r"^lib(?P<stem>[^/]+?)\.so(?:\.(?P<abi>\d+))?$")

# 链接期由 libc/gcc 必带、不需要单独装的库名（`-lm` 这类）
_IMPLICIT_LINK_LIBS = frozenset({
    "m", "dl", "rt", "pthread", "c", "gcc", "gcc_s", "stdc++", "util", "resolv",
    "nsl", "crypt", "atomic", "quadmath",
})


def shared_lib_apt_candidates(soname: str) -> list[str]:
    """动态库 → apt 候选包名（未收录时按 soname 约定猜，由 apt 干跑校验）。"""
    name = (soname or "").strip()
    table = _SONAME_TO_APT.get(name)
    if table:
        return list(table)
    match = _SONAME_RE.fullmatch(name)
    if match is None:
        return []
    stem, abi = match.group("stem"), match.group("abi")
    if abi:
        return [f"lib{stem}{abi}", f"lib{stem}"]
    # 只有链接名（libfoo.so）：装带符号链接的开发包才能让链接期找到
    return [f"lib{stem}-dev", f"lib{stem}"]


@dataclass(frozen=True)
class Evidence:
    file: str
    line: int
    snippet: str


@dataclass
class Requirement:
    kind: str
    name: str
    spec: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    detail: str = ""

    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "spec": self.spec,
            "detail": self.detail,
            "evidence": [asdict(item) for item in self.evidence],
        }


def resolve_root(path: str, *, default: str = "") -> tuple[Path | None, str]:
    """把用户给的路径解析成项目目录；返回 (root, 错误信息)。"""
    raw = (path or default or "").strip()
    if not raw:
        return None, "缺少项目路径（请给出代码目录，例如 /home/bianbu/workspace/demo）"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None, f"路径无法解析：{raw}"
    if not candidate.is_dir():
        return None, f"目录不存在：{candidate}"
    return candidate, ""


def _run(argv: list[str], *, timeout: float = 20) -> tuple[int, str, str]:
    env = {**os.environ, "LC_ALL": "C"}
    try:
        p = run_bounded_command(argv, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", f"命令不存在：{argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时：{' '.join(argv)}"
    except OutputLimitExceeded:
        return 125, "", "命令输出超过上限"
    return p.returncode, p.stdout, p.stderr


def _requirement(
    found: dict[str, Requirement], kind: str, name: str, evidence: Evidence,
    *, spec: str = "", detail: str = "",
) -> None:
    key = f"{kind}:{name}"
    item = found.get(key)
    if item is None:
        found[key] = Requirement(kind=kind, name=name, spec=spec, detail=detail, evidence=[evidence])
        return
    if spec and not item.spec:
        item.spec = spec
    if detail and not item.detail:
        item.detail = detail
    if len(item.evidence) < _MAX_EVIDENCE and evidence not in item.evidence:
        item.evidence.append(evidence)


def _iter_files(root: Path) -> tuple[list[Path], int]:
    files: list[Path] = []
    skipped = 0
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith(".") and filename not in {".env"}:
                continue
            path = Path(current) / filename
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            files.append(path)
            if len(files) >= _MAX_FILES:
                return files, skipped
    return files, skipped


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


_CTYPES_LOADERS = frozenset({"CDLL", "PyDLL", "LoadLibrary"})
_CTYPES_FINDERS = frozenset({"find_library"})
_CTYPES_TARGETS = _CTYPES_LOADERS | _CTYPES_FINDERS


def _dotted_name(node: ast.AST) -> str:
    """把 ``ctypes.cdll.LoadLibrary`` 这类属性访问还原成点分字符串。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _normalise_shared_lib(raw: str) -> str:
    """把 ctypes 参数/链接名规整成 soname 形态（``ssl`` → ``libssl.so``）。"""
    name = (raw or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        return ""
    if ".so" in name:
        return name
    return f"{name}.so" if name.startswith("lib") else f"lib{name}.so"


def _ctypes_local_names(tree: ast.AST) -> set[str]:
    """``from ctypes import CDLL`` 之后就可以裸名调用，这里收集本地别名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "ctypes":
            for alias in node.names:
                if alias.name in _CTYPES_TARGETS:
                    names.add(alias.asname or alias.name)
    return names


def _scan_python_ctypes(
    root: Path, path: Path, node: ast.AST, found: dict[str, Requirement],
    local_names: set[str],
) -> None:
    """`ctypes.CDLL("libssl.so.3")` / `find_library("ssl")` → 运行期动态库需求。"""
    if not isinstance(node, ast.Call) or not node.args:
        return
    func = _dotted_name(node.func)
    if not func:
        return
    tail = func.rsplit(".", 1)[-1]
    if tail not in _CTYPES_TARGETS:
        return
    if not (func.startswith("ctypes.") or func.split(".", 1)[0] in local_names or func in local_names):
        return
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return
    name = _normalise_shared_lib(first.value)
    if not name:
        return
    _requirement(
        found, KIND_SHARED_LIB, name,
        Evidence(_rel(root, path), node.lineno, f"{func}({first.value!r})"),
        detail="ctypes",
    )


def _scan_python(root: Path, path: Path, found: dict[str, Requirement], local_names: set[str]) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, OSError, ValueError):
        return
    ctypes_names = _ctypes_local_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _scan_python_ctypes(root, path, node, found, ctypes_names)
            continue
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for dotted in modules:
            top = dotted.split(".", 1)[0]
            if not top or top.startswith("_") or top in sys.stdlib_module_names:
                continue
            if top in local_names:
                continue
            _requirement(
                found, KIND_PYTHON, _MODULE_TO_DIST.get(top, top),
                Evidence(_rel(root, path), node.lineno, f"import {dotted}"),
                detail=f"module: {top}" if _MODULE_TO_DIST.get(top) else "",
            )


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^">]+)[">]')
_CMAKE_FIND_RE = re.compile(r"find_package\s*\(\s*([A-Za-z0-9_.+-]+)")
_CMAKE_PKG_RE = re.compile(r"pkg_check_modules\s*\(\s*[A-Za-z0-9_]+\s+(?:REQUIRED\s+|QUIET\s+)*([A-Za-z0-9_.+-]+)")
_MAKE_PKG_RE = re.compile(r"pkg-config\s+(?:--exists|--cflags|--libs|--modversion)[^\n#]*?\b([A-Za-z0-9_.+-]+)")
# `-lssl` 这样的链接旗标；前面不能是 `-`/词字符，否则会把 `--libs` 当成 `ibs`
_MAKE_LINK_RE = re.compile(r"(?<![-\w])-l([A-Za-z0-9_+.-]+)")


def _scan_c_header(root: Path, path: Path, line_no: int, header: str, found: dict[str, Requirement]) -> None:
    if header in _SYSTEM_HEADERS:
        return
    if header.startswith(("sys/", "netinet/", "arpa/", "linux/", "bits/")):
        return
    _requirement(
        found, KIND_C_HEADER, header,
        Evidence(_rel(root, path), line_no, f"#include <{header}>"),
    )


def _scan_requirements_txt(root: Path, path: Path, found: dict[str, Requirement]) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    for index, line in enumerate(lines, start=1):
        raw = line.split("#", 1)[0].strip()
        if not raw or raw.startswith("-"):
            continue
        if "://" in raw or raw.startswith(("git+", "http:", "https:", "file:")):
            # 非索引来源（git/URL/本地路径）：不自动安装，但要出现在清单里让用户看到
            _requirement(
                found, KIND_PYTHON, raw[:80],
                Evidence(_rel(root, path), index, raw), spec=raw,
                detail="来源非包索引，需人工确认",
            )
            continue
        name = re.split(r"[\[<>=!~;\s]", raw, maxsplit=1)[0].strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            continue
        _requirement(
            found, KIND_PYTHON, name,
            Evidence(_rel(root, path), index, raw), spec=raw,
        )


def _scan_pyproject(root: Path, path: Path, found: dict[str, Requirement]) -> None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return
    project = data.get("project") if isinstance(data, dict) else None
    specs: list[str] = []
    if isinstance(project, dict):
        for item in project.get("dependencies") or []:
            if isinstance(item, str):
                specs.append(item)
    poetry = (((data.get("tool") or {}) if isinstance(data, dict) else {}).get("poetry") or {})
    if isinstance(poetry, dict):
        for name in (poetry.get("dependencies") or {}):
            if str(name).lower() != "python":
                specs.append(str(name))
    for spec in specs:
        name = re.split(r"[\[<>=!~;\s]", spec.strip(), maxsplit=1)[0].strip()
        if not name or name.lower() == "python" or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            continue
        _requirement(
            found, KIND_PYTHON, name,
            Evidence(_rel(root, path), 1, spec), spec=spec,
        )


def _scan_package_json(root: Path, path: Path, found: dict[str, Requirement]) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            if not isinstance(name, str) or not re.fullmatch(r"[@A-Za-z0-9._/-]+", name):
                continue
            _requirement(
                found, KIND_NODE, name,
                Evidence(_rel(root, path), 1, f"{name}: {spec}"),
                spec=str(spec or ""), detail=section,
            )


def _scan_cmake(root: Path, path: Path, found: dict[str, Requirement]) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    for index, line in enumerate(lines, start=1):
        stripped = line.split("#", 1)[0]
        match = _CMAKE_PKG_RE.search(stripped)
        if match:
            _requirement(
                found, KIND_PKGCONFIG, match.group(1),
                Evidence(_rel(root, path), index, stripped.strip()),
            )
            continue
        match = _CMAKE_FIND_RE.search(stripped)
        if match:
            _requirement(
                found, KIND_PKGCONFIG, match.group(1).lower(),
                Evidence(_rel(root, path), index, stripped.strip()),
                detail="cmake find_package",
            )


def _scan_makefile(root: Path, path: Path, found: dict[str, Requirement]) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    for index, line in enumerate(lines, start=1):
        stripped = line.split("#", 1)[0]
        match = _MAKE_PKG_RE.search(stripped)
        if match:
            _requirement(
                found, KIND_PKGCONFIG, match.group(1),
                Evidence(_rel(root, path), index, stripped.strip()),
            )
        for lib in _MAKE_LINK_RE.findall(stripped):
            if lib in _IMPLICIT_LINK_LIBS:
                continue
            _requirement(
                found, KIND_SHARED_LIB, f"lib{lib}.so",
                Evidence(_rel(root, path), index, stripped.strip()),
                detail="link flag",
            )


def _local_module_names(root: Path) -> set[str]:
    names = {path.stem for path in root.glob("*.py")}
    for child in root.iterdir() if root.is_dir() else []:
        if not child.is_dir() or child.name in _SKIP_DIRS:
            continue
        if (child / "__init__.py").is_file():
            names.add(child.name)
    src = root / "src"
    if src.is_dir():
        for child in src.iterdir():
            if child.suffix == ".py":
                names.add(child.stem)
            elif child.is_dir() and (child / "__init__.py").is_file():
                names.add(child.name)
    return names


# 每扫描这么多文件回报一次进度：大项目有可展示的分母，又不至于把
# notifications/progress 刷满。
_SCAN_PROGRESS_STEP = 200


def scan_project(root: Path, *, limit: int = 200, on_progress=None) -> dict:
    """扫描项目，返回依赖需求清单（按 kind/name 去重，带证据）。

    ``on_progress(done, total)`` 可选，用于上报扫描进度；它只是观测量，
    回调抛异常不能影响扫描本身。
    """
    found: dict[str, Requirement] = {}
    files, skipped = _iter_files(root)
    local_names = _local_module_names(root)
    for position, path in enumerate(files, start=1):
        if on_progress is not None and (
            position % _SCAN_PROGRESS_STEP == 0 or position == len(files)
        ):
            try:
                on_progress(position, len(files))
            except Exception:  # noqa: BLE001 - 进度失败不影响扫描
                on_progress = None
        name = path.name
        suffix = path.suffix.lower()
        try:
            if suffix in _PY_SUFFIXES:
                _scan_python(root, path, found, local_names)
            elif suffix in _C_SUFFIXES:
                for index, line in enumerate(
                    path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
                ):
                    match = _INCLUDE_RE.match(line)
                    if match:
                        _scan_c_header(root, path, index, match.group(1), found)
            elif name == "requirements.txt" or re.fullmatch(r"requirements[-_].+\.txt", name):
                _scan_requirements_txt(root, path, found)
            elif name == "pyproject.toml":
                _scan_pyproject(root, path, found)
            elif name == "package.json":
                _scan_package_json(root, path, found)
            elif name == "CMakeLists.txt" or suffix == ".cmake":
                _scan_cmake(root, path, found)
            elif name.lower() in {"makefile", "gnumakefile"}:
                _scan_makefile(root, path, found)
        except (OSError, ValueError):
            continue
    requirements = sorted(found.values(), key=lambda item: (item.kind, item.name.lower()))
    truncated = len(requirements) > limit
    return {
        "ok": True,
        "root": str(root),
        "files_scanned": len(files),
        "files_skipped": skipped,
        "requirements": [item.to_dict() for item in requirements[:limit]],
        "total_requirements": len(requirements),
        "truncated": truncated,
    }


# ── 本机满足度判定 ────────────────────────────────────────────────────

_PY_CHECK = """
import importlib.metadata as metadata, importlib.util, json, sys
names = json.loads(sys.argv[1])


def top_levels(distribution):
    # 分发名 != import 名时的真实导入名（pyahocorasick -> ahocorasick）
    declared = (distribution.read_text("top_level.txt") or "").split()
    if declared:
        return declared
    found = set()
    for path in distribution.files or []:
        parts = getattr(path, "parts", ())
        if not parts:
            continue
        head = parts[0]
        if head.startswith("..") or head.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if path.suffix in (".so", ".pyd", ".py"):
            found.add(head.split(".")[0])
        elif len(parts) > 1 and parts[1] == "__init__.py":
            found.add(head)
    return sorted(found)


result = {}
for name in names:
    module = ""
    try:
        if importlib.util.find_spec(name) is not None:
            module = name
    except BaseException:
        module = ""
    if not module:
        try:
            distribution = metadata.distribution(name)
        except BaseException:
            distribution = None
        if distribution is not None:
            for candidate in top_levels(distribution):
                try:
                    if candidate and importlib.util.find_spec(candidate) is not None:
                        module = candidate
                        break
                except BaseException:
                    continue
    result[name] = module
print(json.dumps(result))
"""


def project_python(root: Path) -> str | None:
    """项目自带的 venv 解释器（存在才返回）。"""
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def install_target_python(root: Path, python: str = "") -> tuple[str, str]:
    """Python 依赖的安装/验证目标解释器 → (解释器路径, 环境标签)。

    项目 venv 优先；否则用用户共享 venv —— 共享 venv 可能尚未创建
    （由 pip_install 首次安装时创建），这里照样返回它的路径，
    这样「计划阶段解析出来的目标环境」和「安装/验证阶段实际用的环境」一致。
    """
    if python:
        resolved = shutil.which(python) or python
        return resolved, "explicit"
    project = project_python(root)
    if project:
        return project, "project"
    return str(SHARED_VENV_PYTHON), "user"


def _python_runner(root: Path, python: str = "") -> str:
    if python:
        return shutil.which(python) or python
    project = project_python(root)
    if project:
        return project
    if SHARED_VENV_PYTHON.is_file():
        return str(SHARED_VENV_PYTHON)
    return shutil.which("python3") or sys.executable


def _check_python(requirements: list[Requirement], root: Path, python: str) -> dict[str, dict]:
    modules = {}
    for item in requirements:
        module = item.detail.removeprefix("module: ").strip() if item.detail.startswith("module: ") else item.name
        modules[item.key()] = module
    names = sorted(set(modules.values()))
    runner = _python_runner(root, python)
    rc, out, err = _run([runner, "-c", _PY_CHECK, json.dumps(names)], timeout=30)
    results: dict[str, dict] = {}
    if rc != 0:
        for key, module in modules.items():
            results[key] = {
                "satisfied": None,
                "check": f"{runner} import {module}",
                "detail": err.strip() or "Python import 探测失败",
            }
        return results
    try:
        raw = json.loads(out)
    except ValueError:
        raw = {}
    for key, module in modules.items():
        matched = str(raw.get(module) or "")
        results[key] = {
            "satisfied": bool(matched),
            # 命中分发里的真实 import 名时按真实名展示（pyahocorasick → ahocorasick）
            "check": f"{runner} import {matched or module}",
            "detail": "" if matched else f"模块 {module} 不可导入",
        }
    return results


def _check_c_header(requirements: list[Requirement]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    roots = (Path("/usr/include"), Path("/usr/local/include"))
    for item in requirements:
        matches = [str(base / item.name) for base in roots if (base / item.name).is_file()]
        results[item.key()] = {
            "satisfied": bool(matches),
            "check": f"test -f /usr/include/{item.name}",
            "detail": matches[0] if matches else f"头文件 {item.name} 不在 /usr/include",
        }
    return results


def _check_pkgconfig(requirements: list[Requirement]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    if shutil.which("pkg-config") is None:
        for item in requirements:
            results[item.key()] = {
                "satisfied": None, "check": "pkg-config --exists", "detail": "未安装 pkg-config",
            }
        return results
    for item in requirements:
        rc, _, err = _run(["pkg-config", "--exists", item.name], timeout=10)
        results[item.key()] = {
            "satisfied": rc == 0,
            "check": f"pkg-config --exists {item.name}",
            "detail": (err or "").strip() if rc != 0 else "",
        }
    return results


def _ldconfig_entries() -> str:
    for argv in (["ldconfig", "-p"], ["/sbin/ldconfig", "-p"]):
        rc, out, _ = _run(argv, timeout=20)
        if rc == 0:
            return out
    return ""


_SO_PROBE = """
import ctypes, json, sys
result = {}
for name in json.loads(sys.argv[1]):
    try:
        ctypes.CDLL(name)
        result[name] = True
    except OSError:
        result[name] = False
print(json.dumps(result))
"""


def _ldconfig_has(entries: str, soname: str) -> bool:
    return bool(re.search(rf"(?:^|\s){re.escape(soname)}\s", entries, re.MULTILINE))


def _check_shared_lib(requirements: list[Requirement]) -> dict[str, dict]:
    """动态库是否可加载：优先真加载（ctypes.CDLL），拿不到就退回 ldconfig 名单。"""
    names = sorted({item.name for item in requirements})
    runner = shutil.which("python3") or sys.executable
    rc, out, _ = _run([runner, "-c", _SO_PROBE, json.dumps(names)], timeout=30)
    probed: dict[str, bool] = {}
    if rc == 0:
        try:
            probed = json.loads(out)
        except ValueError:
            probed = {}
    entries = "" if probed else _ldconfig_entries()
    results: dict[str, dict] = {}
    for item in requirements:
        if item.name in probed:
            loaded = bool(probed[item.name])
            results[item.key()] = {
                "satisfied": loaded,
                "check": f"ctypes.CDLL({item.name!r})",
                "detail": "" if loaded else f"动态库 {item.name} 无法加载（ldd not found 或依赖缺失）",
            }
            continue
        present = _ldconfig_has(entries, item.name)
        results[item.key()] = {
            "satisfied": present,
            "check": f"ldconfig -p | grep {item.name}",
            "detail": "" if present else f"ldconfig 缓存里没有 {item.name}",
        }
    return results


def _check_command(requirements: list[Requirement]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for item in requirements:
        path = shutil.which(item.name)
        results[item.key()] = {
            "satisfied": path is not None,
            "check": f"which {item.name}",
            "detail": path or f"命令 {item.name} 不在 PATH",
        }
    return results


def _check_node(requirements: list[Requirement], root: Path) -> dict[str, dict]:
    results: dict[str, dict] = {}
    has_npm = shutil.which("npm") is not None
    for item in requirements:
        local = (root / "node_modules" / item.name).exists()
        results[item.key()] = {
            "satisfied": bool(local and has_npm),
            "check": f"node_modules/{item.name}",
            "detail": "" if local and has_npm else (
                "本机未安装 npm/node" if not has_npm else f"node_modules 中没有 {item.name}"
            ),
        }
    return results


def check_requirements(
    requirements: list[Requirement], *, root: Path, python: str = ""
) -> dict[str, dict]:
    """逐条判定本机是否满足；返回 {requirement.key(): {satisfied, check, detail}}。"""
    grouped: dict[str, list[Requirement]] = {}
    for item in requirements:
        grouped.setdefault(item.kind, []).append(item)
    results: dict[str, dict] = {}
    if grouped.get(KIND_PYTHON):
        results.update(_check_python(grouped[KIND_PYTHON], root, python))
    if grouped.get(KIND_C_HEADER):
        results.update(_check_c_header(grouped[KIND_C_HEADER]))
    if grouped.get(KIND_PKGCONFIG):
        results.update(_check_pkgconfig(grouped[KIND_PKGCONFIG]))
    if grouped.get(KIND_SHARED_LIB):
        results.update(_check_shared_lib(grouped[KIND_SHARED_LIB]))
    if grouped.get(KIND_COMMAND):
        results.update(_check_command(grouped[KIND_COMMAND]))
    if grouped.get(KIND_NODE):
        results.update(_check_node(grouped[KIND_NODE], root))
    for item in requirements:
        results.setdefault(
            item.key(),
            {"satisfied": None, "check": "", "detail": f"不支持的依赖类型：{item.kind}"},
        )
    return results


def requirements_from_dicts(payload: list[dict]) -> list[Requirement]:
    items: list[Requirement] = []
    for raw in payload or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not kind or not name:
            continue
        evidence = [
            Evidence(
                str(entry.get("file") or ""), int(entry.get("line") or 0),
                str(entry.get("snippet") or ""),
            )
            for entry in (raw.get("evidence") or [])
            if isinstance(entry, dict)
        ]
        items.append(Requirement(
            kind=kind, name=name, spec=str(raw.get("spec") or ""),
            detail=str(raw.get("detail") or ""), evidence=evidence,
        ))
    return items


def project_fingerprint(root: Path, *, max_files: int = 2000) -> str:
    """项目文件指纹：内容 + 相对路径，用于计划失效判断（只读）。"""
    digest = hashlib.sha256()
    files, _ = _iter_files(root)
    count = 0
    for path in sorted(files)[:max_files]:
        rel = _rel(root, path)
        digest.update(rel.encode("utf-8", "ignore"))
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    count += len(chunk)
                    if count > 32_000_000:
                        break
        except OSError:
            continue
        if count > 32_000_000:
            break
    return digest.hexdigest()[:32]

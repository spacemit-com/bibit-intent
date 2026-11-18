from __future__ import annotations

import configparser
import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _qt_string(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    return re.sub(r"\\(?:x([0-9a-fA-F]{1,4})|(.))",
                  lambda match: chr(int(match[1], 16)) if match[1] else escapes.get(match[2], match[2]), value)


def _settings(config_roots: list[Path], profile: str) -> tuple[Path, dict[str, str]]:
    for root in config_roots:
        config = root / "pcmanfm-qt" / profile / "settings.conf"
        if not config.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        with config.open(encoding="utf-8-sig") as stream:
            parser.read_file(stream)
        if not parser.has_section("Desktop"):
            raise ValueError("Missing Desktop settings")
        return config, {key: _qt_string(value) for key, value in parser.items("Desktop")}
    raise ValueError("No PCManFM-Qt profile settings found")


def _image_path(value: str, home: Path) -> Path:
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("Only local file:// image URLs are supported")
        value = unquote(parsed.path)
    if value.startswith("~/"):
        value = str(home / value[2:])
    if any(ord(char) < 32 for char in value) or not Path(value).is_absolute():
        raise ValueError("An absolute local image path is required")
    path = Path(value).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
        raise ValueError("Select a PNG, JPEG, BMP or GIF image file")
    if not os.access(path, os.R_OK):
        raise ValueError("The desktop user cannot read the image")
    return path


def _validate_image(path: Path) -> None:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 16, 16, True)


def _call(environment: dict[str, str], *arguments: str) -> str:
    result = subprocess.run(["/usr/bin/busctl", "--user", "--auto-start=no", "--timeout=5", *arguments],
                            env=environment, capture_output=True, text=True, timeout=7)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "D-Bus call failed")
    return result.stdout.strip()


def _context(process_id: int) -> tuple[dict[str, str], Path, Path, list[Path], str]:
    process = Path("/proc") / str(process_id)
    if process.stat().st_uid != os.getuid():
        raise ValueError("Wallpaper must be set as the desktop session owner")
    command = (process / "cmdline").read_bytes().decode().split("\0")
    if Path(command[0]).name != "pcmanfm-qt" or "--desktop" not in command:
        raise ValueError("PCManFM-Qt desktop is not running")
    process_env = dict(entry.decode().split("=", 1) for entry in (process / "environ").read_bytes().split(b"\0") if b"=" in entry)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    runtime = Path(f"/run/user/{os.getuid()}")
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
                   "DBUS_SESSION_BUS_ADDRESS": process_env.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")}
    profile = "default"
    for index, argument in enumerate(command):
        if argument.startswith("--profile="):
            profile = argument.split("=", 1)[1]
        elif argument in ("--profile", "-p") and index + 1 < len(command):
            profile = command[index + 1]
    if not re.fullmatch(r"[\w.-]+", profile) or profile in (".", ".."):
        raise ValueError("Unsupported profile name")
    config_roots = [Path(process_env.get("XDG_CONFIG_HOME") or home / ".config")]
    config_roots.extend(Path(root) for root in (process_env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if root.startswith("/"))
    return environment, home, runtime, config_roots, profile


def apply_wallpaper(process_id: int, value: str) -> dict:
    environment, home, runtime, config_roots, profile = _context(process_id)
    path = _image_path(value, home)
    _validate_image(path)
    lock_path = runtime / "bibit-intent-wallpaper.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = _call(environment, "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                      "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s", "org.pcmanfm.PCManFM")
        if owner != f"u {process_id}":
            raise ValueError("PCManFM-Qt D-Bus owner does not match the desktop process")
        _, before = _settings(config_roots, profile)
        if before.get("PerScreenWallpaper", "false").lower() == "true" or int(before.get("SlideShowInterval", "0")) > 0:
            raise ValueError("Disable per-screen wallpaper or slideshow in desktop settings first")
        mode = before.get("WallpaperMode", "zoom")
        if mode not in ("stretch", "fit", "center", "tile", "zoom"):
            mode = "zoom"
        _call(environment, "call", "org.pcmanfm.PCManFM", "/Application", "org.pcmanfm.Application",
              "setWallpaper", "ss", str(path), mode)
        config, after = _settings(config_roots, profile)
        if after.get("Wallpaper") != str(path) or after.get("WallpaperMode") != mode:
            raise ValueError("PCManFM-Qt did not persist the requested wallpaper; desktop update is unconfirmed")
    return {"ok": True, "path": str(path), "previous_path": before.get("Wallpaper", ""),
            "profile": profile, "config": str(config), "channel": "host", "mode": mode}


def main() -> None:
    try:
        result = apply_wallpaper(int(sys.argv[1]), sys.argv[2])
    except Exception as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

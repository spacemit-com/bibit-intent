from __future__ import annotations

import re


def parse_wallpaper_request(text: str) -> str | None:
    text = text.strip().rstrip("。！!")
    patterns = (
        r"(?:请|请帮我|帮我)?(?:把|将)\s*(.+?)\s*(?:设置|设|更换|换|变)(?:为|成)(?:桌面)?(?:壁纸|背景)",
        r"(?:请|请帮我|帮我)?(?:把|将)?(?:桌面)?壁纸(?:设置|设|更换|换|切换)(?:为|成|到)\s*(.+)",
        r"(?:请|请帮我|帮我)?(?:设置|更换|切换)(?:桌面)?壁纸(?:为|成|到)\s*(.+)",
        r"(?:please\s+)?(?:set|change)\s+(?:the\s+)?(?:desktop\s+)?(?:wallpaper|background)\s+to\s+(.+)",
        r"(?:please\s+)?(?:set|use)\s+(.+?)\s+as\s+(?:the\s+)?(?:desktop\s+)?(?:wallpaper|background)",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, re.I)
        if match is None:
            continue
        path = match.group(1).strip()
        if len(path) >= 2 and (path[0], path[-1]) in (("\"", "\""), ("'", "'"), ("“", "”")):
            path = path[1:-1]
        elif re.search(r"\s|然后|并且|再把|同时|并", path):
            return None
        if path.startswith(("/", "~/", "file://")) and not any(ord(char) < 32 for char in path):
            return path
    return None


def is_wallpaper_request(text: str) -> bool:
    return bool(re.search(r"壁纸|桌面背景|\bwallpaper\b|\bdesktop\s+background\b", text, re.I))

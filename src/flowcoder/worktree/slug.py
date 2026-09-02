"""Worktree slug 校验与扁平化。"""

from __future__ import annotations

import re

MAX_SLUG_LENGTH = 64
_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_slug(name: str) -> str | None:
    """校验 worktree 名是否符合限制，非法时返回错误描述，合法返回 None。"""
    if not name:
        return "name cannot be empty"
    if len(name) > MAX_SLUG_LENGTH:
        return f"name too long (max {MAX_SLUG_LENGTH} characters)"

    # 逐段校验，既允许层级名（a/b）又不允许路径穿越（. / .. / 空段）
    segments = name.split("/")
    for seg in segments:
        if not seg:
            return "name contains empty segment"
        if seg in (".", ".."):
            return "name must not contain '.' or '..' as a segment"
        if not _SEGMENT_RE.match(seg):
            return f"invalid segment: {seg!r} (allowed: letters, digits, '.', '-', '_')"

    return None


def flatten_slug(name: str) -> str:
    return name.replace("/", "+")

"""Helpers for compact task display titles."""

from __future__ import annotations

import re


MAX_TASK_TITLE_CHARS = 20


def summarize_task_title(text: str | None, *, fallback: str = "未命名任务",
                         max_chars: int = MAX_TASK_TITLE_CHARS) -> str:
    """Return a compact task title suitable for banners and live panels."""
    cleaned = _clean_title_source(text or "")
    title = _domain_title(cleaned) or _first_useful_phrase(cleaned)
    if not title:
        title = _clean_title_source(fallback) or "未命名任务"
    return _limit_title(title, max_chars)


def _clean_title_source(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[@#*_`>\\[\\]{}()（）【】「」『』“”\"']", " ", value)
    value = re.sub(r"[\u2705\u274c\u26a0\ufe0f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^(请|帮我|帮|麻烦|需要|请你|你来|我要|我想)\s*", "", value)
    value = re.sub(r"^(统一风格提示词|通用负面提示词)\s*[：:]\s*", "", value)
    return value.strip(" ，,。.;；:：")


def _domain_title(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return ""
    lower = compact.lower()
    parts: list[str] = []

    if "早教" in compact:
        parts.append("早教")
    elif "幼教" in compact:
        parts.append("幼教")

    has_ai = bool(re.search(r"(?i)(^|[^a-z])ai([^a-z]|$)", text)) or "智能" in compact
    if has_ai:
        parts.append("AI")

    if "微信小程序" in compact:
        parts.append("微信小程序")
    elif "小程序" in compact:
        parts.append("小程序")
    elif "移动端" in compact:
        parts.append("移动端")

    if "高保真" in compact:
        parts.append("高保真")
    if "demo" in lower:
        parts.append("Demo")
    elif "原型" in compact:
        parts.append("原型")
    elif "页面" in compact or "界面" in compact:
        parts.append("界面")

    title = "".join(parts)
    if len(title) >= 4:
        return title
    return ""


def _first_useful_phrase(text: str) -> str:
    if not text:
        return ""
    separators = r"[。；;\n\r]|(?:\d+\.\s*)"
    fragments = [frag.strip(" ，,。.;；:：") for frag in re.split(separators, text)]
    fragments = [frag for frag in fragments if frag]
    if not fragments:
        return text

    prefixes = (
        "生成", "设计", "开发", "实现", "修复", "优化", "改造", "创建",
        "输出", "制作", "搭建", "整理", "检查", "分析",
    )
    for fragment in fragments:
        if any(prefix in fragment for prefix in prefixes):
            return _compress_phrase(fragment)
    return _compress_phrase(fragments[0])


def _compress_phrase(text: str) -> str:
    value = _clean_title_source(text)
    value = re.sub(
        r"(需要|生成|设计|开发|实现|修复|优化|改造|创建|输出|制作|搭建|整理|检查|分析|一套|一个|这套|这个)",
        "",
        value,
    )
    value = re.sub(r"\s+", "", value)
    return value.strip(" ，,。.;；:：") or _clean_title_source(text)


def _limit_title(title: str, max_chars: int) -> str:
    value = re.sub(r"\s+", "", str(title or "")).strip(" ，,。.;；:：")
    limit = max(1, int(max_chars or MAX_TASK_TITLE_CHARS))
    return value[:limit]

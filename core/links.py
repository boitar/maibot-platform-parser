"""Recover card targets before display text and reject truncated X post URLs."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

URL_RE = re.compile(r"https?://[^\s\]\)）>\"'<，。！？：]+")
X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
X_STATUS_PATH = re.compile(
    r"/(?:(?:[A-Za-z0-9_]+/)*status|statuses)/[0-9]+(?:/(?:photo|video)/[0-9]+)?/?\Z"
)
TARGET_FIELDS = ("jumpUrl", "jump_url", "source_url", "url", "qqdocurl", "target_url")
TARGET_LABEL = re.compile(r"(?:链接|跳转|链接地址)\s*[:：]\s*$")
DISPLAY_FIELDS = {"text", "title", "desc", "description", "prompt", "content", "data"}


def _card_targets(value: Any, depth: int = 0) -> list[str]:
    if depth > 12:
        return []
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return _card_targets(json.loads(value), depth + 1)
            except (ValueError, RecursionError):
                pass
        return []
    if isinstance(value, dict):
        targets = [
            value[key] for key in TARGET_FIELDS if isinstance(value.get(key), str)
        ]
        for key, child in value.items():
            if key not in TARGET_FIELDS:
                targets.extend(_card_targets(child, depth + 1))
        return targets
    if isinstance(value, list):
        return [target for child in value for target in _card_targets(child, depth + 1)]
    return []


def _visible_text(value: Any, depth: int = 0) -> list[str]:
    if depth > 12:
        return []
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return _visible_text(json.loads(value), depth + 1)
            except (ValueError, RecursionError):
                pass
        return [value]
    if isinstance(value, dict):
        return [
            text
            for key, child in value.items()
            if key in DISPLAY_FIELDS or isinstance(child, (dict, list))
            for text in _visible_text(child, depth + 1)
        ]
    if isinstance(value, list):
        return [text for child in value for text in _visible_text(child, depth + 1)]
    return []


def message_source(message: dict[str, Any]) -> str:
    targets = _card_targets(message.get("raw_message", []))
    info = message.get("message_info") or {}
    if isinstance(info, dict):
        config = info.get("additional_config") or {}
        if isinstance(config, dict):
            targets.extend(_card_targets(config.get("platform_card_payloads", [])))
    parts = [f"链接: {target}" for target in dict.fromkeys(targets)]
    parts.append(str(message.get("processed_plain_text") or ""))
    for segment in message.get("raw_message", []) or []:
        if not isinstance(segment, dict):
            continue
        parts.extend(_visible_text(segment.get("data")))
    return "\n".join(parts)


def _is_x_post(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.hostname in X_HOSTS and bool(
            re.search(r"/(?:status|statuses)/", parsed.path)
        )
    except ValueError:
        return False


def _complete_x_post(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        decoded = unquote(url)
        return not any(marker in decoded for marker in ("…", "...", "⋯")) and bool(
            X_STATUS_PATH.fullmatch(parsed.path)
        )
    except ValueError:
        return False


def extract_urls(text: str) -> list[str]:
    candidates = []
    seen = set()
    for match in URL_RE.finditer(text):
        raw = match.group(0)
        is_x = _is_x_post(raw)
        # Check truncation before stripping punctuation; never turn /211... into /211.
        if is_x and any(marker in unquote(raw) for marker in ("…", "...", "⋯")):
            continue
        url = raw.rstrip(".,，。!！?？;；:：")
        if is_x and not _complete_x_post(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        labeled_target = is_x and bool(
            TARGET_LABEL.search(text[max(0, match.start() - 20) : match.start()])
        )
        candidates.append((0 if labeled_target else 1, len(candidates), url))
    return [url for _, _, url in sorted(candidates)]


def has_incomplete_x_post(text: str) -> bool:
    for match in URL_RE.finditer(text):
        raw = match.group(0)
        if _is_x_post(raw) and not _complete_x_post(raw.rstrip(",，。!！?？;；:：")):
            return True
    return False

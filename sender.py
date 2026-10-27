"""OneBot HTTP sender for the MaiBot multi-platform parser plugin."""

from __future__ import annotations

import asyncio
import base64
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("plugin.multi_platform_parser.sender")


class ApiSettings:
    host: str
    port: int
    token: str
    bot_uin: str


MessageSegment = dict[str, Any]


async def send_text(message: dict[str, Any], text: str, api: ApiSettings) -> bool:
    return await _send_message(
        message, [{"type": "text", "data": {"text": text}}], api, timeout=30
    )


async def send_image(message: dict[str, Any], path: Path, api: ApiSettings) -> bool:
    """Send image bytes without requiring access to the bot's filesystem.

    Args:
        message: Incoming message identifying the recipient.
        path: Local image to send.
        api: OneBot endpoint and authentication settings.

    Returns:
        Whether OneBot reported a successful send.
    """
    # QQ may not have permission to read files in the bot's cache directory.
    image_b64 = await asyncio.to_thread(
        lambda: base64.b64encode(path.read_bytes()).decode("ascii")
    )
    return await _send_message(
        message,
        [{"type": "image", "data": {"file": f"base64://{image_b64}"}}],
        api,
        timeout=120,
    )


async def send_video(message: dict[str, Any], path: Path, api: ApiSettings) -> bool:
    nodes = await embed_local_media([[video_segment(path)]])
    return await _send_message(message, nodes[0], api, timeout=300)


async def embed_local_media(
    nodes: list[list[MessageSegment]],
) -> list[list[MessageSegment]]:
    """Embed local image/video bytes for both SDK and HTTP without mutating nodes."""
    embedded = []
    cache: dict[str, str] = {}
    for node in nodes:
        content = []
        for segment in node:
            if segment.get("type") in {"image", "video"}:
                data = segment.get("data", {})
                value = str(data.get("file", ""))
                if value.startswith("file://"):
                    if value not in cache:
                        path = Path(
                            urllib.request.url2pathname(
                                urllib.parse.urlparse(value).path
                            )
                        )
                        encoded = await asyncio.to_thread(
                            lambda p=path: base64.b64encode(p.read_bytes()).decode(
                                "ascii"
                            )
                        )
                        cache[value] = f"base64://{encoded}"
                    segment = {**segment, "data": {**data, "file": cache[value]}}
            content.append(segment)
        embedded.append(content)
    return embedded


async def send_file(
    message: dict[str, Any], path: Path, name: str | None, api: ApiSettings
) -> bool:
    """上传文件到群/私聊。使用 upload_group_file / upload_private_file 接口。"""
    if _is_private_message(message):
        user_id = _get_user_id(message)
        if not user_id:
            logger.error("Cannot upload private file: missing user id")
            return False
        url = f"http://{api.host}:{api.port}/upload_private_file"
        payload = {
            "user_id": user_id,
            "file": _file_uri(path),
            "name": name or path.name,
        }
    else:
        group_id = _get_group_id(message)
        if not group_id:
            logger.error("Cannot upload group file: missing group id")
            return False
        url = f"http://{api.host}:{api.port}/upload_group_file"
        payload = {
            "group_id": group_id,
            "file": _file_uri(path),
            "name": name or path.name,
        }
    return await _post_onebot(url, payload, api, timeout=300)


async def send_group_forward(
    message: dict[str, Any],
    nodes: list[list[MessageSegment]],
    api: ApiSettings,
) -> bool:
    """Send forward nodes with local image/video files embedded as Base64.

    Args:
        message: Incoming message identifying the recipient.
        nodes: Message segments grouped into forward nodes.
        api: OneBot endpoint and authentication settings.

    Returns:
        Whether OneBot reported a successful send.
    """
    if not nodes:
        return True

    forward_nodes = []
    for content in await embed_local_media(nodes):
        forward_nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "麦麦解析",
                    "uin": api.bot_uin or "10000",
                    "content": content,
                },
            }
        )

    timeout = (
        300
        if any(seg.get("type") == "video" for node in nodes for seg in node)
        else 120
    )
    group_id = _get_group_id(message)
    if group_id:
        return await _post_onebot(
            f"http://{api.host}:{api.port}/send_group_forward_msg",
            {"group_id": group_id, "messages": forward_nodes},
            api,
            timeout=timeout,
        )

    user_id = _get_user_id(message)
    if user_id:
        return await _post_onebot(
            f"http://{api.host}:{api.port}/send_private_forward_msg",
            {"user_id": user_id, "messages": forward_nodes},
            api,
            timeout=timeout,
        )

    return False


async def get_message(message_id: str, api: ApiSettings) -> list[MessageSegment] | None:
    """通过 OneBot get_msg 拉取指定消息的原始消息段（用于引用消息解析）。"""
    url = f"http://{api.host}:{api.port}/get_msg"
    token = str(getattr(api, "token", "") or "").strip()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json={"message_id": message_id}, headers=headers, timeout=30
            ) as response:
                if response.status != 200:
                    logger.error("get_msg failed: HTTP %s", response.status)
                    return None
                body = await response.json()
    except Exception as exc:
        logger.error("get_msg request error: %s", exc)
        return None

    if not isinstance(body, dict) or body.get("retcode") not in (0, None):
        logger.error("get_msg returned error: %s", body)
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    chain = data.get("message")
    if isinstance(chain, list):
        return chain
    raw = data.get("raw_message")
    if raw:
        return [{"type": "text", "data": {"text": str(raw)}}]
    return None


def text_segment(text: str) -> MessageSegment:
    return {"type": "text", "data": {"text": text}}


def image_segment(path: Path) -> MessageSegment:
    return {"type": "image", "data": {"file": _file_uri(path)}}


def video_segment(path: Path) -> MessageSegment:
    return {"type": "video", "data": {"file": _file_uri(path)}}


async def _send_message(
    message: dict[str, Any],
    segments: list[MessageSegment],
    api: ApiSettings,
    *,
    timeout: int,
) -> bool:
    if _is_private_message(message):
        user_id = _get_user_id(message)
        if not user_id:
            logger.error("Cannot send private message: missing user id")
            return False
        url = f"http://{api.host}:{api.port}/send_private_msg"
        payload = {"user_id": user_id, "message": segments}
    else:
        group_id = _get_group_id(message)
        if not group_id:
            logger.error("Cannot send group message: missing group id")
            return False
        url = f"http://{api.host}:{api.port}/send_group_msg"
        payload = {"group_id": group_id, "message": segments}

    return await _post_onebot(url, payload, api, timeout=timeout)


async def _post_onebot(
    url: str, payload: dict[str, Any], api: ApiSettings, *, timeout: int
) -> bool:
    token = str(getattr(api, "token", "") or "").strip()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession() as session:
            request_url = url
            for attempt in range(2):
                async with session.post(
                    request_url, json=payload, headers=headers, timeout=timeout
                ) as response:
                    if response.status in (401, 403) and token and attempt == 0:
                        request_url = f"{url}?access_token={urllib.parse.quote(token)}"
                        continue
                    if response.status != 200:
                        logger.error(
                            "OneBot request failed: HTTP %s %s",
                            response.status,
                            await response.text(),
                        )
                        return False
                    body = await response.json()
                    if not isinstance(body, dict):
                        logger.error("OneBot returned an invalid response")
                        return False
                    if body.get("retcode") == 0 and body.get("status") == "ok":
                        data = body.get("data")
                        if not isinstance(data, dict):
                            data = {}
                        logger.info(
                            "OneBot %s succeeded: message_id=%s forward_id=%s",
                            urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1],
                            data.get("message_id"),
                            data.get("forward_id") or data.get("res_id"),
                        )
                        return True
                    logger.error(
                        "OneBot action failed: status=%s retcode=%s message=%s",
                        body.get("status"),
                        body.get("retcode"),
                        body.get("message") or body.get("wording"),
                    )
                    return False
    except asyncio.TimeoutError:
        logger.error("OneBot request timed out: %s", url)
        return False
    except Exception as exc:
        logger.error("OneBot request error: %s", exc)
        return False


def _file_uri(path: Path) -> str:
    value = str(path)
    if value.startswith(("http://", "https://", "file://")):
        return value
    return "file:///" + urllib.request.pathname2url(value).lstrip("/")


def _is_private_message(message: dict[str, Any]) -> bool:
    message_info = message.get("message_info", {})
    return bool(message_info) and message_info.get("group_info") is None


def _get_user_id(message: dict[str, Any]) -> str | None:
    message_info = message.get("message_info", {})
    user_info = (
        message_info.get("user_info", {}) if isinstance(message_info, dict) else {}
    )
    user_id = user_info.get("user_id") if isinstance(user_info, dict) else None
    return str(user_id) if user_id else None


def _get_group_id(message: dict[str, Any]) -> str | None:
    message_info = message.get("message_info", {})
    group_info = (
        message_info.get("group_info") if isinstance(message_info, dict) else None
    )
    group_id = group_info.get("group_id") if isinstance(group_info, dict) else None
    return str(group_id) if group_id else None

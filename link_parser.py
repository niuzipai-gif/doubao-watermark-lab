"""Resolve public Doubao share links to original media downloads.

This module is intentionally separate from the local watermark-cleaning
pipeline. It only handles public share pages and never calls clean-image or
clean-video.
"""

import html
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse


MAX_RESPONSE_BYTES = int(os.environ.get("DOUBAO_LINK_MAX_BYTES", str(300 * 1024 * 1024)))
MAX_JSON_BYTES = 8 * 1024 * 1024
DOUBAO_API = "https://www.doubao.com/samantha/media/get_play_info"
DOUBAO_HOST_SUFFIX = ".doubao.com"
MEDIA_HOST_SUFFIXES = (".doubao.com", ".byteimg.com", ".ibytedimg.com", ".bytecdn.cn")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class LinkResolutionError(ValueError):
    """A user-facing error from the public share-link resolver."""


def _host_allowed(host: str, suffixes: tuple[str, ...]) -> bool:
    normalized = (host or "").lower().rstrip(".")
    return any(normalized == suffix[1:] or normalized.endswith(suffix) for suffix in suffixes)


def _validate_share_url(raw_url: str):
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise LinkResolutionError("链接必须以 http:// 或 https:// 开头")
    if not _host_allowed(parsed.hostname or "", (DOUBAO_HOST_SUFFIX,)):
        raise LinkResolutionError("这里只解析 doubao.com 的公开分享链接")
    if not parsed.path.startswith(("/thread/", "/video-sharing")):
        raise LinkResolutionError("请粘贴豆包对话分享链接或视频分享链接")
    return parsed


def _read_response(request, limit: int):
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise LinkResolutionError("远程素材超过 300 MB，已停止下载")
            return data, response.headers
    except LinkResolutionError:
        raise
    except urllib.error.HTTPError as error:
        raise LinkResolutionError(f"远程请求失败：HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise LinkResolutionError("远程请求超时或网络不可用") from error


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    data, _ = _read_response(request, MAX_JSON_BYTES)
    return data.decode("utf-8", errors="replace")


def _fetch_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Origin": "https://www.doubao.com",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    data, _ = _read_response(request, MAX_JSON_BYTES)
    try:
        result = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise LinkResolutionError("豆包返回的数据格式异常") from error
    if not isinstance(result, dict) or "data" not in result:
        raise LinkResolutionError("链接可能已失效，豆包没有返回媒体信息")
    return result


def _extract_video_keys(raw_html: str, parsed_url) -> list[str]:
    query = parse_qs(parsed_url.query)
    keys = list(query.get("video_id", []))
    decoded = html.unescape(raw_html).replace("\\/", "/")
    patterns = (
        r'\\?"vid\\?"\s*:\s*\\?"([^"\\]+)',
        r'&quot;vid& quot;\s*:\s*&quot;([^&]+)',
        r'video_id=([^&"\\]+)',
    )
    for pattern in patterns:
        keys.extend(re.findall(pattern, decoded, flags=re.IGNORECASE))
    return list(dict.fromkeys(item for item in keys if item))


def _extract_image_urls(raw_html: str) -> list[str]:
    decoded = html.unescape(raw_html).replace("\\/", "/").replace('\\"', '"')
    patterns = (
        r'image_ori_raw.{0,1200}?"url"\s*:\s*"(https?://[^"\\]+)',
        r'"url"\s*:\s*"(https?://[^"\\]+\.(?:png|jpe?g|webp)(?:\?[^"\\]*)?)',
    )
    urls = []
    for pattern in patterns:
        urls.extend(re.findall(pattern, decoded, flags=re.IGNORECASE | re.DOTALL))
    return list(dict.fromkeys(item.replace("&amp;", "&") for item in urls if item))


def _parse_thread_ssr(raw_html: str) -> tuple[list[str], list[str]]:
    """Extract media references from the escaped SSR payload in a /thread/ page."""
    patterns = (
        r'data-script-src="modern-run-router-data-fn" data-fn-args="(.*?)" nonce="',
        r'data-script-src="modern-run-window-fn" data-fn-name="mergeLoaderData" data-fn-args="(.*?)" nonce="',
    )
    payload = None
    for pattern in patterns:
        match = re.search(pattern, raw_html, flags=re.DOTALL)
        if not match:
            continue
        payload_text = html.unescape(match.group(1)).replace("&quot;", '"').replace("\\/", "/")
        try:
            payload = json.loads(payload_text)
            break
        except (TypeError, ValueError):
            continue
    if payload is None:
        return [], []

    image_urls: list[str] = []
    video_keys: list[str] = []

    def add_image(value: object) -> None:
        if not isinstance(value, str):
            return
        value = html.unescape(value).replace("\\/", "/").replace("&amp;", "&")
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and _host_allowed(parsed.hostname or "", MEDIA_HOST_SUFFIXES):
            image_urls.append(value)

    def add_video_key(value: object) -> None:
        if isinstance(value, str) and 4 <= len(value) <= 256:
            video_keys.append(value)

    def walk(value: object, depth: int = 0) -> None:
        if depth > 14:
            return
        if isinstance(value, str):
            candidate = html.unescape(value).replace("&quot;", '"')
            if candidate.startswith(("{", "[")):
                try:
                    walk(json.loads(candidate), depth + 1)
                except (TypeError, ValueError):
                    pass
            return
        if isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
            return
        if not isinstance(value, dict):
            return

        image_raw = value.get("image_ori_raw")
        if isinstance(image_raw, dict):
            add_image(image_raw.get("url"))
        image = value.get("image")
        if isinstance(image, dict):
            nested_image_raw = image.get("image_ori_raw")
            if isinstance(nested_image_raw, dict):
                add_image(nested_image_raw.get("url"))
        for key in ("vid", "video_id"):
            if key in value:
                add_video_key(value.get(key))
        for child in value.values():
            walk(child, depth + 1)

    walk(payload)
    return list(dict.fromkeys(image_urls)), list(dict.fromkeys(video_keys))


def _play_info(video_key: str) -> dict:
    params = {
        "version_code": "20800",
        "language": "zh-CN",
        "device_platform": "web",
        "aid": "497858",
        "real_aid": "497858",
        "pkg_type": "release_version",
        "device_id": "",
        "pc_version": "2.51.7",
        "region": "",
        "sys_region": "",
        "samantha_web": "1",
        "use-olympus-account": "1",
        "web_tab_id": "",
    }
    query = "&".join(f"{key}={value}" for key, value in params.items())
    result = _fetch_json(f"{DOUBAO_API}?{query}", {"key": video_key})
    data = result.get("data") or {}
    original = data.get("original_media_info") or {}
    media_url = original.get("main_url")
    if not media_url or not _host_allowed(urlparse(media_url).hostname or "", MEDIA_HOST_SUFFIXES):
        raise LinkResolutionError("豆包没有返回可下载的公开媒体地址")
    meta = original.get("meta") or {}
    return {
        "url": media_url,
        "kind": "video",
        "width": meta.get("width"),
        "height": meta.get("height"),
        "definition": meta.get("definition"),
    }


def _download_media(media_url: str, kind: str) -> dict:
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname or "", MEDIA_HOST_SUFFIXES):
        raise LinkResolutionError("解析结果不是受支持的豆包媒体地址")
    request = urllib.request.Request(media_url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.doubao.com/"})
    data, headers = _read_response(request, MAX_RESPONSE_BYTES)
    content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
    suffix = PurePosixPath(parsed.path).suffix.lower()
    if kind == "video":
        if content_type not in {"video/mp4", "video/webm", "video/quicktime"}:
            content_type = "video/mp4" if suffix != ".webm" else "video/webm"
        suffix = ".webm" if content_type == "video/webm" else ".mp4"
    else:
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            content_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
        suffix = ".jpg" if content_type == "image/jpeg" else ".webp" if content_type == "image/webp" else ".png"
    return {"data": data, "mimetype": content_type, "suffix": suffix, "kind": kind}


def resolve_first_media(raw_url: str) -> dict:
    """Resolve and download the first public image/video from a Doubao share page."""
    parsed = _validate_share_url(raw_url.strip())
    page_html = ""
    if parsed.path.startswith("/thread/"):
        page_html = _fetch_text(raw_url)
    ssr_images, ssr_video_keys = _parse_thread_ssr(page_html) if page_html else ([], [])
    video_keys = list(dict.fromkeys(ssr_video_keys + _extract_video_keys(page_html, parsed)))
    for key in video_keys:
        try:
            return _download_media(_play_info(key)["url"], "video")
        except LinkResolutionError:
            continue
    image_urls = list(dict.fromkeys(ssr_images + _extract_image_urls(page_html)))
    if image_urls:
        return _download_media(image_urls[0], "image")
    if parsed.path.startswith("/video-sharing"):
        raise LinkResolutionError("视频分享链接缺少 video_id，或链接已经失效")
    raise LinkResolutionError("分享页中没有找到可下载的公开图片或视频")

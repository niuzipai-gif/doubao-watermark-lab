"""Resolve public Doubao share links to original media downloads.

This module is intentionally separate from the local watermark-cleaning
pipeline. It only handles public share pages and never calls clean-image or
clean-video.
"""

import base64
import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAX_RESPONSE_BYTES = int(os.environ.get("DOUBAO_LINK_MAX_BYTES", str(300 * 1024 * 1024)))
MAX_JSON_BYTES = 8 * 1024 * 1024
DOUBAO_API = "https://www.doubao.com/samantha/media/get_play_info"
TRANSLATE_PROXY_HOST = "www-doubao-com.translate.goog"
DOUBAO_HOST_SUFFIX = ".doubao.com"
MEDIA_HOST_SUFFIXES = (
    ".doubao.com",
    ".byteimg.com",
    ".ibytedimg.com",
    ".bytecdn.cn",
    ".douyin.com",
    ".snssdk.com",
)
FPLAY_KDF_SALT = "TdTC5rgxYgkOUrPHpnM7pByyRiuCmrWKGWs521cXdST0m69/COjWjSanLjfBqVovHwWlGJKu8pSXMrYqOKrdWA=="
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
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.doubao.com/",
    }
    request = urllib.request.Request(url, headers=headers)
    data, _ = _read_response(request, MAX_JSON_BYTES)
    direct_text = data.decode("utf-8", errors="replace")
    parsed = urlparse(url)
    if not parsed.path.startswith("/thread/"):
        return direct_text

    proxy_query = parse_qs(parsed.query, keep_blank_values=True)
    proxy_query.update(
        {
            "_x_tr_sl": ["auto"],
            "_x_tr_tl": ["en"],
            "_x_tr_hl": ["en"],
        }
    )
    proxy_urls = (
        f"https://{TRANSLATE_PROXY_HOST}{parsed.path}?{urlencode(proxy_query, doseq=True)}",
        "https://translate.google.com/translate?" + urlencode({"sl": "auto", "tl": "en", "u": url}),
    )
    for proxy_url in proxy_urls:
        try:
            proxy_request = urllib.request.Request(proxy_url, headers=headers)
            proxy_data, _ = _read_response(proxy_request, MAX_JSON_BYTES)
            proxy_text = proxy_data.decode("utf-8", errors="replace")
            if "fallback_api" in proxy_text or "image_ori_raw" in proxy_text:
                return proxy_text
        except LinkResolutionError:
            continue
    return direct_text


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
    for _ in range(3):
        decoded = decoded.replace("\\\\", "\\")
    patterns = (
        r'\\?"vid\\?"\s*:\s*\\?"([^"\\]+)',
        r'&quot;vid& quot;\s*:\s*&quot;([^&]+)',
        r'video_id=([^&"\\]+)',
        r'\b(?:vid|video_id)\b[^A-Za-z0-9]{0,40}(v[0-9a-z_-]{12,})',
    )
    for pattern in patterns:
        keys.extend(re.findall(pattern, decoded, flags=re.IGNORECASE))
    return list(dict.fromkeys(item for item in keys if item))


def _extract_fallback_apis(raw_html: str) -> list[str]:
    decoded = html.unescape(raw_html).replace("&amp;", "&")
    for _ in range(4):
        decoded = (
            decoded.replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\/", "/")
            .replace("\\\\", "\\")
            .replace('\\"', '"')
        )
    patterns = (
        r'fallback_api\s*"?\s*:\s*"(https?://[^"\\]+)',
        r'fallback_api\s*:\s*(https?://[^"\\]+)',
    )
    urls = []
    for pattern in patterns:
        urls.extend(re.findall(pattern, decoded, flags=re.IGNORECASE))
    return list(
        dict.fromkeys(
            item
            for item in urls
            if _host_allowed(urlparse(item).hostname or "", MEDIA_HOST_SUFFIXES)
        )
    )


def _extract_video_keys_from_fallback_apis(fallback_apis: list[str]) -> list[str]:
    keys = []
    for fallback_api in fallback_apis:
        for segment in urlparse(fallback_api).path.split("/"):
            if re.fullmatch(r"v[0-9a-z_-]{12,}", segment, flags=re.IGNORECASE):
                keys.append(segment)
    return list(dict.fromkeys(keys))


def _fetch_get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.doubao.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    data, _ = _read_response(request, MAX_JSON_BYTES)
    try:
        result = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise LinkResolutionError("豆包视频接口返回的数据格式异常") from error
    if not isinstance(result, dict):
        raise LinkResolutionError("豆包视频接口没有返回有效数据")
    return result


def _decode_base64_urlsafe(value: str) -> bytes:
    normalized = str(value or "").replace("-", "+").replace("_", "/")
    normalized += "=" * ((4 - len(normalized) % 4) % 4)
    try:
        return base64.b64decode(normalized, validate=False)
    except (TypeError, ValueError) as error:
        raise LinkResolutionError("豆包视频地址编码异常") from error


def _unwatermarked_fallback_url(fallback_api: str) -> str:
    parsed = urlparse(fallback_api)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.pop("force_fids", None)
    query.pop("logo_type", None)
    query["codec_type"] = ["1"]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def _decrypt_fplay_url(encoded_url: str, key_seed: str) -> str:
    encrypted = _decode_base64_urlsafe(encoded_url)
    seed = _decode_base64_urlsafe(key_seed)
    if len(encrypted) <= 4 or not seed:
        raise LinkResolutionError("豆包视频加密地址不完整")
    first_hash = hashlib.sha512(seed).digest()
    salt = _decode_base64_urlsafe(FPLAY_KDF_SALT)
    derived = hashlib.sha512(first_hash + salt).digest()
    cipher = Cipher(algorithms.AES(derived[:16]), modes.CBC(derived[16:32]))
    decryptor = cipher.decryptor()
    plain = decryptor.update(encrypted[4:]) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    media_url = plain.decode("utf-8", errors="strict").strip()
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname or "", MEDIA_HOST_SUFFIXES):
        raise LinkResolutionError("豆包解密后的视频地址不受支持")
    return media_url


def _fallback_video_urls(fallback_api: str) -> list[str]:
    result = _fetch_get_json(_unwatermarked_fallback_url(fallback_api))
    data = (result.get("video_info") or {}).get("data") or {}
    key_seed = data.get("key_seed")
    video_list = data.get("video_list") or {}
    if not key_seed or not isinstance(video_list, dict):
        raise LinkResolutionError("豆包没有返回可解密的视频地址")
    urls = []
    preferred_items = sorted(video_list.items(), key=lambda item: (item[0] != "video_1", item[0]))
    for _, item in preferred_items:
        if not isinstance(item, dict):
            continue
        for field in ("main_url", "backup_url_1"):
            encoded_url = item.get(field)
            if not encoded_url:
                continue
            try:
                urls.append(_decrypt_fplay_url(encoded_url, key_seed))
            except (LinkResolutionError, ValueError):
                continue
    if not urls:
        raise LinkResolutionError("豆包返回的视频地址无法解密")
    return list(dict.fromkeys(urls))


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
    fallback_apis = _extract_fallback_apis(page_html) if page_html else []
    fallback_errors = []
    for fallback_api in fallback_apis:
        try:
            for media_url in _fallback_video_urls(fallback_api):
                return _download_media(media_url, "video")
        except LinkResolutionError as error:
            fallback_errors.append(str(error))
            continue
    ssr_images, ssr_video_keys = _parse_thread_ssr(page_html) if page_html else ([], [])
    video_keys = list(
        dict.fromkeys(
            _extract_video_keys_from_fallback_apis(fallback_apis)
            + ssr_video_keys
            + _extract_video_keys(page_html, parsed)
        )
    )
    video_errors = []
    for key in video_keys:
        try:
            return _download_media(_play_info(key)["url"], "video")
        except LinkResolutionError as error:
            video_errors.append(str(error))
            continue
    image_urls = list(dict.fromkeys(ssr_images + _extract_image_urls(page_html)))
    if image_urls:
        return _download_media(image_urls[0], "image")
    if parsed.path.startswith("/video-sharing"):
        raise LinkResolutionError("视频分享链接缺少 video_id，或链接已经失效")
    diagnostics = [f"fallback={len(fallback_apis)}", f"video_id={len(video_keys)}", f"image={len(image_urls)}"]
    if fallback_errors:
        diagnostics.append(f"fallback_error={fallback_errors[0]}")
    if video_errors:
        diagnostics.append(f"video_error={video_errors[0]}")
    raise LinkResolutionError("分享页中没有找到可下载的公开图片或视频（" + ", ".join(diagnostics) + "）")

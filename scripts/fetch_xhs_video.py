#!/usr/bin/env python3
"""Fetch the public mobile page and MP4 for a user-supplied Xiaohongshu link."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import platform
import re
import shutil
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse, urlunparse
from urllib.request import Request, urlopen


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
PAGE_HOSTS = {
    "xhslink.cn", "www.xhslink.cn", "xhslink.com", "www.xhslink.com",
    "xiaohongshu.com", "www.xiaohongshu.com", "m.xiaohongshu.com",
}


class ReadError(Exception):
    pass


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = ""

    def handle_data(self, data):
        if self.current is not None:
            self.current += data

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append(self.current)
            self.current = None


def normalize_page_url(url):
    """Unwrap a login redirect while retaining the original share parameters."""
    parsed = urlparse(url)
    target = parse_qs(parsed.query).get("redirectPath", [url])[0]
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in PAGE_HOSTS:
        raise ReadError("请提供小红书的公开笔记或分享链接。")
    return urlunparse(parsed._replace(scheme="https"))


def note_id_from_url(url):
    match = re.search(r"/(?:discovery/item|explore)/([0-9a-f]{24})(?:/|$)", urlparse(url).path)
    return match.group(1) if match else None


def json_literals(text):
    """Replace JS undefined outside strings; never execute page JavaScript."""
    pattern = r'"(?:\\.|[^"\\])*"|\bundefined\b'
    return re.sub(pattern, lambda m: "null" if m.group() == "undefined" else m.group(), text)


def extract_note(html, expected_id=None):
    parser = ScriptParser()
    parser.feed(html)
    states = []
    for name in ("__SETUP_SERVER_STATE__", "__INITIAL_STATE__"):
        for script in parser.scripts:
            match = re.search(r"window\." + name + r"\s*=\s*", script)
            if match:
                try:
                    state, _ = json.JSONDecoder().raw_decode(json_literals(script[match.end():]))
                    states.append(state)
                except json.JSONDecodeError:
                    continue

    def walk(value):
        if isinstance(value, dict):
            if value.get("noteId") and isinstance(value.get("video"), dict) and "title" in value:
                yield value
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    candidates = {}
    for state in states:
        for note in walk(state):
            if expected_id is None or note["noteId"] == expected_id:
                candidates.setdefault(note["noteId"], note)
    if len(candidates) != 1:
        raise ReadError("没有读到唯一匹配的目标视频数据；页面可能要求登录或结构已变化。")
    return next(iter(candidates.values()))


def media_urls(note):
    streams = note
    for key in ("video", "media", "stream"):
        streams = streams.get(key) if isinstance(streams, dict) else None
    if not isinstance(streams, dict):
        raise ReadError("已读取笔记，但没有发现支持的公开视频 MP4 地址。")
    urls = []
    for codec in ("h264", "h265", "av1", "h266"):
        variants = streams.get(codec)
        if not isinstance(variants, list):
            continue
        for stream in variants:
            if not isinstance(stream, dict):
                continue
            backups = stream.get("backupUrls")
            backups = backups if isinstance(backups, list) else []
            for url in [stream.get("masterUrl"), *backups]:
                if not isinstance(url, str):
                    continue
                parsed = urlparse(url)
                host = parsed.hostname or ""
                if parsed.scheme in ("http", "https") and host.endswith(".xhscdn.com"):
                    url = urlunparse(parsed._replace(scheme="https"))
                    if url not in urls:
                        urls.append(url)
    if not urls:
        raise ReadError("已读取笔记，但没有发现支持的公开视频 MP4 地址。")
    return urls[:3]


def request(url, timeout):
    req = Request(url, headers={"User-Agent": MOBILE_UA, "Referer": "https://www.xiaohongshu.com/"})
    return urlopen(req, timeout=timeout)


def fetch_note(url, output, timeout):
    target = normalize_page_url(url)
    attempted = set()
    last_error = None
    for attempt in range(1, 4):
        if target in attempted:
            break
        attempted.add(target)
        with request(target, timeout) as response:
            final = response.geturl()
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ReadError("页面过大，停止读取。请改用可用浏览器读取该笔记。")
        html = raw.decode("utf-8", errors="replace")
        (output / f"page-{attempt}.html").write_text(html, encoding="utf-8")
        resolved = normalize_page_url(final)
        expected_id = note_id_from_url(resolved) or note_id_from_url(target)
        try:
            return extract_note(html, expected_id)
        except ReadError as error:
            last_error = error
        if resolved not in attempted:
            target = resolved
        else:
            parsed = urlparse(resolved)
            alternate = parsed.path.replace("/explore/", "/discovery/item/", 1)
            if alternate == parsed.path:
                break
            target = urlunparse(parsed._replace(path=alternate))
    raise last_error or ReadError("没有取得视频正文。")


def download_video(urls, output, timeout):
    destination = output / "video.mp4"
    temporary = output / "video.mp4.part"
    last_status = None
    for url in urls:
        try:
            with request(url, timeout) as response:
                first = response.read(4096)
                if first[4:8] != b"ftyp":
                    raise ReadError("媒体请求未返回 MP4，不能把登录页或错误页当作视频。")
                with temporary.open("wb") as stream:
                    stream.write(first)
                    shutil.copyfileobj(response, stream)
            temporary.replace(destination)
            return destination
        except HTTPError as error:
            last_status = f"HTTP {error.code}"
        except (URLError, TimeoutError, ReadError) as error:
            last_status = type(error).__name__
        finally:
            temporary.unlink(missing_ok=True)
    raise ReadError(f"视频下载失败（{last_status}）。请重新读取分享页获取新地址，或用可用浏览器播放核对。")


def environment():
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "network": "not_tested",
        "image_reading": "requires_agent_image_tool_or_ocr",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(environment(), ensure_ascii=False, indent=2))
        return 0
    if not args.url:
        parser.error("需要分享链接，或使用 --check 检查环境")
    if args.timeout <= 0:
        parser.error("timeout 必须大于 0")
    try:
        normalize_page_url(args.url)
        digest = hashlib.sha256(args.url.encode()).hexdigest()[:12]
        output = (args.output or Path("sources") / f"xhs-{digest}").resolve()
        output.mkdir(parents=True, exist_ok=True)
        note = fetch_note(args.url, output, args.timeout)
        (output / "note-data.json").write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
        video = download_video(media_urls(note), output, args.timeout)
        result = {
            "source_url": args.url,
            "note_id": note["noteId"],
            "title": note.get("title", ""),
            "description": note.get("desc", ""),
            "video_path": str(video),
            "size_bytes": video.stat().st_size,
            "method": "public_mobile_page_state",
        }
        (output / "source.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("note_id", "title", "video_path", "size_bytes")}, ensure_ascii=False, indent=2))
        return 0
    except HTTPError as error:
        print(f"读取失败：HTTP {error.code}；检查分享链接、网络和登录要求。", file=sys.stderr)
    except URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            print("证书验证失败：检查当前 Python 的证书配置和代理证书；不要关闭 HTTPS 证书验证。", file=sys.stderr)
        else:
            print("网络请求失败：检查终端联网权限、代理和连接；不要把网络错误判为笔记失效。", file=sys.stderr)
    except TimeoutError:
        print("网络请求失败：检查终端联网权限、代理和连接；不要把网络错误判为笔记失效。", file=sys.stderr)
    except (ReadError, OSError) as error:
        print(f"读取失败：{error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

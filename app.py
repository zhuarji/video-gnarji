#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoGrab —— 网页版视频下载工具
后端：FastAPI + yt-dlp

用法：
    python3 app.py                 # 默认监听 0.0.0.0:8080
    python3 app.py --port 3000
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html as html_mod
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# PyInstaller 打包后（frozen）：可写文件放 exe 旁边，静态资源在解包临时目录里
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ASSET_DIR = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else BASE_DIR

# 所有运行时产物（下载文件、ffmpeg、临时文件）统一放在程序所在目录：
#   安装器版 -> 安装文件夹内（如 X:\VideoArji\downloads）
#   便携 exe -> exe 旁边；脚本运行 -> 脚本旁边
# 可用环境变量 VIDEOGRAB_HOME 覆盖到任意位置
if os.environ.get("VIDEOGRAB_HOME"):
    DATA_DIR = Path(os.environ["VIDEOGRAB_HOME"]).expanduser()
else:
    DATA_DIR = BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="VideoGrab", docs_url=None, redoc_url=None)

# 单个下载任务的状态表：task_id -> {"loop", "queue", "status", ...}
TASKS: dict[str, dict] = {}

executor = ThreadPoolExecutor(max_workers=4)


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
# 只匹配 URL 里的合法 ASCII 字符：分享文案里的中文、emoji、全角标点会自然截断
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE)
URL_TRAIL = "，。、；：！？）】》”’.,;:!?)]}>'\""


def extract_url(text: str) -> str:
    """从「复制分享」的整段文案里提取真正的链接

    抖音/快手点「复制链接」拿到的往往是一大段话，例如：
        6.12 复制打开抖音，看看【xxx的作品】... https://v.douyin.com/iRNBho6u/ 复制此链接...
    直接当链接解析必然失败，这里把里面的 URL 抠出来。
    """
    text = (text or "").strip()
    if not text:
        return ""
    m = URL_RE.search(text)
    if not m:
        return ""
    return m.group(0).rstrip(URL_TRAIL)


def looks_like_url(text: str) -> bool:
    return bool(extract_url(text))


def human_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return "-"


def human_duration(s) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "-"
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ----------------------------------------------------------------------------
# ffmpeg 自动补齐（打包版 Windows：首次运行自动下载 ffmpeg.exe 到程序目录）
# ----------------------------------------------------------------------------
# npmmirror（淘宝 npm 镜像）国内速度最优；后两个为 GitHub 官方源及其加速镜像
FFMPEG_SOURCES = [
    "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64",
    "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0/ffmpeg-win32-x64",
    "https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
]


def _download(src: str, dest: Path, tmp: Path) -> None:
    """带百分比进度地下载 src；.zip 结尾的源会顺带解出 ffmpeg.exe。"""
    import urllib.request

    last = [0]

    def report(n: int, block: int, total: int) -> None:
        if total <= 0:
            return
        pct = min(100, int(n * block * 100 / total))
        if pct - last[0] >= 10:
            last[0] = pct
            print(f"    {pct}%  ({n * block // 1048576}/{total // 1048576} MB)")

    urllib.request.urlretrieve(src, tmp, reporthook=report)
    if src.endswith(".zip"):
        with zipfile.ZipFile(tmp) as zf:
            member = next(n for n in zf.namelist() if n.endswith("/ffmpeg.exe"))
            with zf.open(member) as fr, open(dest, "wb") as fw:
                shutil.copyfileobj(fr, fw, 1024 * 1024)
    else:
        shutil.move(str(tmp), dest)


def ffmpeg_path():
    """返回可用的 ffmpeg 可执行文件路径；优先系统 PATH，其次主文件夹"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    for cand in (DATA_DIR / "ffmpeg.exe", BASE_DIR / "ffmpeg.exe"):
        if cand.exists():
            return str(cand)
    return None


def ensure_ffmpeg() -> bool:
    """检测 ffmpeg；Windows 打包版缺失时自动下载到主文件夹。返回是否可用。"""
    if ffmpeg_path():
        return True
    if os.name != "nt":
        return False
    print(f"  首次运行：正在自动下载 ffmpeg（约 80MB）到 {DATA_DIR} …")
    tmp = DATA_DIR / "ffmpeg.dl.tmp"
    for src in FFMPEG_SOURCES:
        try:
            _download(src, DATA_DIR / "ffmpeg.exe", tmp)
            print("  ✓ ffmpeg 就绪")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 此源下载失败（{type(e).__name__}），尝试备用源…")
            tmp.unlink(missing_ok=True)
    print(f"  ✗ 自动下载失败：请手动下载 ffmpeg.exe 放到 {DATA_DIR} 后重新打开")
    return False


def base_opts() -> dict:
    """所有 yt-dlp 调用的公共参数"""
    ffmpeg = ffmpeg_path()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        "merge_output_format": "mp4",
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    return opts


# ----------------------------------------------------------------------------
# 短视频分享页解析（抖音 / 快手）
# ----------------------------------------------------------------------------
# 为什么单独做：yt-dlp 的抖音解析器依赖网页接口，经常被风控拦下；快手则完全
# 没有官方支持。这里改走「分享页」——短链跳转后的移动端页面里本身就嵌着视频
# 直链（JSON），不需要签名、不需要登录，能拿到无水印的原画质 MP4。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)

SHORT_VIDEO_HOSTS = ("douyin.com", "iesdouyin.com", "kuaishou.com", "chenzhongtech.com", "gifshow.com")


def is_short_video_site(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(h in host for h in SHORT_VIDEO_HOSTS)


def _http_get(url: str, headers: dict | None = None, timeout: int = 20, limit: int = 6 * 1024 * 1024):
    """GET 一个页面，返回 (最终地址, 文本)。自动跟随跳转、自动解 gzip。"""
    h = {
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(limit)
        final = r.geturl()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
    return final, raw.decode("utf-8", "replace")


def _json_block(text: str, marker: str, span: int = 8 * 1024 * 1024):
    """从 HTML 里抠出 `marker = {...}` 的 JSON（按花括号配对，正确处理字符串转义）"""
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth, in_str, esc = 0, False, False
    end = min(len(text), j + span)
    for k in range(j, end):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[j : k + 1])
                    except Exception:  # noqa: BLE001
                        return None
    return None


def _deep_values(obj, keys: set):
    """递归收集所有名为 keys 中任意一个的字段值（页面结构各版本不一，靠它兜）"""
    found = []

    def walk(o, depth=0):
        if depth > 40:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys:
                    found.append(v)
                walk(v, depth + 1)
        elif isinstance(o, list):
            for x in o:
                walk(x, depth + 1)

    walk(obj)
    return found


def _first_url(v) -> str:
    """从 str / list / dict 里挖出第一个 http(s) 地址"""
    if isinstance(v, str):
        return v if v.startswith("http") else ""
    if isinstance(v, list):
        for x in v:
            u = _first_url(x)
            if u:
                return u
    elif isinstance(v, dict):
        for k in ("url_list", "url", "src", "srcNoMark", "playUrl", "mainUrl", "urls"):
            if k in v:
                u = _first_url(v[k])
                if u:
                    return u
        for x in v.values():
            u = _first_url(x)
            if u:
                return u
    return ""


MP4_RE = re.compile(r"https?://[^\"'\s<>]{5,600}?\.mp4(?:\?[^\"'\s<>]{0,600})?", re.IGNORECASE)


def _first_mp4(html_text: str) -> str:
    """终极兜底：页面 HTML 里出现的第一个 mp4 直链"""
    unescaped = html_text.replace("\\u002F", "/").replace("\\/", "/")
    m = MP4_RE.search(unescaped)
    return m.group(0) if m else ""


def _html_title(html_text: str, suffix: str = "") -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.IGNORECASE)
    t = html_mod.unescape(m.group(1)).strip() if m else ""
    if suffix:
        t = re.sub(rf"\s*[-–|]\s*{suffix}.*$", "", t)
    t = re.sub(r"\s+", " ", t)
    return t[:120] or "未命名视频"


def _douyin_id(url: str) -> str:
    m = re.search(r"/(?:share/)?video/(\d{6,})", url) or re.search(
        r"[?&](?:modal_id|vid|aweme_id)=(\d{6,})", url
    )
    return m.group(1) if m else ""


def _douyin_from_item(item: dict, page: str, vid: str) -> dict | None:
    video = item.get("video") or {}
    mp4 = ""
    for key in ("play_addr", "play_addr_h264", "play_addr_265", "play_addr_lowbr"):
        mp4 = _first_url(video.get(key) or {})
        if mp4:
            break
    if not mp4:
        mp4 = _first_url(video.get("download_addr") or {})
    if not mp4:
        mp4 = _first_mp4(page)
    if not mp4:
        return None
    # playwm 是带水印版本，换成 play 即无水印
    mp4 = mp4.replace("/playwm/", "/play/").replace("playwm", "play")

    duration = video.get("duration")
    return {
        "site": "抖音",
        "id": str(item.get("aweme_id") or vid or "douyin"),
        "title": (item.get("desc") or "").strip() or _html_title(page, "抖音"),
        "uploader": ((item.get("author") or {}).get("nickname") or "").strip(),
        "duration": round(duration / 1000) if isinstance(duration, (int, float)) and duration else None,
        "thumbnail": _first_url(video.get("cover") or video.get("origin_cover") or {}),
        "url": mp4,
        "referer": "https://www.douyin.com/",
    }


def parse_douyin(url: str, timeout: int = 20) -> dict | None:
    """抖音：短链跳转 → 分享页 _ROUTER_DATA → play_addr（无水印）

    依次尝试多个页面：先分享页，再短链跳转后的原页面。
    平台偶尔会返回风控/空数据页，所以任何一个页面解析不出内容就换下一个。
    """
    final, page0 = _http_get(url, timeout=timeout)
    vid = _douyin_id(final) or _douyin_id(url)

    pages: list[tuple[str, str]] = []
    if vid:
        try:
            f2, p2 = _http_get(
                f"https://www.iesdouyin.com/share/video/{vid}/",
                headers={"Referer": "https://www.douyin.com/"},
                timeout=timeout,
            )
            if "_ROUTER_DATA" in p2 or "item_list" in p2:
                pages.append((f2, p2))
        except Exception:  # noqa: BLE001
            pass
    pages.append((final, page0))

    for f_, page in pages:
        data = _json_block(page, "window._ROUTER_DATA") or _json_block(page, "_ROUTER_DATA")
        item = None
        if data:
            for v in _deep_values(data, {"item_list"}):
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    item = v[0]
                    break
            if item is None:
                for v in _deep_values(data, {"aweme_detail", "aweme_info"}):
                    if isinstance(v, dict) and v.get("video"):
                        item = v
                        break
        if item:
            got = _douyin_from_item(item, page, vid)
            if got:
                return got
        mp4 = _first_mp4(page)
        if mp4:
            return {
                "site": "抖音",
                "id": vid or "douyin",
                "title": _html_title(page, "抖音"),
                "uploader": "",
                "duration": None,
                "thumbnail": "",
                "url": mp4.replace("playwm", "play"),
                "referer": "https://www.douyin.com/",
            }
    return None


def _kuaishou_id(url: str) -> str:
    m = re.search(
        r"/(?:short-video|video|photo|fw/photo|profile/[^/]+)/([A-Za-z0-9_\-]{6,})", url
    )
    return m.group(1) if m else ""


def parse_kuaishou(url: str, timeout: int = 20) -> dict | None:
    """快手：短链跳转 → 移动版页面 INIT_STATE → mainMvUrls（无水印）

    同样按候选页面顺序尝试，解析不出来就换下一个（含短链原始页面）。
    """
    final, page0 = _http_get(url, timeout=timeout)
    pid = _kuaishou_id(final) or _kuaishou_id(url)

    pages: list[tuple[str, str]] = []
    if pid:
        candidates = (
            f"https://v.m.chenzhongtech.com/fw/photo/{pid}",
            f"https://www.kuaishou.com/short-video/{pid}",
        )
        for cand in candidates:
            try:
                f2, h2 = _http_get(cand, headers={"Referer": "https://www.kuaishou.com/"}, timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            if "INIT_STATE" in h2 or "mainMvUrls" in h2:
                pages.append((f2, h2))
    pages.append((final, page0))

    for f_, page in pages:
        data = (
            _json_block(page, "window.INIT_STATE")
            or _json_block(page, "INIT_STATE")
            or _json_block(page, "window.__APOLLO_STATE__")
        )
        mp4, caption, cover = "", "", ""
        if data:
            for v in _deep_values(data, {"mainMvUrls", "mainMvUrl", "playUrls", "playUrl", "srcNoMark"}):
                mp4 = _first_url(v)
                if mp4:
                    break
            caps = [v for v in _deep_values(data, {"caption"}) if isinstance(v, str) and v.strip()]
            if caps:
                caption = caps[0]
            covers = [v for v in _deep_values(data, {"coverUrls", "coverUrl", "poster"}) if _first_url(v)]
            if covers:
                cover = _first_url(covers[0])

        if not mp4:
            mp4 = _first_mp4(page)
        if not mp4:
            continue

        return {
            "site": "快手",
            "id": pid or "kuaishou",
            "title": (caption or "").strip() or _html_title(page, "快手"),
            "uploader": "",
            "duration": None,
            "thumbnail": cover,
            "url": mp4,
            "referer": "https://www.kuaishou.com/",
        }
    return None


def native_extract(url: str) -> dict | None:
    """抖音/快手分享页直连解析；其他站点返回 None（继续走 yt-dlp）"""
    low = url.lower()
    try:
        if "douyin.com" in low:
            return parse_douyin(url)
        if any(k in low for k in ("kuaishou.com", "chenzhongtech.com", "gifshow.com")):
            return parse_kuaishou(url)
    except Exception as e:  # noqa: BLE001
        print(f"  [native] {type(e).__name__}: {e}")
    return None


def resolve_canonical(url: str) -> str:
    """把分享短链换成 yt-dlp 认得的规范地址（抖音只认 www.douyin.com/video/<id>）"""
    if "douyin.com" not in url.lower():
        return url
    vid = _douyin_id(url)
    if not vid:
        try:
            final, _ = _http_get(url, timeout=12)
            vid = _douyin_id(final)
        except Exception:  # noqa: BLE001
            pass
    return f"https://www.douyin.com/video/{vid}" if vid else url


# 可从中自动读取登录态的浏览器（yt-dlp --cookies-from-browser 支持的名称）
BROWSER_NAMES = {"chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium", "safari", "whale"}


def apply_cookies(opts: dict, cookies_text: str, browser: str) -> str | None:
    """给 yt-dlp 挂上 Cookie：优先用户粘贴的文本，其次自动读本机浏览器

    抖音的网页接口需要带签名的 Cookie（s_v_web_id / msToken / ttwid），
    纯 HTTP 抓取拿不到，所以「自动读取本机浏览器」是它最省事的可用路径。
    返回临时 cookie 文件路径（调用方负责清理），无则 None。
    """
    cookies_text = (cookies_text or "").strip()
    if cookies_text:
        path = DOWNLOAD_DIR / f".cookies_{uuid.uuid4().hex}.txt"
        path.write_text(cookies_text, encoding="utf-8")
        opts["cookiefile"] = str(path)
        return str(path)
    b = (browser or "").strip().lower()
    if b in BROWSER_NAMES:
        opts["cookiesfrombrowser"] = (b,)
    return None


# 分享页解析结果缓存：/api/info 里解析好，/api/download 按链接取用
NATIVE_CACHE: dict[str, dict] = {}
NATIVE_TTL = 1800


def cache_native(url: str, data: dict) -> None:
    NATIVE_CACHE[url] = {"t": time.time(), "data": data}
    # 顺手清掉过期项，避免长跑进程越攒越多
    now = time.time()
    for k in [k for k, v in NATIVE_CACHE.items() if now - v["t"] > NATIVE_TTL]:
        NATIVE_CACHE.pop(k, None)


def get_native(url: str) -> dict | None:
    item = NATIVE_CACHE.get(url)
    if not item or time.time() - item["t"] > NATIVE_TTL:
        return None
    return item["data"]


# ----------------------------------------------------------------------------
# 解析视频信息
# ----------------------------------------------------------------------------
@app.get("/api/status")
async def api_status():
    """环境自检：ffmpeg 是否可用直接影响「高清流合并」和「MP3 转换」"""
    try:
        version = yt_dlp.version.__version__
    except Exception:  # noqa: BLE001
        version = "unknown"
    return {
        "ffmpeg": ffmpeg_path() is not None,
        "ytdlp": version,
        "data_dir": str(DATA_DIR),
        "download_dir": str(DOWNLOAD_DIR),
    }


@app.post("/api/info")
async def api_info(payload: dict):
    raw = (payload.get("url") or "").strip()
    if not raw:
        raise HTTPException(400, "请提供视频链接")
    # 抖音/快手「复制链接」常带着标题文案一起复制，这里先把真正的 URL 抠出来
    url = extract_url(raw)
    if not url:
        raise HTTPException(400, "没有识别到有效链接。请粘贴包含 http:// 或 https:// 地址的内容")

    pw = (payload.get("password") or "").strip()
    want_playlist = not bool(payload.get("single", True))

    def build(ignore: bool):
        o = base_opts()
        o.update(
            {
                "skip_download": True,
                "noplaylist": not want_playlist,
                "ignoreerrors": ignore,
            }
        )
        if pw:
            o["videopassword"] = pw
        return o

    # 单视频：不忽略错误，这样失败时能拿到 yt-dlp 的具体原因
    # （ignoreerrors=True 会让它静默返回 None，用户只能看到「无法解析该链接」）
    opts = build(ignore=want_playlist)
    if pw:
        opts["videopassword"] = pw

    proxy = (payload.get("proxy") or "").strip()
    if proxy:
        opts["proxy"] = proxy

    cookies = (payload.get("cookies") or "").strip()
    browser = (payload.get("browser") or "").strip()
    _tmp = apply_cookies(opts, cookies, browser)
    cookiefile = Path(_tmp) if _tmp else None

    loop = asyncio.get_event_loop()

    # 抖音/快手：先走分享页直连（免登录、无水印、成功率高），失败再交给 yt-dlp
    if is_short_video_site(url):
        native = await loop.run_in_executor(executor, native_extract, url)
        if native:
            cache_native(url, native)
            ff = ffmpeg_path() is not None
            fmts = [
                {
                    "id": "native",
                    "label": "原画质（无水印）",
                    "note": f"来自{native['site']}分享页，直接保存 MP4",
                    "kind": "video",
                    "recommend": True,
                }
            ]
            if ff:
                fmts.append(
                    {
                        "id": "native_mp3",
                        "label": "仅音频（MP3 320k）",
                        "note": "下载后自动转码",
                        "kind": "audio",
                    }
                )
            return {
                "playlist": False,
                "ffmpeg": ff,
                "native": True,
                "url": url,
                "id": native["id"],
                "title": native["title"],
                "uploader": native["uploader"],
                "duration": native["duration"],
                "duration_h": human_duration(native["duration"]),
                "view_count": None,
                "thumbnail": native["thumbnail"],
                "extractor": f"{native['site']}（分享页）",
                "description": "",
                "formats": fmts,
                "raw_formats": [],
                "subtitles": [],
            }

    # 分享页直连失败后：把抖音短链换成 www.douyin.com/video/<id>，
    # 否则 yt-dlp 不认 v.douyin.com 这种地址，会直接报「暂不支持该链接」
    if is_short_video_site(url):
        url = await loop.run_in_executor(executor, resolve_canonical, url)

    def extract(o):
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        info = await loop.run_in_executor(executor, extract, opts)
        if info is None and opts.get("ignoreerrors"):
            # 播放列表容错模式会静默失败，改用严格模式再跑一次，把真实原因抛出来
            info = await loop.run_in_executor(executor, extract, {**opts, "ignoreerrors": False})
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR:", "").strip()
        human = friendly_error(msg)
        tail = "（常见原因：网络不通、链接失效、需登录后查看或被平台风控拦截）"
        if "douyin" in url.lower():
            tail = (
                "（抖音的风控要求「带签名的 Cookie」：请到「高级选项 → 自动读取本机浏览器 Cookie」"
                "选 Chrome / Edge，或在浏览器里打开一次抖音后重试；也可手动粘贴 cookies.txt 内容）"
            )
        elif is_short_video_site(url):
            tail = "（快手解析失败：请确认链接是公开视频，或到「高级选项」粘贴 Cookie 后重试）"
        raise HTTPException(400, human if human != msg else f"{msg[-200:]}\n{tail}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"解析出错：{e}")
    finally:
        if cookiefile:
            cookiefile.unlink(missing_ok=True)

    if not info:
        raise HTTPException(
            400,
            "解析未返回任何内容。常见原因：① 链接不完整或已失效；"
            "② 平台需要登录（请到「高级选项」粘贴 Cookie）；③ 网络不通或需代理。",
        )

    has_ffmpeg = ffmpeg_path() is not None

    # 播放列表：直接展开为多个条目
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = [format_info(e, has_ffmpeg) for e in info["entries"] if e]
        return {
            "playlist": True,
            "ffmpeg": has_ffmpeg,
            "title": info.get("title") or "播放列表",
            "count": len(entries),
            "entries": entries,
        }

    return {"playlist": False, "url": url, **format_info(info, has_ffmpeg)}


def friendly_error(msg: str) -> str:
    """把 yt-dlp 的原始报错翻译成人话"""
    m = msg.lower()
    # 注意：状态码必须排在语义匹配之前，且关键词要足够精确
    # （曾踩坑：用 "age" 匹配会命中 webpage/message 里的字母，把 412 误报成年龄限制）
    table = [
        (
            "could not find",
            "没找到浏览器的 Cookie 数据库：请确认已安装并打开过该浏览器，"
            "或改用上方「Cookie 文本」手动粘贴",
        ),
        (
            "failed to decrypt",
            "浏览器 Cookie 解密失败（Chrome 新版加密）：请改用「Cookie 文本」手动粘贴",
        ),
        ("unsupported browser", "这个浏览器暂不支持自动读取，请改选 Chrome / Edge / Firefox"),
        ("http error 412", "平台风控拦截（HTTP 412）：未登录或对高频请求限流。建议粘贴 Cookie 后重试"),
        ("http error 403", "访问被拒绝（403）：可能需要 Cookie，或当前 IP 被限制"),
        ("http error 404", "链接不存在（404）：请检查链接是否复制完整、视频是否已删除"),
        ("http error 429", "请求过于频繁（429）：平台限流，请隔几分钟再试"),
        ("precondition failed", "平台风控拦截（412 Precondition Failed）：建议粘贴 Cookie 后重试"),
        ("video unavailable", "视频不存在或已被删除"),
        ("video is private", "这是私密视频，需要登录后访问，请粘贴 Cookie"),
        ("age restricted", "该视频存在年龄限制，需要登录验证"),
        ("age-restricted", "该视频存在年龄限制，需要登录验证"),
        ("confirm your age", "该视频存在年龄限制，需要登录验证"),
        ("members-only", "这是会员专属内容，本工具不会绕过付费/会员限制"),
        ("this video is not available", "该视频不可用（可能已删除或受地区限制）"),
        ("geo restricted", "该视频受地区限制，可尝试填写代理地址"),
        ("sign in to confirm", "平台要求登录验证，请粘贴 Cookie"),
        ("this content isn't available", "内容不可用，可能需要登录权限"),
        ("timed out", "连接超时，可尝试填写代理地址"),
        ("tls/ssl", "网络被拦截（TLS 握手失败），请检查网络或配置代理"),
        ("unable to download webpage", "无法打开网页：网络不通，或被平台拦截"),
        ("unsupported url", "暂不支持该站点或链接格式"),
    ]
    for kw, human in table:
        if kw in m:
            return human
    return msg[-300:]


def build_format_list(info: dict, has_ffmpeg: bool = True) -> tuple[list[dict], list[dict]]:
    """把 yt-dlp 的原始格式表整理成给用户选的清爽列表

    没有 ffmpeg 时只能下载「自带音轨的复合流」——
    高清通常是视频流和音轨分开的，强行选它会在最后合并一步失败。
    所以这时只列出确定能成功的选项，而不是让用户点了才发现下不了。
    """
    formats = info.get("formats") or []

    def vid(f):
        return (f.get("vcodec") or "none") != "none"

    def aud(f):
        return (f.get("acodec") or "none") != "none"

    has_video = any(vid(f) for f in formats)
    has_audio = any(aud(f) for f in formats)
    heights = sorted({f.get("height") for f in formats if f.get("height") and vid(f)}, reverse=True)

    out: list[dict] = []

    def add(fid, label, note, kind, **extra):
        item = {"id": fid, "label": label, "note": note, "kind": kind}
        item.update(extra)
        out.append(item)

    top = heights[0] if heights else 0

    if has_ffmpeg:
        if has_video and has_audio:
            # 默认锁 1080p：直接用 best 会选中需要大会员的最高档（4K / 1080P高码率），
            # 非会员必然失败。视频本身没到 1080p 时，退到它实际的最高档。
            default_h = min(1080, top) if top else 1080
            add(
                f"bv*[height<={default_h}]+ba/b[height<={default_h}]",
                f"{default_h}p",  # 徽章已标「推荐」，标签不再重复
                "绝大多数账号可用（默认）",
                "video",
                recommend=True,
            )
            if top > 1080:
                add("bv*+ba/b", f"最高画质（{top}p）", "可能需要大会员/付费权限", "video")
        elif has_video:
            add("bestvideo", "仅视频（无声音轨）", "该源视频本身不含音轨", "video", recommend=True)

        for h in heights:
            if h < 240 or h not in (2160, 1440, 1080, 720, 480, 360):
                continue
            # 已由「最高画质」项和推荐项覆盖，避免重复
            if top > 1080 and h == top:
                continue
            if h == (min(1080, top) if top else 1080):
                continue
            add(f"bv*[height<={h}]+ba/b[height<={h}]", f"{h}p", f"{h}p", "video")

        # 去重（不同清晰度可能算出同一批）
        seen, uniq = set(), []
        for item in out:
            if item["id"] not in seen:
                seen.add(item["id"])
                uniq.append(item)
        out = uniq

        if has_audio:
            add("bestaudio", "仅音频（原声）", "保留原始音频编码", "audio")
            add("mp3", "仅音频（MP3 320k）", "自动转码，适合音乐/播客", "audio")
    else:
        # 无 ffmpeg：只给复合流（画面+声音在同一条流里，无需合并）
        combined = [f for f in formats if vid(f) and aud(f)]
        combo_heights = sorted({f.get("height") for f in combined if f.get("height")}, reverse=True)
        for h in combo_heights:
            add(
                f"best[vcodec!=none][acodec!=none][height<={h}]",
                f"{h}p（含音轨）",
                "无需 ffmpeg，可直接下载",
                "video",
                recommend=(h == combo_heights[0]),
            )
        if not out and has_video:
            add("bestvideo", "仅视频（无声音）", "缺少 ffmpeg，无法合并音轨", "video", recommend=True)
        if has_audio:
            add("bestaudio", "仅音频（原声）", "无需 ffmpeg", "audio")

    # 兜底：部分直链/小众站点不返回清晰度信息，此时至少给一个可点的选项
    if not out:
        out = [
            {
                "id": "best",
                "label": "原画质下载",
                "note": "该链接未提供清晰度选项，直接保存原文件",
                "kind": "video",
                "recommend": True,
            }
        ]
        if has_audio and has_ffmpeg:
            out.append({"id": "mp3", "label": "转为 MP3", "note": "仅提取音频", "kind": "audio"})

    # 原格式列表（高级用户）
    raw = []
    for f in formats:
        raw.append(
            {
                "format_id": f.get("format_id", ""),
                "ext": f.get("ext", ""),
                "resolution": f.get("resolution") or ("纯音频" if f.get("acodec") != "none" else "-"),
                "fps": f.get("fps"),
                "vcodec": (f.get("vcodec") or "")[:24],
                "acodec": (f.get("acodec") or "")[:24],
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "filesize_h": human_bytes(f.get("filesize") or f.get("filesize_approx")),
                "note": (f.get("format_note") or ""),
            }
        )
    raw = [r for r in raw if r["format_id"]]
    return out, raw  # type: ignore[return-value]


def format_info(info: dict, has_ffmpeg: bool = True) -> dict:
    fmts, raw = build_format_list(info, has_ffmpeg)
    thumb = info.get("thumbnail")
    return {
        "ffmpeg": has_ffmpeg,
        "id": info.get("id", ""),
        "title": info.get("title") or info.get("fulltitle") or "未知标题",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "duration_h": human_duration(info.get("duration")),
        "view_count": info.get("view_count"),
        "thumbnail": thumb,
        "extractor": info.get("extractor_key") or "",
        "description": (info.get("description") or "")[:300],
        "formats": fmts,
        "raw_formats": raw,
        "subtitles": sorted(set(list(info.get("subtitles") or {}) + list(info.get("automatic_captions") or {})))[:40],
    }


# ----------------------------------------------------------------------------
# 下载
# ----------------------------------------------------------------------------
@app.delete("/api/tasks/{task_id}")
async def cancel_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task["cancel"] = True
    return {"ok": True}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """轮询兜底：WebSocket 不可用时，前端改用它拉取进度"""
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"task_id": task_id, "finished": task["finished"], **task.get("state", {})}


def snapshot(task: dict, msg: dict) -> None:
    """保存任务最新状态快照 —— WebSocket 不可用时供 HTTP 轮询兜底"""
    task["state"] = msg


def attach_progress(opts: dict, task_id: str) -> None:
    def hook(d):
        task = TASKS.get(task_id)
        if not task:
            return
        q = task["queue"]
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "").strip().replace("%", "")
            try:
                pct_f = float(pct)
            except ValueError:
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct_f = (d.get("downloaded_bytes", 0) / total * 100) if total else 0.0
            msg = {
                "status": "downloading",
                "percent": round(pct_f, 1),
                "speed": d.get("_speed_str", "").strip(),
                "eta": d.get("_eta_str", "").strip(),
                "size": human_bytes(d.get("total_bytes") or d.get("total_bytes_estimate")),
                "downloaded": human_bytes(d.get("downloaded_bytes")),
                "frag": f"分片 {d.get('fragment_index', '?')}/{d.get('fragment_count', '?')}"
                if d.get("fragment_count")
                else "",
            }
            snapshot(task, msg)
            q.put_nowait(msg)
        elif d["status"] == "finished":
            msg = {"status": "processing", "percent": 100, "msg": "下载完成，正在合并/转码…"}
            snapshot(task, msg)
            q.put_nowait(msg)

    opts["progress_hooks"] = [hook]
    opts["logger"] = ForwardLogger(task_id)

class ForwardLogger:
    """适配器：把 yt-dlp 的日志调用转发到任务队列（yt-dlp 要求 Logger 风格对象）"""

    KEYWORDS = ("merg", "convert", "extractaudio", "ffmpeg", "subtitle", "post-process")
    NOISE = ("deleting original file", "pass -k to keep")

    def __init__(self, task_id: str):
        self.task_id = task_id

    def _push(self, msg):
        task = TASKS.get(self.task_id)
        if not task or not msg:
            return
        text = str(msg).strip()
        low = text.lower()
        if any(k in low for k in self.NOISE):
            return
        if any(k in low for k in self.KEYWORDS):
            # yt-dlp 的日志是英文，换成用户能看懂的说法
            friendly = "正在合并音视频…" if "merg" in low or "ffmpeg" in low else (
                "正在提取/转换音频…" if "extractaudio" in low or "convert" in low else (
                    "正在写入字幕…" if "subtitle" in low else "正在后期处理…")
            )
            msg = {"status": "processing", "msg": friendly}
            snapshot(task, msg)
            task["queue"].put_nowait(msg)

    def debug(self, msg=""):
        self._push(msg)

    def info(self, msg=""):
        self._push(msg)

    def warning(self, msg=""):
        self._push(msg)

    def error(self, msg=""):
        self._push(msg)

    def critical(self, msg=""):
        self._push(msg)


@app.websocket("/ws/{task_id}")
async def ws_progress(ws: WebSocket, task_id: str):
    await ws.accept()
    task = TASKS.get(task_id)
    if not task:
        await ws.send_json({"status": "error", "msg": "任务不存在"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    aq: asyncio.Queue = asyncio.Queue()

    def pump():
        """单个线程负责搬运，避免每次 wait_for 超时后残留阻塞线程"""
        q = task["queue"]
        while True:
            try:
                msg = q.get()
            except EOFError:
                return
            loop.call_soon_threadsafe(aq.put_nowait, msg)
            if msg.get("status") in ("done", "error", "cancelled"):
                return

    threading.Thread(target=pump, daemon=True).start()

    try:
        while True:
            try:
                msg = await asyncio.wait_for(aq.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # 心跳，顺便探测连接是否还活着
                try:
                    await ws.send_json({"status": "ping"})
                except Exception:  # noqa: BLE001
                    break
                continue
            await ws.send_json(msg)
            if msg.get("status") in ("done", "error", "cancelled"):
                break
    except WebSocketDisconnect:
        return
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def run_download(task_id: str, url: str, fmt: str, options: dict) -> None:
    task = TASKS[task_id]
    q = task["queue"]

    out_dir = DOWNLOAD_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    opts = base_opts()
    opts.update(
        {
            "format": fmt,
            "outtmpl": {"default": str(out_dir / "%(title).100s [%(id)s].%(ext)s")},
            "progress_hooks": [],
            "ignoreerrors": False,
            "noplaylist": options.get("single", True),
            "writethumbnail": bool(options.get("thumbnail", True)),
            "embedthumbnail": bool(options.get("thumbnail", True)),
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
        }
    )

    if options.get("proxy"):
        opts["proxy"] = options["proxy"]
    if options.get("password"):
        opts["videopassword"] = options["password"]
    if options.get("rate_limit"):
        opts["ratelimit"] = options["rate_limit"]

    # 字幕
    if options.get("subtitles"):
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = options["subtitles"]
        opts["embedsubtitles"] = bool(options.get("embed_subs", True))

    # MP3 转码
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ]

    _tmp = apply_cookies(opts, options.get("cookies"), options.get("browser"))
    cookiefile = Path(_tmp) if _tmp else None

    attach_progress(opts, task_id)

    class _Cancel(Exception):
        pass

    def check():
        if task.get("cancel"):
            raise _Cancel

    opts["progress_hooks"].append(lambda d: check())

    def finish(msg, put=True):
        snapshot(task, msg)
        task["finished"] = True
        if put:
            q.put_nowait(msg)

    def attempt(fmt_value: str, frag_concurrency: int):
        o = dict(opts)
        o["format"] = fmt_value
        o["concurrent_fragment_downloads"] = frag_concurrency
        with yt_dlp.YoutubeDL(o) as ydl:
            ydl.download([url])

    def note(msg):
        snapshot(task, {"status": "processing", "msg": msg})
        q.put_nowait({"status": "processing", "msg": msg})

    # mp3 靠后处理转码，format 已在 opts 里设好，沿用即可，不参与清晰度降级
    chain = [opts.get("format", fmt)] if fmt == "mp3" else [fmt] + fallback_chain(fmt)

    last_err = None
    try:
        for idx, cur in enumerate(chain):
            for frag in (4, 1):
                try:
                    attempt(cur, frag)
                    finish({"status": "done", "percent": 100})
                    last_err = None
                    break
                except _Cancel:
                    raise
                except yt_dlp.utils.DownloadError as e:
                    last_err = e
                    if frag == 4:
                        # 并发分片过高会触发部分站点风控，先降为单线程再试一次
                        note("降低并发重试…")
                        continue
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    break
            if last_err is None:
                break
            if idx < len(chain) - 1:
                note("当前清晰度不可用（可能需要会员权限），自动降级重试…")
        if last_err is not None:
            finish({"status": "error", "msg": download_error(last_err, url, options)})
    except _Cancel:
        finish({"status": "cancelled", "msg": "已取消"})
    finally:
        if cookiefile:
            cookiefile.unlink(missing_ok=True)
        task["finished"] = True


class _Cancelled(Exception):
    pass


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:100] or "video"


def parse_rate(v) -> int:
    """把 '2M' / '500K' 解析成 bytes/s；非法值返回 0（不限速）"""
    m = re.match(r"^\s*([\d.]+)\s*([KMG]?)", str(v or ""), re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]
    return int(n * mult)


def run_direct(task_id: str, page_url: str, fmt: str, options: dict) -> None:
    """分享页直链下载（抖音/快手）：直接 HTTP 拉 MP4，进度/取消与 yt-dlp 任务一致"""
    task = TASKS[task_id]
    q = task["queue"]

    def push(msg, final=False):
        snapshot(task, msg)
        q.put_nowait(msg)
        if final:
            task["finished"] = True

    info = get_native(page_url)
    if not info:
        push({"status": "error", "msg": "解析结果已过期，请重新点击「解析」后再下载"}, final=True)
        return

    out_dir = DOWNLOAD_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    title = safe_name(info.get("title") or "video")
    vid = safe_name(str(info.get("id") or task_id))
    mp4_path = out_dir / f"{title} [{vid}].mp4"

    headers = {
        "User-Agent": MOBILE_UA,
        "Referer": info.get("referer") or page_url,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    rate = parse_rate(options.get("rate_limit"))

    try:
        push({"status": "downloading", "percent": 0, "msg": "开始下载…"})
        req = urllib.request.Request(info["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r, open(mp4_path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            t0 = time.time()
            last_push = 0.0
            while True:
                if task.get("cancel"):
                    raise _Cancelled
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if rate:
                    expect = done / rate
                    spent = time.time() - t0
                    if expect > spent:
                        time.sleep(min(expect - spent, 0.5))
                now = time.time()
                if now - last_push >= 0.4:
                    last_push = now
                    elapsed = max(now - t0, 0.001)
                    speed = done / elapsed
                    eta = int((total - done) / speed) if total and speed > 0 else 0
                    push(
                        {
                            "status": "downloading",
                            "percent": round(done / total * 100, 1) if total else 0,
                            "speed": human_bytes(speed) + "/s",
                            "eta": f"{eta // 60:02d}:{eta % 60:02d}" if total else "",
                            "size": human_bytes(total) if total else "-",
                            "downloaded": human_bytes(done),
                        }
                    )

        if mp4_path.stat().st_size < 1024:
            raise RuntimeError("服务器返回的文件异常（可能被平台风控拦截）")

        if fmt == "native_mp3":
            ff = ffmpeg_path()
            if not ff:
                push({"status": "error", "msg": "未检测到 ffmpeg，无法转换 MP3"}, final=True)
                return
            push({"status": "processing", "percent": 100, "msg": "正在提取音频…"})
            mp3_path = mp4_path.with_suffix(".mp3")
            proc = subprocess.run(
                [ff, "-y", "-loglevel", "error", "-i", str(mp4_path), "-vn", "-b:a", "320k", str(mp3_path)],
                capture_output=True,
            )
            if proc.returncode != 0 or not mp3_path.exists():
                push({"status": "error", "msg": "MP3 转码失败，可改选「原画质」下载"}, final=True)
                return
            mp4_path.unlink(missing_ok=True)

        push({"status": "done", "percent": 100}, final=True)
    except _Cancelled:
        mp4_path.unlink(missing_ok=True)
        push({"status": "cancelled", "msg": "已取消"}, final=True)
    except Exception as e:  # noqa: BLE001
        mp4_path.unlink(missing_ok=True)
        push({"status": "error", "msg": f"下载失败：{e}"}, final=True)


FALLBACK_STEPS = (1080, 720, 480, 360)


def fallback_chain(fmt: str) -> list[str]:
    """清晰度降级链：当前档位不可用时（如需要会员权限）依次退到更低档"""
    m = re.search(r"height<=(\d+)", fmt)
    if m:
        h = int(m.group(1))
        if "vcodec!=none" in fmt:  # 无 ffmpeg 时的复合流写法
            return [
                f"best[vcodec!=none][acodec!=none][height<={x}]"
                for x in FALLBACK_STEPS
                if x < h
            ]
        return [f"bv*[height<={x}]+ba/b[height<={x}]" for x in FALLBACK_STEPS if x < h]
    if fmt in ("best", "bv*+ba/b", "bv*+ba", "b"):
        return [f"bv*[height<={x}]+ba/b[height<={x}]" for x in FALLBACK_STEPS]
    return []


def download_error(e: Exception, url: str, options: dict) -> str:
    """生成带站点专属建议的错误文案"""
    raw = str(e).replace("ERROR:", "").strip()
    msg = friendly_error(raw)
    if msg == raw:
        msg = raw[-220:]
    low = (url or "").lower()
    if "bilibili" in low and not (options or {}).get("cookies"):
        msg += (
            "\n\nB站下载建议：① 在「高级选项 → Cookie 文本」粘贴登录后导出的 Cookie（最有效）；"
            "② 或先试 360p/480p 低清晰度确认链路；③ 确认未在短时间内大量请求。"
        )
    return msg


@app.post("/api/download")
async def api_download(payload: dict):
    url = extract_url(payload.get("url") or "")
    if not url:
        raise HTTPException(400, "缺少链接")

    fmt = (payload.get("format") or "best").strip()
    options = payload.get("options") or {}
    if fmt in ("native", "native_mp3") and not get_native(url):
        raise HTTPException(400, "解析结果已过期，请重新点击「解析」后再下载")

    task_id = uuid.uuid4().hex[:12]

    TASKS[task_id] = {
        "queue": queue.Queue(),
        "cancel": False,
        "finished": False,
        "url": url,
        "created": time.time(),
        "state": {"status": "starting", "percent": 0, "msg": "任务已创建，等待下载…"},
    }

    target = run_direct if fmt in ("native", "native_mp3") else run_download
    threading.Thread(target=target, args=(task_id, url, fmt, options), daemon=True).start()

    return {"task_id": task_id}


# ----------------------------------------------------------------------------
# 文件管理
# ----------------------------------------------------------------------------
MEDIA_EXT = {
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4a", ".mp3",
    ".aac", ".opus", ".ogg", ".flac", ".wav", ".srt", ".vtt", ".ass", ".jpg", ".webp", ".png",
}


@app.get("/api/files")
async def list_files():
    items = []
    for tid in sorted(DOWNLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not tid.is_dir() or tid.name.startswith("."):
            continue
        for f in tid.iterdir():
            if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in MEDIA_EXT:
                continue
            st = f.stat()
            items.append(
                {
                    "task_id": tid.name,
                    "name": f.name,
                    "size": st.st_size,
                    "size_h": human_bytes(st.st_size),
                    "mtime": int(st.st_mtime),
                    "kind": "audio" if f.suffix.lower() in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"} else ("sub" if f.suffix.lower() in {".srt", ".vtt", ".ass"} else "video"),
                }
            )
    return items


@app.get("/api/files/{task_id}/{name}")
async def get_file(task_id: str, name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = DOWNLOAD_DIR / task_id / name
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=p.name)


@app.delete("/api/files/{task_id}/{name}")
async def delete_file(task_id: str, name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = DOWNLOAD_DIR / task_id / name
    if p.is_file():
        p.unlink()
    return {"ok": True}


# ----------------------------------------------------------------------------
# 静态页面
# ----------------------------------------------------------------------------
class NoCacheStaticFiles(StaticFiles):
    """禁用缓存：否则改了页面后用户刷新仍看到旧版，会以为修改没生效"""

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        try:
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        except Exception:  # noqa: BLE001
            pass
        return resp


app.mount("/", NoCacheStaticFiles(directory=str(ASSET_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()

    if FROZEN or os.name == "nt":
        print("╭──────────────────────────────────────╮")
        print("│   VideoGrab · 本地视频下载器          │")
        print("╰──────────────────────────────────────╯")
        ensure_ffmpeg()
        import webbrowser
        import threading as _th

        _th.Timer(1.8, lambda: webbrowser.open(f"http://localhost:{a.port}")).start()
        print(f"  服务运行中：http://localhost:{a.port}")
        print(f"  下载目录：{DOWNLOAD_DIR}")
        print("  关闭本窗口即停止服务。")

    uvicorn.run(app, host=a.host, port=a.port, log_level="info")

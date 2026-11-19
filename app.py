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
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
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
#   打包 exe -> exe 旁边；脚本运行 -> 脚本旁边（绿色便携，不往用户目录写东西）
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
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def looks_like_url(text: str) -> bool:
    text = text.strip()
    return bool(URL_RE.fullmatch(text) or URL_RE.match(text))


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
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "请提供视频链接")
    if not looks_like_url(url):
        raise HTTPException(400, "链接格式不正确")

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
    cookiefile = None
    if cookies:
        cookiefile = DOWNLOAD_DIR / f".cookies_{uuid.uuid4().hex}.txt"
        cookiefile.write_text(cookies, encoding="utf-8")
        opts["cookiefile"] = str(cookiefile)

    def extract(o):
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(executor, extract, opts)
        if info is None and opts.get("ignoreerrors"):
            # 播放列表容错模式会静默失败，改用严格模式再跑一次，把真实原因抛出来
            info = await loop.run_in_executor(executor, extract, {**opts, "ignoreerrors": False})
    except yt_dlp.utils.DownloadError as e:
        raw = str(e).replace("ERROR:", "").strip()
        human = friendly_error(raw)
        raise HTTPException(
            400,
            human
            if human != raw
            else f"{raw[-200:]}\n（常见原因：网络不通、链接失效、需登录后查看或被平台风控拦截）",
        )
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

    return {"playlist": False, **format_info(info, has_ffmpeg)}


def friendly_error(msg: str) -> str:
    """把 yt-dlp 的原始报错翻译成人话"""
    m = msg.lower()
    # 注意：状态码必须排在语义匹配之前，且关键词要足够精确
    # （曾踩坑：用 "age" 匹配会命中 webpage/message 里的字母，把 412 误报成年龄限制）
    table = [
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

    cookiefile = None
    if options.get("cookies"):
        cookiefile = out_dir / f".cookies_{task_id}.txt"
        cookiefile.write_text(options["cookies"], encoding="utf-8")
        opts["cookiefile"] = str(cookiefile)

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
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少链接")

    fmt = (payload.get("format") or "best").strip()
    task_id = uuid.uuid4().hex[:12]

    TASKS[task_id] = {
        "queue": queue.Queue(),
        "cancel": False,
        "finished": False,
        "url": url,
        "created": time.time(),
        "state": {"status": "starting", "percent": 0, "msg": "任务已创建，等待下载…"},
    }

    threading.Thread(
        target=run_download,
        args=(task_id, url, fmt, payload.get("options") or {}),
        daemon=True,
    ).start()

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

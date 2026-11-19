#!/usr/bin/env bash
# VideoGrab 启动脚本 (macOS / Linux)
# 用法： bash start.sh [端口]

set -e
PORT="${1:-8080}"
cd "$(cd "$(dirname "$0")" && pwd)"

echo "╭──────────────────────────────────────╮"
echo "│   VideoGrab · 本地视频下载器          │"
echo "╰──────────────────────────────────────╯"
echo

# ── 1. 找 Python ────────────────────────────────────────────
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    V=$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")
    MAJ=${V%%.*}; MIN=${V##*.}
    if [ "$MAJ" -ge 3 ] && [ "$MIN" -ge 9 ]; then PY="$c"; break; fi
  fi
done
[ -z "$PY" ] && { echo "✗ 未找到 Python 3.9+"; echo "  macOS:   brew install python"; echo "  Ubuntu:  sudo apt install python3"; exit 1; }
echo "▸ Python: $($PY --version 2>&1)"

# ── 2. 建独立环境（不污染系统 Python）────────────────────────
PIPFLAGS=""
if [ ! -d ".venv" ]; then
  echo "▸ 创建独立虚拟环境…"
  if ! $PY -m venv .venv 2>/dev/null; then
    echo "  ⚠ venv 不可用（Ubuntu 请执行：sudo apt install python3-venv），改用系统环境"
  fi
fi
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
  PY=python
else
  $PY -c "import sys,os; sys.exit(0 if os.access(os.path.dirname(sys.executable)+'/EXTERNALLY-MANAGED', os.R_OK) else 1)" \
    && PIPFLAGS="--break-system-packages"
fi

# ── 3. 依赖 ────────────────────────────────────────────────
echo "▸ 检查依赖…"
if ! $PY -c "import yt_dlp, fastapi, uvicorn" 2>/dev/null; then
  echo "  安装 yt-dlp / fastapi / uvicorn（首次约需 1 分钟）…"
  PIPARGS="install -U yt-dlp fastapi uvicorn"
  if ! $PY -m pip $PIPARGS $PIPFLAGS 2>/dev/null; then
    echo "  默认源失败，改用清华镜像重试…"
    $PY -m pip $PIPARGS $PIPFLAGS -i https://pypi.tuna.tsinghua.edu.cn/simple
  fi
  echo "  ✓ 依赖安装完成"
else
  echo "  ✓ 依赖已就绪"
fi

# ── 4. ffmpeg ──────────────────────────────────────────────
if command -v ffmpeg >/dev/null 2>&1; then
  echo "  ✓ ffmpeg 已就绪"
else
  echo "  ⚠ 未检测到 ffmpeg（不影响基础下载，但合并高清流 / 转 MP3 需要它）"
  echo "    macOS:   brew install ffmpeg"
  echo "    Ubuntu:  sudo apt install ffmpeg"
fi

# ── 5. 启动 ────────────────────────────────────────────────
mkdir -p downloads
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "─────────────────────────────────────────"
echo "  本机访问：  http://localhost:${PORT}"
[ -n "$IP" ] && echo "  局域网访问：http://${IP}:${PORT}"
echo "  停止服务：  Ctrl + C"
echo "─────────────────────────────────────────"
echo

[ "$PORT" = "8080" ] && exec $PY app.py || exec $PY app.py --port "$PORT"

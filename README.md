# VideoGrab · 网页版视频下载器

粘贴链接即可下载。**不注册、不登录、不收费**，完全跑在你自己的机器上，数据不出本地。

## 快速开始

```bash
cd video-downloader
bash start.sh            # 默认 8080 端口
bash start.sh 3000       # 指定端口
```

然后浏览器打开 http://localhost:8080

### 手动启动

```bash
pip3 install -U yt-dlp fastapi uvicorn
python3 app.py --port 8080
```

> MP3 转换和「高清视频 + 音轨」合并依赖 ffmpeg。缺了它也能下（会退化为直接合并或单流），但建议装上。
> Debian/Ubuntu：`sudo apt install ffmpeg`；macOS：`brew install ffmpeg`。

## 功能

| 功能 | 说明 |
|---|---|
| 站点覆盖 | 依托 yt-dlp，支持 YouTube、B站、抖音、微博、X、Instagram、Vimeo 等 1000+ 站点 |
| 清晰度选择 | 解析后列出 2160p / 1440p / 1080p / 720p / 480p / 360p 可用档位 |
| 仅音频 | 保留原音轨，或转码为 MP3 320k |
| 字幕 | 下载指定语言字幕并烧录进视频 |
| 封面嵌入 | 自动把封面写入文件元数据 |
| 播放列表 | 勾选后可整站批量下载 |
| 实时进度 | WebSocket 推送百分比、速度、剩余时间、已下载体积 |
| 文件管理 | 页面内直接查看 / 下载到本地 / 删除 |
| 取消任务 | 下载过程中随时中断 |

## 高级选项

界面上点「高级选项」展开：

- **Cookie 文本** — 部分平台对未登录请求有风控（如 B站会返回 HTTP 412），粘贴 Netscape 格式 cookies.txt 内容即可。用浏览器扩展「Get cookies.txt LOCALLY」导出。
- **代理地址** — 例如 `http://127.0.0.1:7890`，用于跨国站点或内网穿透。
- **视频密码** — 针对设置了访问密码的视频。
- **限速** — 例 `2M`、`500K`，避免占满带宽。

## 文件结构

```
video-downloader/
├── app.py              # FastAPI 后端：解析 / 下载 / 进度推送 / 文件管理
├── static/index.html   # 前端页面（内嵌 CSS/JS，零构建）
├── downloads/          # 下载产物，按任务 ID 分目录
└── start.sh            # 依赖自检 + 启动
```

## 关于「不需要注册不需要会员」

工具本身不做任何账号体系，也不需要你登录任何东西 —— 这是它和市面常见「在线下载网站」最大的区别。

但要说明白一点：**工具跳过的是「下载工具」本身的注册门槛，不是视频平台的付费/会员限制。**

- 公开可见的视频：直接粘贴链接就能下，这部分完全免费。
- 会员专属 / 付费购买 / DRM 加密的内容：本工具**不会**也**不能被用于**绕过这些限制，这不是技术能力问题，是法律和道德边界。
- 需要登录才能看的自己的内容（如自己的网盘、会员内的自己上传的视频）：可通过 Cookie 选项处理，但请确认你有该内容的访问权。

## API

纯 HTTP 接口，方便脚本调用：

```bash
# 解析
curl -X POST http://localhost:8080/api/info \
  -H "Content-Type: application/json" -d '{"url":"https://..."}'

# 下载（返回 task_id）
curl -X POST http://localhost:8080/api/download \
  -H "Content-Type: application/json" \
  -d '{"url":"https://...","format":"bv*[height<=1080]+ba/b"}'

# 实时进度（WebSocket）
ws://localhost:8080/ws/<task_id>

# 文件列表 / 取文件 / 删除
GET    /api/files
GET    /api/files/<task_id>/<filename>
DELETE /api/files/<task_id>/<filename>
```

## 声明

本工具基于开源项目 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 构建，仅供个人保存**自己拥有版权或已获授权**的内容，以及公开演讲、教程、CC 授权作品。下载受版权保护的内容可能违反平台条款与相关法律，请自行确认你对目标内容拥有相应权利。

构建者不对使用者的具体下载行为负责。

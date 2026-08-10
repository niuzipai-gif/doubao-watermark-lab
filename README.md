# 豆包水印清理器 V3

独立于 `omni/gemini` 的本地图片/视频水印清理工具。

## 当前处理策略

- 图片：识别右下区域的浅色豆包字样，使用小范围 Telea 修复。
- 视频：逐帧重新检测水印框；检测不到时使用上一帧模板跟踪，保持 mask 随水印移动；每帧独立修复，避免首帧固定 mask 失效。
- 视频功能标记为 BETA：复杂运动背景可能需要更强的时空视频修复模型。
- 链接解析：独立解析豆包公开分享链接，直接下载豆包返回的原始图片/视频；不会自动进入本地去水印流程。
- 所有文件在本地处理，不上传原素材。

## 本地运行

```powershell
python server.py
```

Windows 下直接双击 `start-doubao-lab.bat`。它会在后台启动本地后端，并自动打开 GitHub Pages；如果后端已经启动，不会重复启动。

本地后端地址是 `http://127.0.0.1:4173/`。它启用 OpenCV 的图片和视频处理；公开 Pages 会自动尝试连接这个本地地址。

链接解析需要本地后端在线。支持豆包公开的 `/thread/` 对话分享链接和带 `video_id` 的 `/video-sharing` 视频分享链接；登录态、失效链接以及豆包页面结构变化会直接返回失败原因，不会伪造下载结果。

解析接口：`POST /api/resolve-link`，JSON 请求体为 `{ "url": "https://www.doubao.com/..." }`。

## 公网部署

GitHub Pages 只负责静态前端，公网处理需要单独部署 Flask API。仓库已经包含 `Dockerfile`、`render.yaml` 和生产启动命令，推荐使用 Render Web Service：

1. 登录 Render，选择 `New -> Web Service -> Public Git Repository`，填入 `https://github.com/niuzipai-gif/doubao-watermark-lab`。
2. 选择 Docker 部署；仓库中的 `render.yaml` 默认使用 Free 规格，首次请求可能需要等待冷启动。
3. 部署完成后复制 Render 的 `https://...onrender.com` 地址，写入 `api-config.js` 的 `window.DOUBAO_API_BASE`，再提交并推送一次。
4. 打开 GitHub Pages，图片、BETA 视频和链接解析都会直接请求公网 API；朋友不需要安装 Python 或启动本地后端。

Render Free Web Service 会在 15 分钟无请求后休眠，下一次请求需要冷启动；朋友频繁使用或视频任务较多时，应升级实例规格。处理文件只写入临时目录，不保存用户素材。

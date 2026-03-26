# QNAIGC Video Test（内部用）

这是一个最小化测试页面：输入“参考视频 URL + 文本 prompt”，调用 `https://api.qnaigc.com/v1` 的视频任务接口创建任务并轮询，直到拿到 `task_result.videos[].url` 后在页面播放。

文档参考：
- [七牛云 Kling 视频生成 API 文档（qnaigc）](https://developer.qiniu.com/aitokenapi/13388/new-video-generate-kling-api)

## 1) 准备环境变量

不要把 API Key 写进前端页面，后端只从环境变量读取：

```bash
export QNAIGC_API_KEY="你的 sk-xxx"
```

也可以在启动前把环境变量放进你的 shell。

## 2) 安装依赖并启动

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 3) 打开测试页

浏览器打开：

`http://localhost:8000/`

在页面里填：
- `参考视频 URL`：必须是公网可访问的 mp4/mov 链接
- `prompt`：你希望模型生成的新视频内容描述

提交后会自动轮询任务状态，并在 `completed` 后播放结果视频。

## 4) 部署（给同事随时打开就能用）

本项目支持一键部署到 Render（推荐，最省心，给你一个长期 HTTPS 地址）。

### 部署前注意

- 不要把 `backend/.env` 上传到公网仓库；线上请用平台的环境变量配置密钥。
- 线上部署后，你会得到一个类似 `https://xxx.onrender.com/` 的地址，同事直接打开即可使用。

### Render 一键部署步骤

1. 把整个 `qnaigc-video-test` 文件夹放到一个 Git 仓库（GitHub/GitLab 都可以）。
2. 在 Render 新建服务，选择 “Blueprint”，指向该仓库（项目根目录里已有 `render.yaml`）。
3. 在 Render 的环境变量里填写：
   - `QNAIGC_API_KEY`
   - `TOS_ACCESS_KEY`
   - `TOS_SECRET_KEY`
   - `TOS_REGION`
   - `TOS_ENDPOINT`
   - `TOS_BUCKET`
4. 部署完成后，打开 Render 提供的 URL（根路径 `/` 就是测试页）。


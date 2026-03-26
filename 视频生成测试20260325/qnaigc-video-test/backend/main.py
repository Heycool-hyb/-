import logging
import os
import time
from base64 import b64decode
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


QNAIGC_BASE_URL = "https://api.qnaigc.com/v1"
KLING_MODEL_ID = "kling-v3-omni"


class VideoJobCreateRequest(BaseModel):
    # 目标：用“参考视频 + 文本 prompt”生成新视频
    # 前端与后端都会固定使用 kling-v3-omni；这里保持为 str，避免因模型不同导致请求校验失败
    model: str = KLING_MODEL_ID
    prompt: str = Field(min_length=1, max_length=2500)

    # 参考视频（必须是公网可访问的 URL）
    video_url: str = Field(min_length=1)
    refer_type: Literal["feature", "base"] = "base"
    keep_original_sound: Literal["yes", "no"] = "yes"

    # 可选：时长/分辨率/模式
    seconds: Literal[
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15",
    ] = "5"
    size: Literal[
        "1920x1080",
        "1080x1920",
        "1280x720",
        "720x1280",
        "1080x1080",
        "720x720",
    ] = "1920x1080"
    mode: Literal["std", "pro"] = "pro"


class VideoJobCreateResponse(BaseModel):
    id: str


class VideoJobStatusResponse(BaseModel):
    id: str
    status: str
    model: Optional[str] = None
    task_result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class ReferenceVideoUploadJsonRequest(BaseModel):
    # 使用 base64 + JSON 接收，避免 FastAPI 依赖 python-multipart（便于本地测试）
    file_name: str = Field(min_length=1)
    content_base64: str = Field(min_length=1)


class SaveGeneratedVideoRequest(BaseModel):
    video_url: str = Field(min_length=1, description="QNAIGC 返回的可下载视频 URL")


class TosPresignRequest(BaseModel):
    filename: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    size: int = Field(ge=1, description="文件大小（字节）")
    bucket: str = Field(min_length=1, description="桶名")
    endpoint: str = Field(min_length=1, description="TOS Endpoint，如 https://tos-xxx.volcengineapi.com")
    object_key: str = Field(min_length=1, description="对象键（路径），例如 generated/xxx.mp4")
    domain: Optional[str] = Field(default=None, description="可访问域名（可选，用于拼接最终 URL）")


app = FastAPI(title="QNAIGC Video Test")
logger = logging.getLogger("uvicorn.error")

APP_BUILD_VERSION = "error-handling-v2"

# 仅用于前端调试页面；内部环境下也可以收紧 allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

GENERATED_DIR = os.path.join(STATIC_DIR, "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = (endpoint or "").strip()
    endpoint = endpoint.removeprefix("https://").removeprefix("http://")
    endpoint = endpoint.strip("/")
    return endpoint


def _require_env(name: str) -> str:
    _load_local_env_file()
    v = os.getenv(name, "").strip()
    if not v:
        raise HTTPException(status_code=500, detail={"message": f"Missing env var: {name}"})
    return v


def _safe_object_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail={"message": "object_key is empty"})
    if key.startswith("/"):
        raise HTTPException(status_code=400, detail={"message": "object_key should not start with '/'"})
    if "\x00" in key:
        raise HTTPException(status_code=400, detail={"message": "object_key contains null byte"})
    # 简单防御：禁止明显的目录穿越片段
    parts = key.split("/")
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail={"message": "object_key contains '..' segment"})
    if len(key) > 1024:
        raise HTTPException(status_code=400, detail={"message": "object_key too long"})
    return key


def _encode_object_key_for_url(key: str) -> str:
    # 仅用于拼接 object_url，不参与签名；签名仍以原始 key 为准。
    return "/".join(quote(p, safe="") for p in (key or "").split("/"))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/api/build")
def api_build() -> Dict[str, str]:
    """
    用于快速确认线上服务是否部署了最新代码。
    返回固定版本号，便于排查“改了但线上没更新”的情况。
    """
    return {"version": APP_BUILD_VERSION}

@app.get("/api/debug/config")
def debug_config() -> Dict[str, Any]:
    # 仅返回“是否配置了”，不返回任何密钥内容。
    _load_local_env_file()

    def has(name: str) -> bool:
        return bool(os.getenv(name, "").strip())

    return {
        "qnaigc": {
            "QNAIGC_BASE_URL": QNAIGC_BASE_URL,
            "has_QNAIGC_API_KEY": has("QNAIGC_API_KEY"),
            "KLING_MODEL_ID": KLING_MODEL_ID,
        },
        "tos": {
            "has_TOS_ACCESS_KEY": has("TOS_ACCESS_KEY"),
            "has_TOS_SECRET_KEY": has("TOS_SECRET_KEY"),
            "has_TOS_REGION": has("TOS_REGION"),
            "has_TOS_ENDPOINT": has("TOS_ENDPOINT"),
            "has_TOS_BUCKET": has("TOS_BUCKET"),
        },
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 把未捕获异常统一转成 JSON，避免默认 text/plain 错误页。
    logger.exception("Unhandled server error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "message": "Internal server error",
                "path": request.url.path,
            }
        },
    )


def _get_api_key() -> str:
    # 优先读取环境变量；如果未设置，则尝试读取后端目录下的 `./.env` 文件
    # （方便不懂环境变量的人进行本地配置；例如创建一个 backend/.env）
    key = os.getenv("QNAIGC_API_KEY", "").strip()
    if not key:
        _load_local_env_file()
        key = os.getenv("QNAIGC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing QNAIGC_API_KEY env var. Please set it before starting the server."
        )
    return key


_LOCAL_ENV_LOADED = False


def _load_local_env_file() -> None:
    """
    读取 backend/.env 并写入 os.environ（仅在当前进程生效）。
    目的：本地运行时避免手动 export 一堆变量。
    规则：只在系统环境变量缺失时写入（不覆盖已有非空值）。
    """
    global _LOCAL_ENV_LOADED
    if _LOCAL_ENV_LOADED:
        return
    _LOCAL_ENV_LOADED = True

    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if not k:
                continue
            # 不覆盖已有非空值
            if os.getenv(k, "").strip():
                continue
            os.environ[k] = v
    except Exception:
        # 读取失败就忽略，后续由 _require_env/_get_api_key 抛出缺失提示
        return


async def _create_video_job(req: VideoJobCreateRequest) -> Dict[str, Any]:
    api_key = _get_api_key()
    url = f"{QNAIGC_BASE_URL}/videos"
    video_url = (req.video_url or "").strip()
    parsed = urlparse(video_url)
    # QNAIGC 需要“云端可访问”的视频地址；如果给的是 localhost/本机地址或相对路径，云端无法读取
    if (
        not video_url
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or video_url.startswith("/")
        or "localhost" in video_url
        or "127.0.0.1" in video_url
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "QNAIGC 需要公网可访问的 video_url。请提供以 http:// 或 https:// 开头、云端能直接访问到的 MP4/MOV 链接，不要使用 localhost/127.0.0.1 或相对路径。",
            },
        )

    payload: Dict[str, Any] = {
        # 固定使用指定模型，确保每次请求一致
        "model": KLING_MODEL_ID,
        "prompt": req.prompt,
        "video_list": [
            {
                "video_url": req.video_url,
                "refer_type": req.refer_type,
                "keep_original_sound": req.keep_original_sound,
            }
        ],
        "seconds": req.seconds,
        "size": req.size,
        "mode": req.mode,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise HTTPException(
                status_code=504,
                detail={"message": "QNAIGC request timeout", "error": str(e)},
            ) from e
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=502,
                detail={"message": "QNAIGC request failed", "error": str(e)},
            ) from e

        # 先读取一次 body，避免 r.json() 失败后无法取到原始内容
        resp_text = (r.text or "").strip()

        def _parse_maybe_json() -> Any:
            # 有些服务会在 JSON 失败时返回文本；尽量把信息结构化
            try:
                ct = (r.headers.get("content-type") or "").lower()
                if "application/json" in ct or ct.endswith("+json"):
                    return r.json()
            except Exception:
                pass
            try:
                return r.json()
            except Exception:
                return resp_text

        if r.status_code >= 400:
            raise HTTPException(
                status_code=r.status_code,
                detail={
                    "message": "Failed to create video job",
                    "response": _parse_maybe_json(),
                },
            )

        try:
            return r.json()
        except Exception:
            # 2xx 但返回非 JSON：把原始 body 返回，避免前端只能看到 500
            raise HTTPException(
                status_code=502,
                detail={"message": "QNAIGC returned non-JSON response", "response": resp_text},
            )


async def _get_video_job_status(job_id: str) -> Dict[str, Any]:
    api_key = _get_api_key()
    url = f"{QNAIGC_BASE_URL}/videos/{job_id}"

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            raise HTTPException(
                status_code=504,
                detail={"message": "QNAIGC request timeout", "error": str(e)},
            ) from e
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=502,
                detail={"message": "QNAIGC request failed", "error": str(e)},
            ) from e

        resp_text = (r.text or "").strip()

        def _parse_maybe_json() -> Any:
            try:
                ct = (r.headers.get("content-type") or "").lower()
                if "application/json" in ct or ct.endswith("+json"):
                    return r.json()
            except Exception:
                pass
            try:
                return r.json()
            except Exception:
                return resp_text

        if r.status_code >= 400:
            raise HTTPException(
                status_code=r.status_code,
                detail={
                    "message": "Failed to get video job status",
                    "response": _parse_maybe_json(),
                },
            )

        try:
            return r.json()
        except Exception:
            raise HTTPException(
                status_code=502,
                detail={"message": "QNAIGC returned non-JSON response", "response": resp_text},
            )


@app.post("/api/video-jobs", response_model=VideoJobCreateResponse)
async def create_video_job(req: VideoJobCreateRequest = Body(...)) -> VideoJobCreateResponse:
    try:
        data = await _create_video_job(req)
    except RuntimeError as e:
        # 例如：缺少 QNAIGC_API_KEY 时，返回可读的 JSON 错误
        raise HTTPException(status_code=500, detail={"message": str(e)})
    except HTTPException:
        # 已经是可读的 JSON 错误，直接透传给 FastAPI
        raise
    except Exception as e:
        # 避免非预期异常导致纯文本 500
        raise HTTPException(status_code=500, detail={"message": "Internal server error", "error": str(e)})
    job_id = data.get("id")
    if not job_id:
        raise HTTPException(status_code=500, detail={"message": "Missing 'id' in response", "data": data})
    return VideoJobCreateResponse(id=job_id)


@app.get("/api/video-jobs/{job_id}", response_model=VideoJobStatusResponse)
async def get_video_job(job_id: str) -> VideoJobStatusResponse:
    try:
        data = await _get_video_job_status(job_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail={"message": str(e)})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"message": "Internal server error", "error": str(e)})
    return VideoJobStatusResponse(
        id=data.get("id", job_id),
        status=data.get("status", "unknown"),
        model=data.get("model"),
        task_result=data.get("task_result"),
        error=data.get("error"),
    )


@app.post("/api/upload/reference-video")
async def upload_reference_video(req: ReferenceVideoUploadJsonRequest = Body(...)) -> Dict[str, str]:
    """
    把“参考视频文件”先上传到本地目录（用于本地测试）。
    返回一个本地可访问 URL：/static/uploads/<filename>

    注意：这里使用 JSON(base64) 上传，避免依赖 python-multipart。
    """
    original_name = os.path.basename(req.file_name)
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    allowed_exts = {"mp4", "mov", "m4v", "webm", "mkv"}
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unsupported file type: .{ext}. Allowed: {sorted(allowed_exts)}"},
        )

    dest_name = f"{int(time.time() * 1000)}_{uuid4().hex}.{ext}"
    dest_path = os.path.join(UPLOAD_DIR, dest_name)

    try:
        raw = b64decode(req.content_base64, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"message": f"Invalid base64: {str(e)}"})

    with open(dest_path, "wb") as f:
        f.write(raw)

    return {"video_url": f"/static/uploads/{dest_name}"}


def _guess_video_ext(content_type: str, video_url: str) -> str:
    ct = (content_type or "").lower()
    if "quicktime" in ct or "mov" in ct:
        return "mov"
    if "mp4" in ct:
        return "mp4"
    if "webm" in ct:
        return "webm"

    parsed = urlparse(video_url)
    path = parsed.path or ""
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in {"mp4", "mov", "webm", "mkv", "m4v"}:
        return "mov" if ext == "m4v" else ext
    return "mp4"


@app.post("/api/save/generated-video")
async def save_generated_video(req: SaveGeneratedVideoRequest = Body(...)) -> Dict[str, str]:
    """
    把 QNAIGC 返回的 generated 视频下载到本地：
    - 保存目录：backend/static/generated/
    - 返回：本地可访问 URL：/static/generated/<filename>
    """
    video_url = req.video_url.strip()
    if not video_url:
        raise HTTPException(status_code=400, detail={"message": "Missing video_url"})

    # 禁止抓取本地地址，避免 127.0.0.1/localhost 这类地址云端不可访问
    if any(x in video_url for x in ("localhost", "127.0.0.1")):
        raise HTTPException(status_code=400, detail={"message": "video_url must be publicly accessible"})

    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream("GET", video_url) as resp:
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=400,
                    detail={"message": f"Failed to download video. status={resp.status_code}"},
                )

            content_type = resp.headers.get("content-type", "")
            ext = _guess_video_ext(content_type, video_url)
            dest_name = f"{int(time.time() * 1000)}_{uuid4().hex}.{ext}"
            dest_path = os.path.join(GENERATED_DIR, dest_name)

            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        f.write(chunk)

    return {"video_url": f"/static/generated/{dest_name}"}


@app.post("/api/tos/presign")
async def tos_presign(req: TosPresignRequest = Body(...)) -> Dict[str, Any]:
    """
    返回火山云 TOS 预签名直传信息：
    - upload_url: 预签名 PUT URL
    - headers: （可选）直传时需要附带的 headers
    - object_url: （可选）上传完成后可访问的 URL
    """
    # 服务器端必须配置 TOS 账号信息（secret 仅在服务端出现）
    tos_ak = _require_env("TOS_ACCESS_KEY")
    tos_sk = _require_env("TOS_SECRET_KEY")
    tos_region = _require_env("TOS_REGION")
    tos_endpoint_env = _normalize_endpoint(_require_env("TOS_ENDPOINT"))
    tos_bucket_env = _require_env("TOS_BUCKET")

    # 请求里也传了 bucket/endpoint：做一致性校验（避免有人用你的 server 去签不同桶）
    req_bucket = (req.bucket or "").strip()
    req_endpoint = _normalize_endpoint(req.endpoint or "")
    if not req_bucket or not req_endpoint:
        raise HTTPException(status_code=400, detail={"message": "bucket/endpoint is required"})
    if req_bucket != tos_bucket_env:
        raise HTTPException(status_code=403, detail={"message": "bucket not allowed"})
    if req_endpoint != tos_endpoint_env:
        raise HTTPException(status_code=403, detail={"message": "endpoint not allowed"})

    object_key = _safe_object_key(req.object_key)
    content_type = (req.content_type or "").strip()

    try:
        from tos import TosClientV2  # type: ignore
        from tos.enum import HttpMethodType  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail={"message": f"tos SDK import failed: {e}"}) from e

    client = TosClientV2(
        ak=tos_ak,
        sk=tos_sk,
        endpoint=tos_endpoint_env,
        region=tos_region,
    )

    # 预签名 PUT：把 Content-Type 纳入签名要求前端复用。
    pre_signed = client.pre_signed_url(
        http_method=HttpMethodType.Http_Method_Put,
        bucket=req_bucket,
        key=object_key,
        expires=300,
        header={"content-type": content_type},
    )

    upload_url = getattr(pre_signed, "signed_url", None) or getattr(pre_signed, "signedUrl", None)
    if not upload_url:
        raise HTTPException(status_code=500, detail={"message": "Failed to generate upload_url"})

    signed_header = getattr(pre_signed, "signed_header", None) or getattr(pre_signed, "signedHeader", None) or {}
    # 浏览器禁止设置 `host`/`content-length` 等敏感头，只下发我们签名用到的 content-type
    extra_headers: Dict[str, str] = {}
    for k, v in (signed_header or {}).items():
        if str(k).lower() == "content-type":
            extra_headers[str(k)] = str(v)

    object_url = None
    if req.domain and req.domain.strip():
        # domain 允许：
        # - 传 CDN 域名（如 https://cdn.example.com）
        # - 也有人只传 endpoint（如 tos-cn-shanghai.volces.com），这种情况下需要补 bucket，
        #   否则会变成 path-style：https://tos-xxx/{bucket}/{key}，浏览器打开会把第一个 path 段当桶名。
        from urllib.parse import urlparse, urlunparse

        raw = req.domain.strip().rstrip("/")
        if not (raw.startswith("http://") or raw.startswith("https://")):
            raw = f"https://{raw}"

        u = urlparse(raw)
        scheme = u.scheme or "https"
        host = (u.netloc or "").strip()
        base_path = (u.path or "").rstrip("/")

        # 如果 host 看起来就是 endpoint（不含 bucket 前缀），则自动补 bucket 前缀
        # 例：tos-cn-shanghai.volces.com -> {bucket}.tos-cn-shanghai.volces.com
        if host and host == tos_endpoint_env:
            host = f"{req_bucket}.{host}"
        elif host and host.endswith(f".{tos_endpoint_env}") and not host.startswith(f"{req_bucket}."):
            # 允许用户传类似 xxx.tos-cn-...；只在缺 bucket 前缀时补
            host = f"{req_bucket}.{host}"

        base = urlunparse((scheme, host, base_path, "", "", "")).rstrip("/")
        object_url = f"{base}/{_encode_object_key_for_url(object_key)}"
    else:
        # 兜底：如果未提供自定义域名，则按常见的虚拟主机风格拼接一个可访问 URL：
        #   https://{bucket}.{endpoint}/{object_key}
        # 注意：不同 TOS/CDN 可能有 path-style 访问方式；这里仅提供默认可用的候选 URL。
        object_url = f"https://{req_bucket}.{tos_endpoint_env}/{_encode_object_key_for_url(object_key)}"

    return {"upload_url": upload_url, "headers": extra_headers, "object_url": object_url}


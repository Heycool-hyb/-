#!/usr/bin/env python3
"""
TOS 私有对象测试脚本（签名 / 上传 / 查看 / 下载 / 列表）

使用方式（示例）：
  在运行环境设置：
  - TOS_ACCESS_KEY / TOS_SECRET_KEY
  - TOS_REGION / TOS_ENDPOINT / TOS_BUCKET
  或使用命令行参数：--ak/--sk/--region/--endpoint/--bucket

  重要：不要把 TOS_SECRET_KEY 写死到代码或提交到仓库。

  # 1) 上传（默认禁止覆盖）
  python3 tos_smoke_test.py upload --file demo.jpg --key debug/demo.jpg --inline

  # 2) 查看对象元信息（重点看 content_disposition / content_type）
  python3 tos_smoke_test.py head --key debug/demo.jpg

  # 3) 生成 signed GET（用于浏览器预览或 curl）
  python3 tos_smoke_test.py sign --key debug/demo.jpg --purpose thumbnail --expires 300

  # 4) 下载验证
  python3 tos_smoke_test.py download --key debug/demo.jpg --out /tmp/demo.jpg

  # 5) 按前缀列举
  python3 tos_smoke_test.py list --prefix debug/ --max-keys 20
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


def _mask(value: str, left: int = 4, right: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= left + right:
        return "*" * len(value)
    return f"{value[:left]}{'*' * (len(value) - left - right)}{value[-right:]}"


@dataclass(frozen=True)
class TosEnv:
    ak: str
    sk: str
    region: str
    endpoint: str
    bucket: str


def _load_env(args: argparse.Namespace) -> TosEnv:
    ak = args.ak or os.getenv("TOS_ACCESS_KEY", "")
    sk = args.sk or os.getenv("TOS_SECRET_KEY", "")
    region = args.region or os.getenv("TOS_REGION", "")
    endpoint = args.endpoint or os.getenv("TOS_ENDPOINT", "")
    bucket = args.bucket or os.getenv("TOS_BUCKET", "") or os.getenv("TOS_BUCKET_NAME", "")

    missing = [name for name, value in {
        "TOS_ACCESS_KEY": ak,
        "TOS_SECRET_KEY": sk,
        "TOS_REGION": region,
        "TOS_ENDPOINT": endpoint,
        "TOS_BUCKET": bucket,
    }.items() if not value]

    if missing:
        raise SystemExit(
            "缺少环境变量/参数："
            + ", ".join(missing)
            + "\n请在运行环境设置：TOS_ACCESS_KEY / TOS_SECRET_KEY / TOS_REGION / TOS_ENDPOINT / TOS_BUCKET"
        )

    return TosEnv(ak=ak, sk=sk, region=region, endpoint=endpoint, bucket=bucket)


def _make_client(env: TosEnv):
    try:
        from tos import TosClientV2  # type: ignore
    except Exception as e:
        raise SystemExit(f"未安装 tos SDK：{e}\n请在运行环境安装：pip install tos") from e

    return TosClientV2(
        ak=env.ak,
        sk=env.sk,
        endpoint=env.endpoint,
        region=env.region,
    )


def _print_env(env: TosEnv) -> None:
    print("TOS config:")
    print(f"- region   : {env.region}")
    print(f"- endpoint : {env.endpoint}")
    print(f"- bucket   : {env.bucket}")
    print(f"- ak       : {_mask(env.ak)}")
    print(f"- sk       : {_mask(env.sk)}")


def _signed_query_for_purpose(key: str, purpose: str) -> Dict[str, str]:
    query: Dict[str, str] = {}
    if purpose == "download":
        filename = os.path.basename(key) or "download"
        query["response-content-disposition"] = f'attachment; filename="{filename}"'
    elif purpose in {"preview", "thumbnail"}:
        query["response-content-disposition"] = "inline"
        ext = os.path.splitext(key)[1].lower()
        if ext in {".jpg", ".jpeg"}:
            query["response-content-type"] = "image/jpeg"
        elif ext == ".png":
            query["response-content-type"] = "image/png"
        elif ext == ".webp":
            query["response-content-type"] = "image/webp"
    return query


def cmd_env(args: argparse.Namespace) -> int:
    env = _load_env(args)
    _print_env(env)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    env = _load_env(args)
    client = _make_client(env)

    local_file = args.file
    if not os.path.exists(local_file):
        cwd = os.getcwd()
        raise SystemExit(f"本地文件不存在：{local_file}（cwd={cwd}）")

    key = args.key
    if not key:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        key = f"debug/upload/{ts}_{os.path.basename(local_file)}"

    content_type = args.content_type
    if not content_type:
        ext = os.path.splitext(key)[1].lower()
        if ext in {".jpg", ".jpeg"}:
            content_type = "image/jpeg"
        elif ext == ".png":
            content_type = "image/png"
        elif ext == ".webp":
            content_type = "image/webp"
        elif ext == ".mp4":
            content_type = "video/mp4"

    content_disposition: Optional[str] = None
    if args.inline:
        content_disposition = "inline"
    elif args.attachment:
        filename = os.path.basename(key) or os.path.basename(local_file)
        content_disposition = f'attachment; filename="{filename}"'

    forbid_overwrite = not args.overwrite

    out = client.put_object_from_file(
        bucket=env.bucket,
        key=key,
        file_path=local_file,
        content_type=content_type,
        content_disposition=content_disposition,
        forbid_overwrite=forbid_overwrite,
    )

    print("Upload OK:")
    print(f"- bucket : {env.bucket}")
    print(f"- key    : {key}")
    print(f"- etag   : {getattr(out, 'etag', None)}")
    return 0


def cmd_upload_demo(args: argparse.Namespace) -> int:
    """
    上传内置 1x1 PNG（用于快速验证 PutObject 权限、Content-Disposition、signed URL 等）
    """
    env = _load_env(args)
    client = _make_client(env)

    key = args.key
    if not key:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        key = f"debug/demo/{ts}_1x1.png"

    # 1x1 transparent PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00"
        b"\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    content_disposition: Optional[str] = None
    if args.inline:
        content_disposition = "inline"
    elif args.attachment:
        filename = os.path.basename(key) or "demo.png"
        content_disposition = f'attachment; filename="{filename}"'

    forbid_overwrite = not args.overwrite

    out = client.put_object(
        bucket=env.bucket,
        key=key,
        content=png_bytes,
        content_length=len(png_bytes),
        content_type="image/png",
        content_disposition=content_disposition,
        forbid_overwrite=forbid_overwrite,
    )

    print("Upload demo OK:")
    print(f"- bucket : {env.bucket}")
    print(f"- key    : {key}")
    print(f"- etag   : {getattr(out, 'etag', None)}")
    return 0


def cmd_head(args: argparse.Namespace) -> int:
    env = _load_env(args)
    client = _make_client(env)

    resp = client.head_object(bucket=env.bucket, key=args.key)

    def g(name: str) -> Any:
        return getattr(resp, name, None)

    print("HEAD OK:")
    print(f"- bucket             : {env.bucket}")
    print(f"- key                : {args.key}")
    print(f"- etag               : {g('etag')}")
    print(f"- content_length     : {g('content_length')}")
    print(f"- content_type       : {g('content_type')}")
    print(f"- content_disposition: {g('content_disposition')}")
    print(f"- last_modified      : {g('last_modified')}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    env = _load_env(args)
    client = _make_client(env)

    try:
        from tos.enum import HttpMethodType  # type: ignore
    except Exception as e:
        raise SystemExit(f"tos SDK enum 导入失败：{e}") from e

    method = (args.method or "GET").upper()
    method_map = {
        "GET": HttpMethodType.Http_Method_Get,
        "HEAD": HttpMethodType.Http_Method_Head,
        "PUT": HttpMethodType.Http_Method_Put,
    }
    if method not in method_map:
        raise SystemExit("method 仅支持 GET/HEAD/PUT")

    purpose = args.purpose
    query = _signed_query_for_purpose(args.key, purpose) if purpose else {}
    if args.response_content_disposition:
        query["response-content-disposition"] = args.response_content_disposition
    if args.response_content_type:
        query["response-content-type"] = args.response_content_type

    result = client.pre_signed_url(
        http_method=method_map[method],
        bucket=env.bucket,
        key=args.key,
        expires=args.expires,
        query=query or None,
    )

    url = getattr(result, "signed_url", None) or getattr(result, "signedUrl", None) or str(result)

    print("Signed URL:")
    print(f"- bucket : {env.bucket}")
    print(f"- key    : {args.key}")
    print(f"- method : {method}")
    print(f"- expires: {args.expires}s")
    if query:
        print(f"- query  : {query}")
    print()
    if args.redact_url:
        print(_mask(url, left=32, right=16))
    else:
        # 注意：signed url 含签名参数，避免粘贴到公共渠道；过期后自动失效
        print(url)
        print()
        print("Quick check:")
        print(f"  curl -I {url!r}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    env = _load_env(args)
    client = _make_client(env)

    out_path = args.out
    if not out_path:
        base = os.path.basename(args.key) or "download.bin"
        out_path = os.path.join("/tmp", base)

    resp = client.get_object(bucket=env.bucket, key=args.key)
    body = getattr(resp, "content", None) or getattr(resp, "body", None)
    if body is None:
        raise SystemExit("下载失败：响应无 body/content")

    # body 可能是流式对象，尽量兼容
    with open(out_path, "wb") as f:
        if hasattr(body, "read"):
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        else:
            f.write(body)

    print("Download OK:")
    print(f"- bucket : {env.bucket}")
    print(f"- key    : {args.key}")
    print(f"- out    : {out_path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    env = _load_env(args)
    client = _make_client(env)

    prefix = args.prefix or ""
    max_keys = args.max_keys

    items = []

    # SDK 版本差异：优先使用 list_objects_type2，否则回退 list_objects
    if hasattr(client, "list_objects_type2"):
        resp = client.list_objects_type2(bucket=env.bucket, prefix=prefix, max_keys=max_keys)
        items = getattr(resp, "contents", None) or getattr(resp, "contents_list", None) or []
    else:
        resp = client.list_objects(bucket=env.bucket, prefix=prefix, max_keys=max_keys)
        items = getattr(resp, "contents", None) or []

    print("List OK:")
    print(f"- bucket   : {env.bucket}")
    print(f"- prefix   : {prefix!r}")
    print(f"- max_keys : {max_keys}")
    print(f"- count    : {len(items)}")
    print()

    for it in items:
        key = getattr(it, "key", None) or getattr(it, "Key", None) or str(it)
        size = getattr(it, "size", None) or getattr(it, "Size", None)
        lm = getattr(it, "last_modified", None) or getattr(it, "LastModified", None)
        print(f"- {key}  size={size}  last_modified={lm}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TOS 私有对象签名/上传/查看 测试脚本")
    p.add_argument("--ak", default=None, help="覆盖 TOS_ACCESS_KEY")
    p.add_argument("--sk", default=None, help="覆盖 TOS_SECRET_KEY")
    p.add_argument("--region", default=None, help="覆盖 TOS_REGION")
    p.add_argument("--endpoint", default=None, help="覆盖 TOS_ENDPOINT")
    p.add_argument("--bucket", default=None, help="覆盖 TOS_BUCKET")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("env", help="打印当前 TOS 配置（脱敏）")
    sp.set_defaults(func=cmd_env)

    sp = sub.add_parser("upload", help="上传本地文件到 TOS")
    sp.add_argument("--file", required=True, help="本地文件路径")
    sp.add_argument("--key", default=None, help="目标对象 key（不传则自动生成 debug/upload/...）")
    sp.add_argument("--content-type", default=None, help="覆盖 Content-Type（如 image/jpeg）")
    sp.add_argument("--inline", action="store_true", help="写入 Content-Disposition: inline（建议缩略图/图片）")
    sp.add_argument("--attachment", action="store_true", help="写入 Content-Disposition: attachment")
    sp.add_argument("--overwrite", action="store_true", help="允许覆盖同 key（默认禁止覆盖）")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser("upload-demo", help="上传内置 1x1 PNG（不依赖本地文件）")
    sp.add_argument("--key", default=None, help="目标对象 key（不传则自动生成 debug/demo/...）")
    sp.add_argument("--inline", action="store_true", help="写入 Content-Disposition: inline")
    sp.add_argument("--attachment", action="store_true", help="写入 Content-Disposition: attachment")
    sp.add_argument("--overwrite", action="store_true", help="允许覆盖同 key（默认禁止覆盖）")
    sp.set_defaults(func=cmd_upload_demo)

    sp = sub.add_parser("head", help="HEAD 查看对象元信息")
    sp.add_argument("--key", required=True, help="对象 key")
    sp.set_defaults(func=cmd_head)

    sp = sub.add_parser("sign", help="生成 signed URL（GET/HEAD/PUT）")
    sp.add_argument("--key", required=True, help="对象 key")
    sp.add_argument("--method", default="GET", help="GET/HEAD/PUT（默认 GET）")
    sp.add_argument("--expires", type=int, default=300, help="过期秒数（默认 300）")
    sp.add_argument("--purpose", default="preview", choices=["preview", "download", "thumbnail"], help="常用用途（影响 inline/attachment 与 response-content-type）")
    sp.add_argument("--response-content-disposition", default=None, help="手动覆盖 response-content-disposition")
    sp.add_argument("--response-content-type", default=None, help="手动覆盖 response-content-type")
    sp.add_argument("--redact-url", action="store_true", help="脱敏打印 URL（默认打印完整 URL 便于测试）")
    sp.set_defaults(func=cmd_sign)

    sp = sub.add_parser("download", help="下载对象到本地文件")
    sp.add_argument("--key", required=True, help="对象 key")
    sp.add_argument("--out", default=None, help="输出路径（默认 /tmp/<basename>）")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("list", help="按 prefix 列举对象")
    sp.add_argument("--prefix", default="", help="前缀")
    sp.add_argument("--max-keys", type=int, default=20, help="最多返回条数（默认 20）")
    sp.set_defaults(func=cmd_list)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

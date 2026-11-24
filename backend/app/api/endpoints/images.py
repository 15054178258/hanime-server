"""
v4.0.3: 外部图片代理（带磁盘缓存）

背景：前端 <img> 直接加载外部 CDN（vdownload.hembed.com 等）的封面/头像，
浏览器直连境外 CDN 时常因网络抖动加载失败（重进页面才恢复）。
本接口让图片走后端的稳定网络通道（可复用已配置的代理），
并在本地磁盘缓存，首次成功后后续请求直接秒开。

安全性：
- 仅允许 http/https 且禁止内网地址（防 SSRF）
- 响应必须是 image/* 且限制最大体积
"""

import asyncio
import hashlib
import os
import time
from typing import Dict, Optional, Set

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from app.config import settings, logger
from app.utils.auth import verify_token

images_router = APIRouter()

# 图片缓存目录（随 DATA_ROOT 持久化）
IMAGE_CACHE_DIR = settings.DATA_ROOT / "image_cache"
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 单张图片最大体积（10MB 足够覆盖封面/头像/横幅）
MAX_IMAGE_SIZE = 10 * 1024 * 1024

# 缓存总量上限（默认 512MB，<=0 表示不限制）
CACHE_MAX_BYTES = settings.IMAGE_CACHE_MAX_MB * 1024 * 1024
# 超限时清理到上限的 80%，避免频繁触发
CACHE_TRIM_RATIO = 0.8
# 清理检查节流（秒）
_CLEANUP_INTERVAL = 300
_last_cleanup_ts = -1e9  # 首次写入即触发一次检查

# 允许的图片 MIME 类型
ALLOWED_IMAGE_TYPES: Set[str] = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif",
}

# content-type -> 扩展名
EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}

# 并发去重：同一 URL 只发一次上游请求
_url_locks: Dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

# 共享的 httpx 客户端（懒创建）
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        proxy = settings.PROXY_URL if settings.USE_PROXY else None
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            proxy=proxy,
            headers={
                "User-Agent": settings.USER_AGENT,
                "Referer": settings.HANIME_BASE_URL,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
    return _client


def _is_private_host(hostname: str) -> bool:
    """阻止 SSRF：禁止内网/回环/链路本地地址"""
    hostname = hostname.lower().strip(".")
    if hostname in ("localhost", "localhost.localdomain"):
        return True
    parts = hostname.split(".")
    # IPv4 字面量检查
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a in (0, 10, 127):
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        if a == 169 and b == 254:
            return True
    return False


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url 参数")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="仅支持 http/https 图片地址")
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 URL")
    if not host or _is_private_host(host):
        raise HTTPException(status_code=400, detail="不允许代理该地址")
    return url


def _cache_path_for(url: str, content_type: str) -> "object":
    """根据 URL 生成缓存文件路径（含正确的扩展名）"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    ext = EXT_BY_TYPE.get(content_type, ".jpg")
    return IMAGE_CACHE_DIR / f"{digest}{ext}"


def _find_existing_cache(url: str):
    """按 URL 哈希查找任意扩展名的已有缓存"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    for ext in EXT_BY_TYPE.values():
        candidate = IMAGE_CACHE_DIR / f"{digest}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate, EXT_BY_TYPE[ext]
    return None, None


def _media_type(ext: str) -> str:
    return "image/" + ext.lstrip(".").replace("jpg", "jpeg")


def _serve_cached_file(cache_file, ext: str) -> FileResponse:
    """返回缓存文件；同时刷新 mtime 用于 LRU 淘汰（1 小时内不重复刷新）"""
    try:
        if time.time() - cache_file.stat().st_mtime > 3600:
            os.utime(cache_file, None)
    except OSError:
        pass
    return FileResponse(
        path=cache_file,
        media_type=_media_type(ext),
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _cache_stats_sync():
    """统计缓存文件数与总大小（字节）"""
    count, total = 0, 0
    for f in IMAGE_CACHE_DIR.iterdir():
        try:
            if f.is_file():
                count += 1
                total += f.stat().st_size
        except OSError:
            continue
    return count, total


def _enforce_cache_limit_sync():
    """超出上限时按最久未访问（mtime）淘汰，清理到上限的 80%"""
    if CACHE_MAX_BYTES <= 0:
        return
    entries = []
    total = 0
    for f in IMAGE_CACHE_DIR.iterdir():
        try:
            if not f.is_file():
                continue
            st = f.stat()
            entries.append((st.st_mtime, st.st_size, f))
            total += st.st_size
        except OSError:
            continue
    if total <= CACHE_MAX_BYTES:
        return

    target = int(CACHE_MAX_BYTES * CACHE_TRIM_RATIO)
    entries.sort(key=lambda e: e[0])  # 最久未访问的先删
    removed, freed = 0, 0
    for _, size, path in entries:
        if total <= target:
            break
        try:
            path.unlink()
            removed += 1
            freed += size
            total -= size
        except OSError:
            continue
    if removed:
        logger.info(
            f"图片缓存超限，已清理 {removed} 个文件，释放 {freed / 1024 / 1024:.1f}MB，"
            f"当前约 {total / 1024 / 1024:.1f}MB（上限 {CACHE_MAX_BYTES / 1024 / 1024:.0f}MB）"
        )


async def _maybe_enforce_cache_limit():
    """节流执行缓存清理（默认 5 分钟内最多检查一次），在线程池中执行避免阻塞事件循环"""
    global _last_cleanup_ts
    if CACHE_MAX_BYTES <= 0:
        return
    now = time.monotonic()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL:
        return
    _last_cleanup_ts = now
    try:
        await asyncio.to_thread(_enforce_cache_limit_sync)
    except Exception as e:
        logger.warning(f"清理图片缓存失败: {e}")


@images_router.get("/cache/stats")
async def image_cache_stats():
    """查看图片缓存占用情况"""
    count, total = await asyncio.to_thread(_cache_stats_sync)
    return {
        "count": count,
        "size_bytes": total,
        "size_mb": round(total / 1024 / 1024, 2),
        "max_mb": settings.IMAGE_CACHE_MAX_MB,
        "dir": str(IMAGE_CACHE_DIR),
    }


@images_router.post("/cache/clear")
async def clear_image_cache(token: Optional[str] = Query(None)):
    """清空图片缓存（需要登录 token，通过 ?token=xxx 传入）"""
    if not token:
        raise HTTPException(status_code=401, detail="缺少认证 token，请通过 ?token=xxx 参数提供")
    verify_token(token)

    def _clear_sync():
        removed, freed = 0, 0
        for f in IMAGE_CACHE_DIR.iterdir():
            try:
                if f.is_file():
                    size = f.stat().st_size
                    f.unlink()
                    removed += 1
                    freed += size
            except OSError:
                continue
        return removed, freed

    removed, freed = await asyncio.to_thread(_clear_sync)
    logger.info(f"图片缓存已清空: {removed} 个文件, {freed / 1024 / 1024:.1f}MB")
    return {"success": True, "removed": removed, "freed_mb": round(freed / 1024 / 1024, 2)}


@images_router.get("/proxy")
async def proxy_image(url: str = Query(...), refresh: bool = Query(False)):
    """
    代理外部图片并缓存到本地磁盘。
    - 首次请求：后端拉取上游图片，写缓存后返回
    - 后续请求：直接读本地缓存（秒开）
    - 上游失败但缓存存在：返回旧缓存（保证可用性）
    - ?refresh=true：强制刷新缓存
    - 缓存总量超过 IMAGE_CACHE_MAX_MB 时自动按最久未访问淘汰
    """
    url = _validate_url(url)

    # 1. 命中缓存直接返回
    if not refresh:
        cache_file, ext = _find_existing_cache(url)
        if cache_file is not None:
            return _serve_cached_file(cache_file, ext)

    # 2. 并发去重：同一 URL 加锁，避免重复拉取
    async with _locks_guard:
        lock = _url_locks.setdefault(url, asyncio.Lock())
    async with lock:
        # 双重检查：等锁期间可能已被其他请求写入
        if not refresh:
            cache_file, ext = _find_existing_cache(url)
            if cache_file is not None:
                return _serve_cached_file(cache_file, ext)

        # 3. 拉取上游图片
        try:
            client = _get_client()
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"图片代理拉取失败: {url} - {e}")
            # 拉取失败但有旧缓存（refresh 场景）则回退旧缓存
            cache_file, ext = _find_existing_cache(url)
            if cache_file is not None:
                return _serve_cached_file(cache_file, ext)
            raise HTTPException(status_code=502, detail="图片拉取失败，请稍后重试")

        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=502, detail="上游返回的不是图片")
        data = resp.content
        if len(data) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=502, detail="图片体积超出限制")

        # 4. 写缓存并返回
        cache_file = _cache_path_for(url, content_type)
        try:
            cache_file.write_bytes(data)
        except Exception as e:
            logger.warning(f"图片缓存写入失败: {e}")

        # 写入后按需触发容量清理（节流，线程池执行）
        await _maybe_enforce_cache_limit()

        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

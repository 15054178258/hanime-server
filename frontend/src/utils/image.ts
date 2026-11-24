/**
 * v4.0.3: 外部图片代理工具
 *
 * 背景：封面/头像等图片原本由浏览器直接从外部 CDN 加载，
 * 网络抖动时常加载失败（重进页面才恢复）。
 * 现在统一改写为走后端 /api/images/proxy 代理（带磁盘缓存），
 * 与 API 同通道，加载稳定。
 */

import request from './request.ts';

/** 需要代理改写的图片字段 */
const IMAGE_URL_KEYS = new Set(['cover_url', 'user_avatar', 'icon_url']);

/**
 * 将外部图片 URL 改写为后端代理地址
 * 本地/相对地址、空值原样返回
 */
export function proxiedImageUrl(url: string | undefined | null): string {
  if (!url) return '';
  // 相对路径（本地封面 /api/downloads/cover/... 等）不走代理
  if (!url.startsWith('http://') && !url.startsWith('https://')) return url;
  // 已经是代理地址则不重复包装
  if (url.includes('/api/images/proxy')) return url;
  const baseURL = request.defaults.baseURL || '/api';
  return `${baseURL}/images/proxy?url=${encodeURIComponent(url)}`;
}

/**
 * 反向还原：从代理地址中提取原始 CDN 地址（用于存库，保持数据原样）
 * 非代理地址原样返回
 */
export function originalImageUrl(url: string | undefined | null): string {
  if (!url) return '';
  if (!url.includes('/api/images/proxy')) return url;
  try {
    const match = url.match(/[?&]url=([^&]+)/);
    return match ? decodeURIComponent(match[1]) : url;
  } catch {
    return url;
  }
}

/**
 * 深度遍历数据，把所有图片字段（cover_url / user_avatar / icon_url）
 * 中的外部 URL 改写为代理地址。返回新对象，不修改原数据。
 */
export function proxyImageUrls<T>(data: T): T {
  if (data == null) return data;

  if (Array.isArray(data)) {
    return data.map((item) => proxyImageUrls(item)) as unknown as T;
  }

  if (typeof data === 'object') {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(data as Record<string, unknown>)) {
      if (IMAGE_URL_KEYS.has(key) && typeof value === 'string') {
        result[key] = proxiedImageUrl(value);
      } else {
        result[key] = proxyImageUrls(value);
      }
    }
    return result as unknown as T;
  }

  return data;
}

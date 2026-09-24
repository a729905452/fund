#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯财经接口客户端。

职责：HTTP 获取，含超时、有限重试、请求间隔、退避与本地缓存。
不做字段解析（解析在 data_layer.py），只返回原始文本/JSON。

约定：
- 所有函数返回原始字符串（行情）或已解析的 JSON dict（K线）
- 写文件统一 UTF-8；接口返回 GBK 时在接收处解码
- 限流退避：HTTP 429 或连续失败时按指数退避，不放大请求
"""
import json
import os
import time
import urllib.error
import urllib.request

# 请求间隔（秒）：同一进程内两次请求的最小间隔，避免触发限流
MIN_REQUEST_INTERVAL = 0.5
# 超时（秒）
DEFAULT_TIMEOUT = 15
# 最大重试次数（不含首次）
MAX_RETRIES = 2

_last_request_ts = 0.0


class FetchError(Exception):
    """取数失败。message 使用中文，便于直接展示。"""


def _throttle():
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_ts = time.time()


def http_get(url, encoding="utf-8", timeout=DEFAULT_TIMEOUT):
    """GET 请求，带间隔、超时与有限重试（429 时退避）。"""
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(encoding, errors="ignore")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < MAX_RETRIES:
                wait = 2 ** attempt  # 1s, 2s 指数退避
                time.sleep(wait)
                continue
            raise FetchError(f"HTTP 请求失败: {e.code} {e.reason}（{url}）")
        except urllib.error.URLError as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(1)
                continue
            raise FetchError(f"网络请求失败: {e.reason}（{url}）")
    raise FetchError(f"请求失败: {last_err}（{url}）")


def build_symbol(code, market):
    """构建腾讯接口 symbol，如 sz000636。仅支持 sh/sz（第一版仅限A股）。"""
    if market not in ("sh", "sz"):
        raise FetchError(f"第一版仅支持 A 股（sh/sz），不支持市场: {market}")
    return f"{market}{code}"


def detect_market(code):
    """按代码规则识别 A 股市场。6开头→sh，0/3开头→sz。"""
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith("6"):
        return "sh"
    if digits.startswith(("0", "3")):
        return "sz"
    return None


def fetch_quote_raw(symbol):
    """获取实时行情原始文本（GBK 解码后）。"""
    return http_get(f"https://qt.gtimg.cn/q={symbol}", encoding="gbk")


def fetch_daily_kline(symbol, count=320, adjust="qfq"):
    """获取日线K线，返回已解析 JSON dict。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,,{count},{adjust}")
    return json.loads(http_get(url))


def fetch_minute_kline(symbol, period=1, count=320):
    """获取分钟K线（period: 1 或 5），返回已解析 JSON dict。"""
    if period not in (1, 5):
        raise FetchError(f"分钟周期仅支持 1/5，收到: {period}")
    url = (f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
           f"?param={symbol},m{period},,{count}")
    return json.loads(http_get(url))


def save_text(path, text):
    """保存文本，UTF-8 无 BOM。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def save_json(path, obj):
    """保存 JSON，UTF-8 无 BOM，保留中文。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

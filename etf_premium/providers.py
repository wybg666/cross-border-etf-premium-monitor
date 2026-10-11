"""Read-only Tencent ETF quotes; only standard-library networking.

Field mapping: https://st.gtimg.com/quotes/hs-fund/bundle.13362df9.js
Tencent's adaptHS maps index 78 to IOPV and 81 to a separate NAV field.
Index 30 timestamps the quote packet, not IOPV. No IOPV date is fabricated.
"""
from __future__ import annotations

import http.client
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
SOURCE_NAME = "腾讯财经公开行情"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 5
REQUEST_ATTEMPTS = 2
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_SYMBOL = re.compile(r"(?:sh|sz)[0-9]{6}\Z")
_ASSIGNMENT = re.compile(r'(?<![A-Za-z0-9_$])(?:var\s+)?v_((?:sh|sz)[0-9]{6})\s*=\s*"([^"\r\n]*)"\s*;')


class ProviderError(RuntimeError):
    """A safe category/message, without underlying URLs or proxy credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    return int(number) if number is not None else None


def _quote_timestamp(value: str) -> Optional[int]:
    if not re.fullmatch(r"[0-9]{14}", value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=_SHANGHAI_TZ)
        return int(parsed.timestamp())
    except (ValueError, OSError, OverflowError):
        return None


def _safe_error(error: Exception) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 407:
            return ProviderError("proxy_auth", "网络代理需要认证，无法取得公开行情")
        if error.code == 429:
            return ProviderError("rate_limited", "行情服务请求过于频繁，请稍后重试")
        return ProviderError("http_error", f"行情服务返回 HTTP {int(error.code)}")
    if isinstance(error, urllib.error.URLError):
        if isinstance(error.reason, Exception):
            return _safe_error(error.reason)
        return ProviderError("network_error", "无法连接公开行情服务，请检查网络或代理设置")
    if isinstance(error, ssl.SSLCertVerificationError):
        return ProviderError("tls_certificate", "行情服务 TLS 证书验证失败；未关闭证书验证")
    if isinstance(error, ssl.SSLError):
        return ProviderError("tls_error", "行情服务 TLS 连接失败")
    if isinstance(error, (TimeoutError, socket.timeout)):
        return ProviderError("timeout", "公开行情请求超时")
    if isinstance(error, socket.gaierror):
        return ProviderError("dns_error", "行情域名解析失败")
    if isinstance(error, UnicodeError):
        return ProviderError("invalid_encoding", "行情响应不是预期的 GBK 编码")
    if isinstance(error, http.client.HTTPException):
        return ProviderError("incomplete_response", "行情连接中断或响应不完整")
    return ProviderError("network_error", "无法连接公开行情服务，请检查网络或代理设置")


def _parse_tencent_quotes(text: str, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    requested = set(symbols)
    rows: Dict[str, Dict[str, Any]] = {}
    for match in _ASSIGNMENT.finditer(text):
        symbol, raw = match.groups()
        if symbol not in requested:
            continue
        fields = raw.split("~")
        if len(fields) < 79:
            raise ProviderError("invalid_response", "行情字段不完整，无法确认 IOPV")
        code = symbol[2:]
        if fields[2] != code:
            raise ProviderError("symbol_mismatch", "行情证券代码与请求不一致")
        if code in rows:
            raise ProviderError("invalid_response", "行情响应包含重复证券记录")
        # The first-party UI displays field 37 in ten-thousand CNY. Field 57
        # is the higher-precision amount; 57*10000 matches the CNY component
        # of field 35 in independently checked ETF samples.
        amount_wan = _num(fields[57])
        amount_cny = _num(amount_wan * 10000) if amount_wan is not None else None
        rows[code] = {
            "code": code,
            "symbol": symbol,
            "name": fields[1],
            "marketPrice": _num(fields[3]),
            "iopv": _num(fields[78]),
            "providerPremiumPct": _num(fields[77]),
            "dayChangePct": _num(fields[32]),
            "dayChange": _num(fields[31]),
            "previousClose": _num(fields[4]),
            "volume": _num(fields[36]),
            "volumeUnit": "手",
            "amount": round(amount_cny, 2) if amount_cny is not None else None,
            "bidPrice": _num(fields[9]),
            "askPrice": _num(fields[19]),
            "quoteTimestamp": _quote_timestamp(fields[30]),
        }
    if not rows:
        raise ProviderError("empty_response", "腾讯行情未返回请求证券的有效记录")
    return rows


def fetch_tencent_quotes(symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """One batch, at most two five-second attempts; do not evaluate returned JS.

    Volume is in lots (手), amount in CNY, prices/IOPV in CNY per ETF unit.
    Missing numeric fields stay None; there is no NAV/old-data fallback.
    Standard urllib proxy behavior and TLS verification are preserved.
    """
    if isinstance(symbols, (str, bytes)):
        raise ProviderError("invalid_symbols", "证券列表格式无效")
    requested = list(symbols)
    if not requested or len(requested) > 100:
        raise ProviderError("invalid_symbols", "每批需包含 1 至 100 个证券代码")
    if any(not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None for symbol in requested):
        raise ProviderError("invalid_symbols", "仅支持 sh/sz 前缀的六位证券代码")
    if len(set(requested)) != len(requested) or len({symbol[2:] for symbol in requested}) != len(requested):
        raise ProviderError("invalid_symbols", "证券列表包含重复代码")
    url = TENCENT_QUOTE_URL + urllib.parse.quote(",".join(requested), safe=",")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gu.qq.com/",
        "Accept": "text/plain,*/*",
        "Accept-Encoding": "identity",
    }
    safe_error = ProviderError("network_error", "无法取得公开行情")
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError("response_too_large", "行情响应超过允许大小")
            return _parse_tencent_quotes(raw.decode("gbk"), requested)
        except (ProviderError, urllib.error.URLError, OSError, UnicodeError, http.client.HTTPException) as exc:
            safe_error = _safe_error(exc)
            if attempt + 1 < REQUEST_ATTEMPTS:
                time.sleep(0.25)
    raise safe_error from None

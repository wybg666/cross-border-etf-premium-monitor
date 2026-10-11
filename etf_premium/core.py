from __future__ import annotations

import copy
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .providers import TENCENT_QUOTE_URL, SOURCE_NAME, fetch_tencent_quotes, _int, _num
from .trading_calendar import is_calendar_supported, is_cn_trading_day


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ETF_PREMIUM_TTL_SECONDS = 20
QUOTE_STALE_SECONDS = 180
PREMIUM_RECONCILIATION_TOLERANCE_PCT = 0.05
ALERT_PREMIUM_PCT = 2.0


@dataclass(frozen=True)
class CrossBorderEtfSpec:
    code: str
    name: str
    secid: str
    benchmark_key: str
    benchmark_label: str


def _spec(code: str, name: str, benchmark_key: str, benchmark_label: str) -> CrossBorderEtfSpec:
    market = "1" if code.startswith("5") else "0"
    return CrossBorderEtfSpec(code, name, f"{market}.{code}", benchmark_key, benchmark_label)


CROSS_BORDER_ETF_UNIVERSE: Tuple[CrossBorderEtfSpec, ...] = (
    _spec("159509", "纳指科技ETF景顺", "nasdaqtech", "纳斯达克科技市值加权"),
    _spec("159501", "纳指ETF嘉实", "nasdaq100", "纳斯达克 100"),
    _spec("159941", "纳指ETF广发", "nasdaq100", "纳斯达克 100"),
    _spec("513100", "纳指ETF国泰", "nasdaq100", "纳斯达克 100"),
    _spec("159696", "纳指ETF易方达", "nasdaq100", "纳斯达克 100"),
    _spec("513300", "纳斯达克ETF华夏", "nasdaq100", "纳斯达克 100"),
    _spec("159659", "纳斯达克100ETF招商", "nasdaq100", "纳斯达克 100"),
    _spec("513870", "纳指ETF富国", "nasdaq100", "纳斯达克 100"),
    _spec("159660", "纳指ETF汇添富", "nasdaq100", "纳斯达克 100"),
    _spec("159632", "纳斯达克ETF华安", "nasdaq100", "纳斯达克 100"),
    _spec("513390", "纳指100ETF博时", "nasdaq100", "纳斯达克 100"),
    _spec("513110", "纳指ETF华泰柏瑞", "nasdaq100", "纳斯达克 100"),
    _spec("159513", "纳斯达克100ETF大成", "nasdaq100", "纳斯达克 100"),
    _spec("513500", "标普500ETF博时", "sp500", "标普 500"),
    _spec("159612", "标普500ETF国泰", "sp500", "标普 500"),
    _spec("513650", "标普500ETF南方", "sp500", "标普 500"),
    _spec("159655", "标普500ETF华夏", "sp500", "标普 500"),
)

PREMIUM_THRESHOLDS: Dict[str, Any] = {
    "alertPct": ALERT_PREMIUM_PCT,
    "reconciliationTolerancePctPoint": PREMIUM_RECONCILIATION_TOLERANCE_PCT,
    "bands": [
        {"key": "extreme", "label": "极高溢价", "minimumPct": 10.0},
        {"key": "high", "label": "高溢价", "minimumPct": 5.0},
        {"key": "elevated", "label": "告警", "minimumPct": 2.0},
        {"key": "watch", "label": "偏高", "minimumPct": 0.5},
        {"key": "normal", "label": "平价附近", "minimumPct": -0.5},
        {"key": "discount", "label": "折价", "maximumPct": -0.5},
    ],
}

_CACHE: Dict[str, Any] = {"timestamp": 0.0, "payload": None}
_CACHE_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()


def calculate_premium_pct(market_price: Any, iopv: Any) -> Optional[float]:
    price = _num(market_price)
    reference = _num(iopv)
    if price is None or reference is None or price <= 0 or reference <= 0:
        return None
    computed = _num((price / reference - 1.0) * 100.0)
    return round(computed, 4) if computed is not None else None


def premium_band(premium_pct: Any) -> str:
    value = _num(premium_pct)
    if value is None:
        return "missing"
    if value >= 10.0:
        return "extreme"
    if value >= 5.0:
        return "high"
    if value >= 2.0:
        return "elevated"
    if value >= 0.5:
        return "watch"
    if value > -0.5:
        return "normal"
    return "discount"


def _quote_datetime(value: Any) -> Optional[datetime]:
    timestamp = _int(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, SHANGHAI_TZ)
    except (OSError, OverflowError, ValueError):
        return None


def _market_phase(now: datetime) -> Dict[str, Any]:
    current = now.astimezone(SHANGHAI_TZ)
    if not is_calendar_supported(current.date()):
        return {"key": "unknown", "label": "交易日历未覆盖此年份", "tradingDay": None}
    trading_day = is_cn_trading_day(current.date())
    minute = current.hour * 60 + current.minute
    open_session = trading_day and (
        9 * 60 + 15 <= minute <= 11 * 60 + 35
        or 12 * 60 + 55 <= minute <= 15 * 60 + 5
    )
    if open_session:
        phase = "open"
        label = "A股交易时段"
    elif trading_day and 11 * 60 + 35 < minute < 12 * 60 + 55:
        phase = "break"
        label = "A股午间休市"
    else:
        phase = "closed"
        label = "A股非交易时段"
    return {"key": phase, "label": label, "tradingDay": trading_day}


def _source_url(spec: CrossBorderEtfSpec) -> str:
    exchange = "sh" if spec.secid.startswith("1.") else "sz"
    return f"https://gu.qq.com/{exchange}{spec.code}"


def _quality(key: str, message: str) -> Dict[str, str]:
    labels = {
        "live": "报价时效通过",
        "closed": "盘后快照",
        "delayed": "行情陈旧",
        "inconsistent": "口径异常",
        "missing": "数据缺失",
        "unknown": "日历待更新",
    }
    return {"key": key, "label": labels.get(key, key), "message": message}


def _missing_item(spec: CrossBorderEtfSpec, reason: str) -> Dict[str, Any]:
    quality = _quality("missing", reason)
    return {
        "code": spec.code,
        "name": spec.name,
        "secid": spec.secid,
        "benchmarkKey": spec.benchmark_key,
        "benchmarkLabel": spec.benchmark_label,
        "marketPrice": None,
        "iopv": None,
        "referenceValue": None,
        "referenceType": "IOPV 参考估值",
        "providerPremiumPct": None,
        "reconciliationGapPctPoint": None,
        "premiumPct": None,
        "premiumBand": "missing",
        "isAlert": False,
        "dayChangePct": None,
        "dayChange": None,
        "previousClose": None,
        "volume": None,
        "amount": None,
        "bidPrice": None,
        "askPrice": None,
        "spreadPct": None,
        "quoteTimestamp": None,
        "quoteTime": None,
        "quoteAgeSeconds": None,
        "tradeDate": None,
        "referenceTime": None,
        "referenceTimePrecision": "not_provided",
        "referenceTimeNote": "腾讯未提供 IOPV 独立日期或时分秒；报价时间不等于估值时间",
        "volumeUnit": "手",
        "quality": quality,
        "qualityStatus": quality["key"],
        "stale": True,
        "fromCache": False,
        "source": SOURCE_NAME,
        "sourceUrl": _source_url(spec),
    }


def _build_item(
    spec: CrossBorderEtfSpec,
    raw: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    if not isinstance(raw, Mapping):
        return _missing_item(spec, "腾讯财经公开行情未返回该标的")

    price = _num(raw.get("marketPrice"))
    iopv = _num(raw.get("iopv"))
    provider_premium = _num(raw.get("providerPremiumPct"))
    quote_dt = _quote_datetime(raw.get("quoteTimestamp"))
    missing_fields: List[str] = []
    if price is None or price <= 0:
        missing_fields.append("最新价")
    if iopv is None or iopv <= 0:
        missing_fields.append("IOPV")
    if provider_premium is None:
        missing_fields.append("溢价率校验字段")
    if quote_dt is None:
        missing_fields.append("行情包时间")

    item = _missing_item(spec, "、".join(missing_fields) + "缺失或无效" if missing_fields else "")
    item.update(
        {
            "name": str(raw.get("name") or spec.name),
            "marketPrice": price,
            "iopv": iopv,
            "referenceValue": iopv,
            "providerPremiumPct": provider_premium,
            "dayChangePct": _num(raw.get("dayChangePct")),
            "dayChange": _num(raw.get("dayChange")),
            "previousClose": _num(raw.get("previousClose")),
            "volume": _num(raw.get("volume")),
            "amount": _num(raw.get("amount")),
            "bidPrice": _num(raw.get("bidPrice")),
            "askPrice": _num(raw.get("askPrice")),
            "quoteTimestamp": _int(raw.get("quoteTimestamp")),
            "quoteTime": quote_dt.strftime("%Y-%m-%d %H:%M:%S") if quote_dt else None,
            "tradeDate": quote_dt.date().isoformat() if quote_dt else None,
            "referenceTime": None,
        }
    )

    bid = _num(item.get("bidPrice"))
    ask = _num(item.get("askPrice"))
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        midpoint = (bid + ask) / 2.0
        item["spreadPct"] = round((ask - bid) / midpoint * 100.0, 4) if midpoint else None

    if missing_fields:
        return item

    computed = calculate_premium_pct(price, iopv)
    gap = _num(abs(float(computed) - float(provider_premium))) if computed is not None else None
    item["reconciliationGapPctPoint"] = round(gap, 4) if gap is not None else None
    if gap is None or gap > PREMIUM_RECONCILIATION_TOLERANCE_PCT:
        item["quality"] = _quality(
            "inconsistent",
            (f"计算溢价与腾讯溢价率差 {gap:.4f} 个百分点，超过 {PREMIUM_RECONCILIATION_TOLERANCE_PCT:.2f}"
             if gap is not None else "数值计算超出有效范围"),
        )
        item["qualityStatus"] = "inconsistent"
        item["stale"] = True
        return item

    raw_quote_age = (current - quote_dt).total_seconds()
    quote_age = max(0.0, raw_quote_age)
    item["quoteAgeSeconds"] = int(quote_age)
    phase = _market_phase(current)
    if raw_quote_age < -60:
        quality = _quality("delayed", "行情包时间在未来，请核对系统时钟及数据源；IOPV 无独立时间")
    elif phase["key"] == "unknown":
        quality = _quality("unknown", "内置交易日历未覆盖当前年份；仅展示快照，不判断盘中时效")
    elif phase["key"] == "open" and quote_age > QUOTE_STALE_SECONDS:
        quality = _quality(
            "delayed",
            f"A股交易时段内行情距当前 {int(quote_age)} 秒；IOPV 无独立时间，无法确认估值时效",
        )
    elif phase["key"] == "open":
        quality = _quality("live", "仅报价时间通过盘中时效阈值；腾讯未提供 IOPV 独立时间，无法确认估值时效")
    else:
        quality = _quality("closed", f"{phase['label']}的行情快照；腾讯未提供 IOPV 独立时间")

    item.update(
        {
            "premiumPct": computed,
            "premiumBand": premium_band(computed),
            "isAlert": bool(computed is not None and computed >= ALERT_PREMIUM_PCT),
            "quality": quality,
            "qualityStatus": quality["key"],
            "stale": quality["key"] in ("delayed", "unknown"),
        }
    )
    return item


def _fetch_quotes() -> Tuple[Dict[str, Mapping[str, Any]], List[Dict[str, Any]]]:
    symbols = [("sh" if spec.secid.startswith("1.") else "sz") + spec.code
               for spec in CROSS_BORDER_ETF_UNIVERSE]
    codes = [spec.code for spec in CROSS_BORDER_ETF_UNIVERSE]
    try:
        rows = fetch_tencent_quotes(symbols)
        missing = [code for code in codes if code not in rows]
        errors = ([{"source": SOURCE_NAME, "codes": missing,
                    "code": "missing_symbols", "error": "腾讯行情未返回部分标的"}]
                  if missing else [])
        return rows, errors
    except Exception as exc:  # one failed fetch returns explicit missing rows
        return {}, [{"source": SOURCE_NAME, "codes": codes,
                     "code": getattr(exc, "code", "provider_error"),
                     "error": str(exc) if hasattr(exc, "code") else "腾讯行情处理失败"}]


def _cached_replacement(item: Mapping[str, Any]) -> Dict[str, Any]:
    recovered = copy.deepcopy(dict(item))
    original_message = quality_message = ""
    quality = recovered.get("quality")
    if isinstance(quality, Mapping):
        original_message = str(quality.get("message") or "")
    quality_message = "刷新未取得该标的，回退最近一次有效内存缓存"
    if original_message:
        quality_message += f"；原状态：{original_message}"
    recovered["quality"] = _quality("delayed", quality_message)
    recovered["qualityStatus"] = "delayed"
    recovered["stale"] = True
    recovered["fromCache"] = True
    return recovered


def _merge_missing_from_cache(items: List[Dict[str, Any]], cached_payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    cached_items = cached_payload.get("items") if isinstance(cached_payload, Mapping) else None
    cached_by_code = {
        str(item.get("code")): item
        for item in (cached_items or [])
        if isinstance(item, Mapping) and item.get("premiumPct") is not None
    }
    merged: List[Dict[str, Any]] = []
    for item in items:
        if item.get("qualityStatus") == "missing" and str(item.get("code")) in cached_by_code:
            merged.append(_cached_replacement(cached_by_code[str(item["code"])]))
        else:
            merged.append(item)
    return merged


def _summary(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    usable = [item for item in items if _num(item.get("premiumPct")) is not None]
    maximum = max(usable, key=lambda item: float(item["premiumPct"]), default=None)
    quality_counts = Counter(str(item.get("qualityStatus") or "missing") for item in items)
    benchmark_counts = Counter(str(item.get("benchmarkKey") or "") for item in items)
    return {
        "totalCount": len(items),
        "usableCount": len(usable),
        "alertCount": sum(1 for item in usable if bool(item.get("isAlert"))),
        "maxPremiumPct": _num(maximum.get("premiumPct")) if maximum else None,
        "maxPremiumCode": str(maximum.get("code")) if maximum else None,
        "maxPremiumName": str(maximum.get("name")) if maximum else None,
        "totalAmount": (round(sum(_num(item.get("amount")) or 0.0 for item in items), 2)
                        if any(_num(item.get("amount")) is not None for item in items) else None),
        "qualityCounts": dict(quality_counts),
        "benchmarkCounts": dict(benchmark_counts),
    }


def _compose_payload(
    items: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    *,
    now: datetime,
) -> Dict[str, Any]:
    summary = _summary(items)
    groups = []
    for key, label in (("nasdaq100", "纳斯达克 100"), ("nasdaqtech", "纳斯达克科技市值加权"), ("sp500", "标普 500")):
        group_items = [copy.deepcopy(item) for item in items if item.get("benchmarkKey") == key]
        groups.append({"key": key, "label": label, "count": len(group_items), "items": group_items})
    quote_times = sorted({str(item["quoteTime"]) for item in items if item.get("quoteTime")})
    phase = _market_phase(now)
    return {
        "ok": summary["usableCount"] > 0,
        "partial": summary["usableCount"] < summary["totalCount"] or bool(errors),
        "generatedAt": now.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "tradeDate": max((str(item["tradeDate"]) for item in items if item.get("tradeDate")), default=None),
        "quoteUpdatedAt": quote_times[-1] if quote_times else None,
        "items": items,
        "groups": groups,
        "summary": summary,
        "errors": errors,
        "thresholds": copy.deepcopy(PREMIUM_THRESHOLDS),
        "formula": "(marketPrice / iopv - 1) * 100",
        "timing": {
            "marketPhase": phase,
            "quoteTimeField": "Tencent quote[30]",
            "referenceDateField": None,
            "referenceTimePrecision": "not_provided",
            "hasIndependentIopvTimestamp": False,
            "note": "腾讯字段 30 是行情包时间；字段 78 为 IOPV，无独立日期或时分秒，不能证明估值与报价同步。",
        },
        "source": {
            "name": SOURCE_NAME,
            "endpoint": TENCENT_QUOTE_URL,
            "fields": {"price": 3, "quoteTime": 30, "amountWanCny": 57, "premiumPct": 77, "iopv": 78},
            "fieldIndexBase": 0,
        },
    }


def _refresh_payload(cached_payload: Optional[Mapping[str, Any]], *, allow_stale: bool) -> Dict[str, Any]:
    raw_by_code, errors = _fetch_quotes()
    now = datetime.now(SHANGHAI_TZ)
    items = [_build_item(spec, raw_by_code.get(spec.code), now=now) for spec in CROSS_BORDER_ETF_UNIVERSE]
    fresh_usable = sum(1 for item in items if item.get("premiumPct") is not None)
    if allow_stale and cached_payload:
        items = _merge_missing_from_cache(items, cached_payload)
    payload = _compose_payload(items, errors, now=now)
    payload["freshUsableCount"] = fresh_usable
    return payload


def _cache_result(payload: Mapping[str, Any], *, state: str, age_seconds: float) -> Dict[str, Any]:
    result = copy.deepcopy(dict(payload))
    result["cache"] = {
        "state": state,
        "ageSeconds": max(0, int(age_seconds)),
        "ttlSeconds": ETF_PREMIUM_TTL_SECONDS,
    }
    return result


def _stale_cache_result(cached: Mapping[str, Any], error_payload: Mapping[str, Any], age_seconds: float) -> Dict[str, Any]:
    items = [_cached_replacement(item) for item in (cached.get("items") or []) if isinstance(item, Mapping)]
    errors = list(copy.deepcopy(error_payload.get("errors") or []))
    errors.append({"source": SOURCE_NAME, "error": "刷新没有取得任何有效标的，已回退最近一次有效内存缓存"})
    result = _compose_payload(items, errors, now=datetime.now(SHANGHAI_TZ))
    result["freshUsableCount"] = 0
    return _cache_result(result, state="stale", age_seconds=age_seconds)


def build_cross_border_etf_premium_payload(
    *,
    force_refresh: bool = False,
    allow_stale: bool = False,
) -> Dict[str, Any]:
    now_epoch = time.time()
    with _CACHE_LOCK:
        cached_timestamp = float(_CACHE.get("timestamp") or 0.0)
        cached_payload = copy.deepcopy(_CACHE.get("payload"))
    cache_age = max(0.0, now_epoch - cached_timestamp) if cached_payload else 0.0
    if cached_payload and not force_refresh and cache_age < ETF_PREMIUM_TTL_SECONDS:
        return _cache_result(cached_payload, state="hit", age_seconds=cache_age)

    with _REFRESH_LOCK:
        if not force_refresh:
            now_epoch = time.time()
            with _CACHE_LOCK:
                cached_timestamp = float(_CACHE.get("timestamp") or 0.0)
                cached_payload = copy.deepcopy(_CACHE.get("payload"))
            cache_age = max(0.0, now_epoch - cached_timestamp) if cached_payload else 0.0
            if cached_payload and cache_age < ETF_PREMIUM_TTL_SECONDS:
                return _cache_result(cached_payload, state="hit", age_seconds=cache_age)

        payload = _refresh_payload(cached_payload, allow_stale=allow_stale)
        fresh_usable = int(payload.get("freshUsableCount") or 0)
        if fresh_usable == 0 and cached_payload and allow_stale:
            return _stale_cache_result(cached_payload, payload, cache_age)
        if fresh_usable > 0 or not allow_stale:
            saved_at = time.time()
            with _CACHE_LOCK:
                _CACHE["timestamp"] = saved_at
                _CACHE["payload"] = copy.deepcopy(payload)
            return _cache_result(payload, state="fresh" if fresh_usable > 0 else "error", age_seconds=0)
        return _cache_result(payload, state="error", age_seconds=0)


def _reset_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE["timestamp"] = 0.0
        _CACHE["payload"] = None

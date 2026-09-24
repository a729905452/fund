#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一数据层与质量检查（M1）。

职责：
- 解析行情/日线/分钟线原始响应为标准结构
- 统一单位：成交量→股、成交额→元（原始值与单位同时保留）
- 记录获取时间、源行情时间、K线区间、周期、复权方式、完成状态
- 质量检查：顺序、重复、缺口、价格有效性、OHLC关系、量额有效性
- 跨接口校验（仅同时间口径）

字段契约见 references/FIELD_CONTRACT.md（M0 实证核定）。
"""
from datetime import datetime, timedelta

# ---------- 标准结构 ----------

def make_bar(ts, open_, close, high, low, volume_shares, amount_yuan=None,
             raw_volume=None, raw_volume_unit=None, raw_amount=None, raw_amount_unit=None,
             complete=True, extra=None):
    """标准K线 bar。内部量=股、额=元；原始值保留。"""
    return {
        "ts": ts,                    # 日线: "YYYY-MM-DD"；分钟: "YYYYMMDDHHmm"
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume_shares,     # 股
        "amount": amount_yuan,       # 元（分钟线无可靠来源时为 None）
        "raw_volume": raw_volume,
        "raw_volume_unit": raw_volume_unit,
        "raw_amount": raw_amount,
        "raw_amount_unit": raw_amount_unit,
        "complete": complete,
        "extra": extra or {},
    }


# ---------- 行情解析 ----------

def parse_quote(raw_text, code, market, fetch_time):
    """解析 qt.gtimg.cn 行情文本为标准 quote dict。"""
    raw_data = raw_text.split('="')[-1].strip().strip('";')
    fields = raw_data.split("~")
    if len(fields) < 46 or not fields[1].strip():
        return {"status": "error", "message": f"未找到股票 {code} 或返回数据不完整"}

    def f(i, cast=float, default=None):
        try:
            v = fields[i].strip()
            return cast(v) if v else default
        except (IndexError, ValueError):
            return default

    # F09：校验响应标的与请求标的一致，错误返回或缓存串标直接拒绝
    resp_code = f(2, str)
    if resp_code and resp_code != code:
        return {"status": "error",
                "message": f"行情响应标的({resp_code})与请求标的({code})不一致，拒绝使用"}
    resp_symbol = raw_text.split("=")[0].strip().lower().lstrip("v_")
    if resp_symbol and resp_symbol != f"{market}{code}".lower():
        return {"status": "error",
                "message": f"行情响应符号({resp_symbol})与请求({market}{code})不一致，拒绝使用"}

    volume_hand = f(6, float, 0) or 0
    amount_wan = f(37, float, 0) or 0
    # field[35]: 价格/成交量(手)/成交额(元)，第三段为精确成交额
    exact_amount = None
    try:
        parts35 = fields[35].split("/")
        if len(parts35) >= 3:
            exact_amount = float(parts35[2])
    except (IndexError, ValueError):
        pass

    return {
        "status": "success",
        "code": f(2, str),
        "name": f(1, str),
        "market": market,
        "price": f(3),
        "prev_close": f(4),
        "open": f(5),
        "high": f(33),
        "low": f(34),
        "change": f(31),
        "change_pct": f(32),
        "volume": int(volume_hand * 100),          # 股
        "amount": exact_amount if exact_amount else amount_wan * 10000,  # 元（优先精确值）
        "pe_ratio": f(39),
        "market_cap_yi": f(45),                     # 亿元
        "snapshot_time": f(30, str),                # 快照时间，非最后成交时间（M0 实证）
        "raw_volume": volume_hand,
        "raw_volume_unit": "手",
        "raw_amount": amount_wan,
        "raw_amount_unit": "万元",
        "raw_field_count": len(fields),
        "fetch_time": fetch_time.isoformat(timespec="seconds"),
    }


# ---------- 日线解析 ----------

# A股收盘时间（市场本地，简单实现；不含节假日历）
MARKET_CLOSE_HHMM = "1500"


def _is_market_closed_for(date_str, as_of):
    """判断 date_str（YYYY-MM-DD）这根日线在 as_of 时点是否已收盘。

    规则：as_of 日期 >  bar日期 → 已收盘
          as_of 日期 == bar日期 → 仅当 as_of 时刻 ≥ 15:00 才收盘
          as_of 日期 <  bar日期 → 未收盘（未来bar，异常，调用方应警觉）
    """
    as_of_date = as_of.strftime("%Y-%m-%d")
    if as_of_date > date_str:
        return True
    if as_of_date < date_str:
        return False
    return as_of.strftime("%H%M") >= MARKET_CLOSE_HHMM


def parse_daily(response_json, symbol, adjust, fetch_time, as_of=None):
    """解析日线响应为标准 bar 列表（F07/F08 修复）。

    - 记录实际返回口径 actual_adjust；请求 qfq 但响应只有 day 时不伪装，
      标记 adjust_mismatch=True（F08）
    - complete 依据 as_of（默认=fetch_time）与市场收盘时间判定，
      不依赖运行当天的系统时钟（F07）
    """
    if as_of is None:
        as_of = fetch_time
    node = response_json.get("data", {}).get(symbol)
    if not node:
        return {"status": "error", "message": f"日线响应中无 {symbol} 数据"}

    requested_key = f"{adjust}day" if adjust in ("qfq", "hfq") else "day"
    raw_bars = node.get(requested_key)
    actual_adjust = adjust
    adjust_mismatch = False
    if not raw_bars:
        # 回退到不复权，必须显式标注，不伪装成请求口径（F08）
        raw_bars = node.get("day")
        actual_adjust = "none"
        adjust_mismatch = (adjust in ("qfq", "hfq"))
    if not raw_bars:
        return {"status": "error", "message": f"日线响应中无 {requested_key}/day 字段"}

    bars = []
    for b in raw_bars:
        # 契约：open,close,high,low 顺序（第2位是close）
        vol_hand = float(b[5])
        bars.append(make_bar(
            ts=b[0],
            open_=float(b[1]), close=float(b[2]), high=float(b[3]), low=float(b[4]),
            volume_shares=int(vol_hand * 100),
            amount_yuan=None,                       # 日线响应无成交额字段
            raw_volume=vol_hand, raw_volume_unit="手",
            complete=_is_market_closed_for(b[0], as_of),
        ))
    result = {
        "status": "success",
        "period": "day",
        "adjust": actual_adjust,            # 实际口径，不伪装
        "requested_adjust": adjust,         # 请求口径
        "adjust_mismatch": adjust_mismatch,  # 口径不一致标记
        "bars": bars,
        "bar_count": len(bars),
        "first_ts": bars[0]["ts"],
        "last_ts": bars[-1]["ts"],
        "fetch_time": fetch_time.isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(timespec="seconds"),
    }
    if adjust_mismatch:
        result["warning"] = (f"请求{adjust}复权但响应未提供，实际为不复权数据；"
                             "跨周期精确价格判断应暂停")
    return result


# ---------- 分钟线解析 ----------

def parse_minute(response_json, symbol, period, fetch_time, as_of=None):
    """解析分钟线响应为标准 bar 列表（F07 修复）。

    契约：时间戳=区间结束；m1 首根 0930 为集合竞价特殊 bar；
    字段[7]非成交额（M0 实证），不使用，amount 置 None。
    complete 依据 as_of（默认=fetch_time），不依赖运行当天的系统时钟。
    """
    if as_of is None:
        as_of = fetch_time
    key = f"m{period}"
    raw_bars = response_json.get("data", {}).get(symbol, {}).get(key)
    if not raw_bars:
        return {"status": "error", "message": f"分钟线响应中无 {key} 数据"}

    bars = []
    for b in raw_bars:
        ts = b[0]
        vol_hand = float(b[5])
        # 时间戳=区间结束：as_of 时刻 ≥ 该bar区间结束时刻 → 已完成
        bar_end = datetime.strptime(ts, "%Y%m%d%H%M")
        complete = as_of >= bar_end
        bars.append(make_bar(
            ts=ts,
            open_=float(b[1]), close=float(b[2]), high=float(b[3]), low=float(b[4]),
            volume_shares=int(vol_hand * 100),
            amount_yuan=None,                       # 字段[7]不可用（M0 实证）
            raw_volume=vol_hand, raw_volume_unit="手",
            raw_amount=b[7] if len(b) > 7 else None,
            raw_amount_unit="未知(已禁用)",
            complete=complete,
        ))
    return {
        "status": "success",
        "period": f"m{period}",
        "adjust": "none",
        "bars": bars,
        "bar_count": len(bars),
        "first_ts": bars[0]["ts"],
        "last_ts": bars[-1]["ts"],
        "fetch_time": fetch_time.isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(timespec="seconds"),
    }


# ---------- 质量检查 ----------

# A股交易时段（分钟）：(09:30,11:30] 与 (13:00,15:00]
def _expected_minute_labels(period):
    """生成一个完整交易日应有的时间戳标签集合（HHmm，区间结束口径）。"""
    labels = set()
    if period == 1:
        labels.add("0930")  # 集合竞价特殊 bar
        t = datetime(2000, 1, 1, 9, 31)
        morning_end = datetime(2000, 1, 1, 11, 30)
        while t <= morning_end:
            labels.add(t.strftime("%H%M")); t += timedelta(minutes=1)
        t = datetime(2000, 1, 1, 13, 1)
        close = datetime(2000, 1, 1, 15, 0)
        while t <= close:
            labels.add(t.strftime("%H%M")); t += timedelta(minutes=1)
    elif period == 5:
        t = datetime(2000, 1, 1, 9, 35)
        morning_end = datetime(2000, 1, 1, 11, 30)
        while t <= morning_end:
            labels.add(t.strftime("%H%M")); t += timedelta(minutes=5)
        t = datetime(2000, 1, 1, 13, 5)
        close = datetime(2000, 1, 1, 15, 0)
        while t <= close:
            labels.add(t.strftime("%H%M")); t += timedelta(minutes=5)
    return labels


def check_bars_quality(dataset, period=None, as_of=None):
    """对标准 bar 列表执行质量检查，返回质量报告。

    dataset: parse_daily/parse_minute 的返回值（status=success）
    as_of: 缺口判定时点（F11），默认取 dataset 的 as_of 或当前时间
    """
    import math
    bars = dataset["bars"]
    issues = []
    warnings = []
    if as_of is None:
        as_of_str = dataset.get("as_of")
        as_of = (datetime.fromisoformat(as_of_str) if as_of_str else datetime.now())

    # 1. 顺序与重复
    ts_list = [b["ts"] for b in bars]
    if ts_list != sorted(ts_list):
        issues.append("时间戳未严格递增")
    dup = len(ts_list) - len(set(ts_list))
    if dup:
        issues.append(f"存在 {dup} 个重复时间戳")

    # 2. 价格有限性、有效性与 OHLC 关系（F10：NaN/inf 一律拦截）
    bad_price = 0
    bad_ohlc = 0
    for b in bars:
        vals = [b["open"], b["close"], b["high"], b["low"]]
        if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in vals):
            bad_price += 1
            continue
        if any(v <= 0 for v in vals):
            bad_price += 1
            continue
        if b["high"] < max(b["open"], b["close"]) or b["low"] > min(b["open"], b["close"]):
            bad_ohlc += 1
    if bad_price:
        issues.append(f"{bad_price} 根 bar 价格无效(缺失/<=0/非有限值)")
    if bad_ohlc:
        issues.append(f"{bad_ohlc} 根 bar 违反 OHLC 关系")

    # 3. 量额有效性（含有限性）
    neg_vol = sum(1 for b in bars
                  if b["volume"] is None or not isinstance(b["volume"], (int, float))
                  or not math.isfinite(b["volume"]) or b["volume"] < 0)
    if neg_vol:
        issues.append(f"{neg_vol} 根 bar 成交量无效")
    if any(b["amount"] is not None for b in bars):
        bad_amt = sum(1 for b in bars
                      if b["amount"] is not None
                      and (not isinstance(b["amount"], (int, float))
                           or not math.isfinite(b["amount"]) or b["amount"] <= 0))
        if bad_amt:
            warnings.append(f"{bad_amt} 根 bar 成交额非正或非有限")

    # 4. 分钟缺口（F11：只排除窗口边缘与尚未发生时段，内部缺失仍报告）
    gaps = []
    if period in (1, 5):
        expected = _expected_minute_labels(period)
        today_compact = as_of.strftime("%Y%m%d")
        now_hhmm = as_of.strftime("%H%M")
        by_day = {}
        for b in bars:
            by_day.setdefault(b["ts"][:8], []).append(b["ts"][-4:])
        sorted_days = sorted(by_day.keys())
        first_day_in_window = sorted_days[0] if sorted_days else None
        for day, labels in sorted(by_day.items()):
            present = set(labels)
            if day == first_day_in_window:
                # 窗口首日被 count 截断：只检查已出现首根之后的内部缺失
                if not labels:
                    continue
                anchor = min(labels)
                internal = sorted(lbl for lbl in expected if lbl >= anchor)
                missing = sorted(set(internal) - present)
            elif day == today_compact:
                # 当日：只检查已发生时段（≤当前时刻）的内部缺失
                happened = sorted(lbl for lbl in expected if lbl <= now_hhmm)
                missing = sorted(set(happened) - present)
            else:
                missing = sorted(expected - present)
            if missing:
                gaps.append({"day": day, "missing_count": len(missing),
                             "missing_sample": missing[:5]})
        if gaps:
            warnings.append(f"{len(gaps)} 个交易日存在分钟缺口")

    usable = len(issues) == 0
    return {
        "usable": usable,
        "issues": issues,
        "warnings": warnings,
        "bar_count": len(bars),
        "first_ts": ts_list[0] if ts_list else None,
        "last_ts": ts_list[-1] if ts_list else None,
        "incomplete_last_bar": bool(bars and not bars[-1]["complete"]),
        "minute_gaps": gaps,
    }


def cross_check_quote_minute(quote, minute_dataset, as_of=None):
    """跨接口校验：当日分钟量求和 vs 行情累计量（同口径才比）。"""
    if as_of is None:
        as_of = datetime.now()
    today_compact = as_of.strftime("%Y%m%d")
    sum_vol = sum(b["volume"] for b in minute_dataset["bars"]
                  if b["ts"].startswith(today_compact))
    qv = quote["volume"]
    if qv <= 0:
        return {"consistent": None, "note": "行情累计量为0，无法校验"}
    diff_pct = abs(sum_vol - qv) / qv
    return {
        "consistent": diff_pct < 0.05,
        "minute_sum_shares": sum_vol,
        "quote_cumulative_shares": qv,
        "diff_pct": round(diff_pct * 100, 4),
        "note": "同口径校验：当日分钟区间量求和 vs 行情累计量",
    }


def check_quote_daily_price_alignment(quote, daily_dataset, as_of=None):
    """核验日线当日 bar 与实时报价的价格口径（F08 修复）。

    仅在满足以下条件时做判断：
    - 日线为前复权（adjust=qfq）且未发生口径回退
    - 当日 bar 与报价属于同一交易日
    当日 bar close 与实时价的差异更可能只是行情变动（日内波动），
    不能据此直接归因为复权变化；只有差异超阈值才提示疑似口径失配。
    """
    if as_of is None:
        as_of = datetime.now()
    if daily_dataset.get("adjust_mismatch"):
        return {"aligned": False, "severity": "mismatch",
                "note": "复权口径回退（请求复权但响应为不复权），暂停跨周期精确价格判断",
                "action": "degrade"}
    if daily_dataset.get("adjust") != "qfq":
        return {"aligned": None, "severity": "skip",
                "note": f"日线口径为{daily_dataset.get('adjust')}，不做qfq锚定校验",
                "action": "none"}
    today = as_of.strftime("%Y-%m-%d")
    today_bars = [b for b in daily_dataset["bars"] if b["ts"] == today]
    if not today_bars:
        return {"aligned": None, "severity": "skip",
                "note": "日线无当日 bar（可能盘后未更新），不校验", "action": "none"}
    bar_close = today_bars[-1]["close"]
    price = quote["price"]
    diff_pct = abs(bar_close - price) / price * 100 if price > 0 else 999
    # 日内行情变动可达数个百分点，0.5%以内视为正常波动，不归因复权
    if diff_pct <= 0.5:
        return {"aligned": True, "severity": "ok",
                "daily_today_close": bar_close, "quote_price": price,
                "diff_pct": round(diff_pct, 3),
                "note": "qfq 口径一致（差异在日内正常波动范围）", "action": "none"}
    return {"aligned": False, "severity": "suspect",
            "daily_today_close": bar_close, "quote_price": price,
            "diff_pct": round(diff_pct, 3),
            "note": (f"当日日线收盘与实时价差异 {round(diff_pct, 2)}% 超阈值；"
                     "可能是行情剧烈变动或复权基准变化，建议重取数据核实"),
            "action": "recheck"}

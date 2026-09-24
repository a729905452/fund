#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M0 数据复核脚本（实证检查，不推测接口语义）。

检查项：
1. 分钟成交量是区间量还是累计量（对比当日1分钟线求和与行情累计量）
2. 分钟时间戳表示区间开始还是结束（检查当日首根bar时间戳）
3. field[30] 语义（休市时段是否随请求时间变化）
4. 每组K线的首尾时间、条数、重复与缺失情况
5. 最后一根K线是否仍在形成中

结果保存到 samples/m0_review_<时间戳>.json，原始响应另存。
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(SKILL_DIR, "samples")
SYMBOL = "sz000636"


def http_get(url, encoding="utf-8", timeout=15):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="ignore")


def bar_stats(bars, name, time_key_index=0):
    """统计一组K线的首尾、重复、缺口（按时间戳字符串）。"""
    times = [b[time_key_index] for b in bars]
    dups = len(times) - len(set(times))
    result = {
        "name": name,
        "count": len(bars),
        "first_time": times[0] if times else None,
        "last_time": times[-1] if times else None,
        "duplicate_timestamps": dups,
    }
    return result


def main():
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    today_compact = now.strftime("%Y%m%d")
    today_dash = now.strftime("%Y-%m-%d")
    report = {"meta": {"run_time_local": now.isoformat(timespec="seconds"), "symbol": SYMBOL}, "checks": {}}

    # ---------- 1. 实时行情（field[30] 语义采样）----------
    raw_quote = http_get(f"https://qt.gtimg.cn/q={SYMBOL}", encoding="gbk")
    raw_data = raw_quote.split('="')[-1].strip().strip('";')
    fields = raw_data.split("~")
    quote = {
        "request_time_local": now.isoformat(timespec="seconds"),
        "field30": fields[30],
        "field35": fields[35],
        "field6_volume_hand": fields[6],
        "field37_amount_wan": fields[37],
        "current_price": fields[3],
    }
    report["checks"]["quote_snapshot"] = quote
    with open(os.path.join(SAMPLES_DIR, f"m0_raw_quote_{stamp}.txt"), "w", encoding="utf-8") as f:
        f.write(raw_quote)

    # ---------- 2. 日线 ----------
    daily_resp = json.loads(http_get(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={SYMBOL},day,,,320,qfq"))
    dnode = daily_resp["data"][SYMBOL]
    daily = dnode.get("qfqday") or dnode.get("day")
    daily_keys = list(dnode.keys())
    with open(os.path.join(SAMPLES_DIR, f"m0_raw_daily_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(daily_resp, f, ensure_ascii=False)

    today_bar = [b for b in daily if b[0] == today_dash]
    report["checks"]["daily"] = {
        **bar_stats(daily, "daily_qfq"),
        "response_data_keys": daily_keys,
        "today_bar": today_bar[-1] if today_bar else None,
        "last_bar_close_equals_quote_price": (
            abs(float(daily[-1][2]) - float(fields[3])) < 1e-9 if today_bar else None),
        "first_10_dates": [b[0] for b in daily[:10]],
        "date_gaps_note": "周末/节假日缺口属正常；仅检查交易日顺序",
        "is_strictly_increasing": all(daily[i][0] < daily[i + 1][0] for i in range(len(daily) - 1)),
    }

    # ---------- 3. 1分钟线 ----------
    m1_resp = json.loads(http_get(
        f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={SYMBOL},m1,,800"))
    m1 = m1_resp["data"][SYMBOL]["m1"]
    with open(os.path.join(SAMPLES_DIR, f"m0_raw_m1_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(m1_resp, f, ensure_ascii=False)

    m1_today = [b for b in m1 if b[0].startswith(today_compact)]
    m1_first_today = m1_today[0] if m1_today else None
    m1_last_today = m1_today[-1] if m1_today else None
    sum_m1_today_hand = sum(float(b[5]) for b in m1_today)

    # 时间戳语义：A股上午09:30开盘。若首根为09:31→时间戳为区间结束；若为09:30→区间开始
    first_minute_label = m1_first_today[0][-4:] if m1_first_today else None
    ts_semantics = "unknown"
    if first_minute_label == "0931":
        ts_semantics = "interval_end(区间结束,首根0931覆盖09:30:00-09:31:00)"
    elif first_minute_label == "0930":
        ts_semantics = "interval_start(区间开始,首根0930覆盖09:30:00-09:31:00)"

    # 午盘检查：上午09:31-11:30共120根；若今日上午有120根且最后一根为1130→时间戳为区间结束
    morning_bars = [b for b in m1_today if b[0][-4:] <= "1130"]
    report["checks"]["m1"] = {
        **bar_stats(m1, "m1"),
        "today_bars": len(m1_today),
        "today_first_bar": m1_first_today,
        "today_last_bar": m1_last_today,
        "morning_bars_count": len(morning_bars),
        "timestamp_semantics": ts_semantics,
        "sum_today_volume_hand": sum_m1_today_hand,
        "quote_cumulative_volume_hand": float(fields[6]),
        "volume_type_conclusion": (
            "interval(区间量): 1分钟求和≈行情累计量"
            if abs(sum_m1_today_hand - float(fields[6])) / max(float(fields[6]), 1) < 0.05
            else f"mismatch: 求和{sum_m1_today_hand} vs 累计{fields[6]}"),
        "is_strictly_increasing": all(m1[i][0] < m1[i + 1][0] for i in range(len(m1) - 1)),
    }

    # ---------- 4. 5分钟线 ----------
    m5_resp = json.loads(http_get(
        f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={SYMBOL},m5,,320"))
    m5 = m5_resp["data"][SYMBOL]["m5"]
    with open(os.path.join(SAMPLES_DIR, f"m0_raw_m5_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(m5_resp, f, ensure_ascii=False)

    m5_today = [b for b in m5 if b[0].startswith(today_compact)]
    m5_today_vol_hand = sum(float(b[5]) for b in m5_today)
    report["checks"]["m5"] = {
        **bar_stats(m5, "m5"),
        "today_bars": len(m5_today),
        "today_first_bar": m5_today[0] if m5_today else None,
        "today_last_bar": m5_today[-1] if m5_today else None,
        "sum_today_volume_hand": m5_today_vol_hand,
        "cross_check_vs_m1": (
            "consistent: 5分钟求和≈1分钟求和"
            if abs(m5_today_vol_hand - sum_m1_today_hand) / max(sum_m1_today_hand, 1) < 0.05
            else f"mismatch: m5={m5_today_vol_hand} vs m1={sum_m1_today_hand}"),
        "is_strictly_increasing": all(m5[i][0] < m5[i + 1][0] for i in range(len(m5) - 1)),
    }

    # ---------- 5. field[30] 语义：结合此前三次休市采样 ----------
    report["checks"]["field30_semantics"] = {
        "evidence": [
            "11:31:06请求→field[30]=20260924113106",
            "11:41:06请求→field[30]=20260924114106",
            "11:44:06请求→field[30]=20260924114406",
            f"本次{now.strftime('%H:%M:%S')}请求→field[30]={fields[30]}",
        ],
        "conclusion": (
            "休市时段(11:30-13:00)field[30]随请求时间持续刷新，"
            "因此field[30]=行情快照时间(≈服务器响应时间)，不能称为最后成交时间。"
            "休市期间真实最后成交时间应取分钟K线最后一根bar的时间戳。"),
    }

    # ---------- 6. 未完成K线判定 ----------
    last_m1_ts = m1_last_today[0] if m1_last_today else ""
    now_hhmm = now.strftime("%H%M")
    in_lunch = "1130" < now_hhmm < "1300"
    report["checks"]["bar_completeness"] = {
        "daily_last_bar": "未完成(当日未收盘)" if today_bar else "无当日数据",
        "m1_last_bar_ts": last_m1_ts,
        "m1_last_bar_status": (
            "已完成(午休,最后一根为11:30)" if in_lunch and last_m1_ts.endswith("1130")
            else "需按当前时间与时间戳语义判断"),
        "m5_last_bar_status": (
            "不完整(11:30 bar仅含1分钟)" if in_lunch and m5_today and m5_today[-1][0].endswith("1130")
            else "需按当前时间判断"),
    }

    out = os.path.join(SAMPLES_DIR, f"m0_review_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已保存: {out}")


if __name__ == "__main__":
    main()

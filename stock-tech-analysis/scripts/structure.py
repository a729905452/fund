#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""位置与风险空间分析（M2）。

职责：
- 识别摆动高低点（带确认窗口，确认时点之前不可用，防未来数据泄漏）
- 生成候选支撑/压力区域，保存上下界、周期、形成/确认时间、来源bar、算法版本
- 合并重叠区域，保留距离现价相关且证据较充分者
- 计算当前区域分类、上方压力距离、合理失败位置距离、空间比

设计约束（来自开发计划）：
- 摆动点需要后续K线确认，只能在确认时点之后使用
- 空间比仅在压力和失败位置均有效时计算；拒绝零距离/负距离/方向错误
- 无法可靠定价时给出缺口，不编造区域
"""
from datetime import datetime

ALGO_VERSION = "swing-v1.0"

# 默认结构参数（初始占位，待 M5 样例校准；不冒充 V3.4 原文规则）
DEFAULT_PARAMS = {
    "swing_window": 3,          # 摆动点确认窗口：左右各3根
    "merge_tolerance_pct": 1.5,  # 区域合并容差（%）：价格间距小于此值视为重叠
    "max_regions_per_side": 5,   # 每侧（支撑/压力）最多保留区域数
    "relevance_pct": 15.0,       # 相关性范围：仅保留距现价 ±15% 内的区域
    "failure_buffer_pct": 1.0,   # 失败位置缓冲（%）：支撑下沿下浮，与确认失守线同口径
}


def find_swing_points(bars, window=None, algo_version=ALGO_VERSION):
    """识别摆动高低点。

    摆动高点：bar[i].high 为 [i-window, i+window] 区间内最高
    摆动低点：bar[i].low  为 [i-window, i+window] 区间内最低
    确认时点：i+window 那根 bar 的时间（此前不可用，防未来数据）

    返回 {"highs": [...], "lows": [...]}，每个点含：
    price, formed_ts(形成bar时间), confirmed_ts(确认bar时间), source_index, algo_version
    """
    if window is None:
        window = DEFAULT_PARAMS["swing_window"]
    highs = []
    lows = []
    n = len(bars)
    for i in range(window, n - window):
        seg = bars[i - window: i + window + 1]
        # 未完成 bar 不参与确认（其价格仍在变化）
        if any(not b["complete"] for b in seg):
            continue
        center = bars[i]
        if all(center["high"] >= b["high"] for b in seg) and \
           any(center["high"] > b["high"] for j, b in enumerate(seg) if j != window):
            highs.append({
                "price": center["high"],
                "formed_ts": center["ts"],
                "confirmed_ts": bars[i + window]["ts"],
                "source_index": i,
                "algo_version": algo_version,
            })
        if all(center["low"] <= b["low"] for b in seg) and \
           any(center["low"] < b["low"] for j, b in enumerate(seg) if j != window):
            lows.append({
                "price": center["low"],
                "formed_ts": center["ts"],
                "confirmed_ts": bars[i + window]["ts"],
                "source_index": i,
                "algo_version": algo_version,
            })
    return {"highs": highs, "lows": lows}


def points_to_regions(points, kind, period, tolerance_pct=None):
    """将摆动点聚合为区域（相近价格的点合并为一个区域）。

    kind: "support" | "resistance"
    返回区域列表，每个区域含：
    lower/upper(价格上下界), points_count, first_formed_ts, last_confirmed_ts,
    period, kind, algo_version
    """
    if tolerance_pct is None:
        tolerance_pct = DEFAULT_PARAMS["merge_tolerance_pct"]
    if not points:
        return []
    sorted_pts = sorted(points, key=lambda p: p["price"])
    regions = []
    cur = [sorted_pts[0]]
    for p in sorted_pts[1:]:
        base = cur[-1]["price"]
        if base > 0 and (p["price"] - base) / base * 100 <= tolerance_pct:
            cur.append(p)
        else:
            regions.append(_build_region(cur, kind, period))
            cur = [p]
    regions.append(_build_region(cur, kind, period))
    return regions


def _build_region(points, kind, period):
    prices = [p["price"] for p in points]
    return {
        "kind": kind,
        "period": period,
        "lower": round(min(prices), 3),
        "upper": round(max(prices), 3),
        "mid": round(sum(prices) / len(prices), 3),
        "points_count": len(points),
        "first_formed_ts": min(p["formed_ts"] for p in points),
        "last_confirmed_ts": max(p["confirmed_ts"] for p in points),
        "algo_version": ALGO_VERSION,
    }


def merge_regions(regions, tolerance_pct=None):
    """合并重叠区域（上下界相交或间距小于容差）。"""
    if tolerance_pct is None:
        tolerance_pct = DEFAULT_PARAMS["merge_tolerance_pct"]
    if len(regions) <= 1:
        return regions
    sorted_r = sorted(regions, key=lambda r: r["lower"])
    merged = [dict(sorted_r[0])]
    for r in sorted_r[1:]:
        last = merged[-1]
        gap_pct = (r["lower"] - last["upper"]) / last["upper"] * 100 if last["upper"] > 0 else 999
        if r["lower"] <= last["upper"] or gap_pct <= tolerance_pct:
            last["lower"] = min(last["lower"], r["lower"])
            last["upper"] = max(last["upper"], r["upper"])
            last["points_count"] += r["points_count"]
            last["first_formed_ts"] = min(last["first_formed_ts"], r["first_formed_ts"])
            last["last_confirmed_ts"] = max(last["last_confirmed_ts"], r["last_confirmed_ts"])
            last["mid"] = round((last["lower"] + last["upper"]) / 2, 3)
        else:
            merged.append(dict(r))
    return merged


def filter_relevant_regions(regions, current_price, relevance_pct=None, max_count=None):
    """保留距现价 ±relevance_pct% 内的区域，按证据强度(点数)与距离排序，截取 max_count。"""
    if relevance_pct is None:
        relevance_pct = DEFAULT_PARAMS["relevance_pct"]
    if max_count is None:
        max_count = DEFAULT_PARAMS["max_regions_per_side"]
    kept = []
    for r in regions:
        dist_pct = abs(r["mid"] - current_price) / current_price * 100 if current_price > 0 else 999
        if dist_pct <= relevance_pct:
            r = dict(r)
            r["distance_pct"] = round(dist_pct, 2)
            kept.append(r)
    # 证据强度优先，其次距离近优先
    kept.sort(key=lambda r: (-r["points_count"], r["distance_pct"]))
    return kept[:max_count]


def classify_position(current_price, supports, resistances):
    """分类当前价格所处位置（F01 修复）。

    同时返回两组关键位，避免现价穿过关键位后原计划被丢弃：
    - 位置描述用：nearest_support（现价下方最近支撑）、nearest_resistance（现价上方最近压力）
    - 计划跟踪用：broken_resistance（现价下方最近压力=已突破的压力，回踩跟踪对象）、
      broken_support（现价上方最近支撑=已跌破的支撑，失守跟踪对象）
    """
    inside_sup = [r for r in supports if r["lower"] <= current_price <= r["upper"]]
    inside_res = [r for r in resistances if r["lower"] <= current_price <= r["upper"]]

    below_sup = [r for r in supports if r["upper"] < current_price]
    above_res = [r for r in resistances if r["lower"] > current_price]
    # 现价下方的压力 = 已被突破的压力（突破后回踩的跟踪对象）
    below_res = [r for r in resistances if r["upper"] < current_price]
    # 现价上方的支撑 = 已被跌破的支撑（支撑失守的跟踪对象）
    above_sup = [r for r in supports if r["lower"] > current_price]

    nearest_support = max(below_sup, key=lambda r: r["upper"], default=None)
    nearest_resistance = min(above_res, key=lambda r: r["lower"], default=None)
    broken_resistance = max(below_res, key=lambda r: r["upper"], default=None)
    broken_support = min(above_sup, key=lambda r: r["lower"], default=None)

    if inside_res:
        zone = "inside_resistance"
        nearest_resistance = inside_res[0]
    elif inside_sup:
        zone = "inside_support"
        nearest_support = inside_sup[0]
    elif nearest_support is None and nearest_resistance is None:
        zone = "no_reference"
    elif nearest_support is None:
        zone = "below_all_support"
    elif nearest_resistance is None:
        zone = "above_all_resistance"
    else:
        zone = "between"

    def dist(region, use):
        if not region or current_price <= 0:
            return None
        edge = region[use]
        return round((edge - current_price) / current_price * 100, 2)

    return {
        "zone": zone,
        "current_price": current_price,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "broken_resistance": broken_resistance,
        "broken_support": broken_support,
        "support_distance_pct": dist(nearest_support, "upper"),
        "resistance_distance_pct": dist(nearest_resistance, "lower"),
    }


def compute_space_ratio(position, failure_buffer_pct=0.0):
    """计算空间比 = 上方压力距离 / 下方失败位置距离（F04 修复）。

    失败位置 = 防守支撑下沿 × (1 - failure_buffer_pct%)，与信号模块的
    确认失守线同口径，不再用支撑上沿充当失败价。
    区分理论价格距离（到支撑上沿）与执行风险距离（到失败位置）。

    仅在压力与失败位置均有效、方向正确、距离为正时计算；
    否则返回 None 并说明原因（不为了提高比值贴近失败线）。
    """
    sup = position.get("nearest_support")
    res = position.get("nearest_resistance")
    price = position.get("current_price", 0)
    if not sup or not res:
        return {"ratio": None, "reason": "压力或失败位置缺失，无法计算空间比"}
    if price <= 0:
        return {"ratio": None, "reason": "现价无效"}

    failure_price = round(sup["lower"] * (1 - failure_buffer_pct / 100), 3)
    up = res["lower"] - price
    down = price - failure_price
    theoretical_down = price - sup["upper"]

    if up <= 0:
        return {"ratio": None, "reason": f"压力方向错误(压力{res['lower']}<=现价{price})"}
    if down <= 0:
        return {"ratio": None,
                "reason": f"现价已处于失败位置{failure_price}之内，无安全空间"}
    if down / price < 0.001:
        return {"ratio": None, "reason": "失败位置距离过近(<0.1%)，拒绝计算"}

    return {
        "ratio": round(up / down, 2),
        "up_pct": round(up / price * 100, 2),
        "down_pct": round(down / price * 100, 2),
        "failure_price": failure_price,
        "failure_basis": f"支撑下沿{sup['lower']}下浮{failure_buffer_pct}%（与确认失守线同口径）",
        "theoretical_down_pct": (round(theoretical_down / price * 100, 2)
                                 if theoretical_down > 0 else 0.0),
        "theoretical_note": "到支撑上沿的理论距离，仅供对照，不作为失败位置",
        "reason": "ok",
    }


def analyze_structure(bars, current_price, period="day", params=None):
    """结构分析统一入口。

    bars: 标准 bar 列表（data_layer 输出）
    返回：区域列表、位置分类、空间比、可计算与不可计算项
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    swings = find_swing_points(bars, window=p["swing_window"])
    support_regions = points_to_regions(swings["lows"], "support", period, p["merge_tolerance_pct"])
    resistance_regions = points_to_regions(swings["highs"], "resistance", period, p["merge_tolerance_pct"])

    support_regions = merge_regions(support_regions, p["merge_tolerance_pct"])
    resistance_regions = merge_regions(resistance_regions, p["merge_tolerance_pct"])

    support_regions = filter_relevant_regions(support_regions, current_price,
                                              p["relevance_pct"], p["max_regions_per_side"])
    resistance_regions = filter_relevant_regions(resistance_regions, current_price,
                                                 p["relevance_pct"], p["max_regions_per_side"])

    position = classify_position(current_price, support_regions, resistance_regions)
    space = compute_space_ratio(position, p.get("failure_buffer_pct", 0.0))

    not_computable = []
    if not support_regions:
        not_computable.append("支撑区域：相关范围内无已确认摆动低点")
    if not resistance_regions:
        not_computable.append("压力区域：相关范围内无已确认摆动高点")
    if space["ratio"] is None:
        not_computable.append(f"空间比：{space['reason']}")

    return {
        "period": period,
        "current_price": current_price,
        "swing_points": {
            "highs_count": len(swings["highs"]),
            "lows_count": len(swings["lows"]),
            "window": p["swing_window"],
        },
        "supports": support_regions,
        "resistances": resistance_regions,
        "position": position,
        "space_ratio": space,
        "not_computable": not_computable,
        "params_used": p,
        "algo_version": ALGO_VERSION,
    }

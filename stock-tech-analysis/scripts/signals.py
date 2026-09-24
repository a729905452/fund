#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三类场景与条件判断（M3，审查修复版）。

场景：
1. 支撑附近回踩（support_retest）：进入观察区 → 卖压变化(m5) → 止跌迹象(m5)
2. 突破后回踩（breakout_pullback）：突破确认 → 回踩原突破位 → m5转强 → 最高接受价
3. 支撑失守与反抽失败（support_failure）：初步穿越 → 规则确认失守 →
   失守后的收回 → 失守后的反抽失败（严格按事件时间顺序）

修复要点（对应审查报告）：
- F01：突破/失守跟踪使用 broken_resistance/broken_support（原计划关键位），
  不随现价重选；位置描述与计划跟踪分离
- F02：分钟转强必要条件必须显式为 True；缺失或不足标记待确认/资料不足
- F03：收回与反抽只统计失守确认时点之后的K线，破位前的收盘不参与
- F06：确认层使用5分钟线；1分钟仅产生早期提示（early_hint），不替代确认
- 盈利保护需建仓时点才计算持仓以来峰值，否则仅输出历史回撤参考

买入状态机：
  data_insufficient / waiting / in_observation_zone / confirm_pending /
  tech_condition_met / above_max_accept_price / plan_invalid
卖出状态机（独立）：
  no_new_trigger / risk_watch / tech_reduce_triggered / exit_triggered
"""
import json
import os

RULES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "config", "rules.json")


def load_rules(path=None):
    with open(path or RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


BUY_STATES = [
    "data_insufficient",        # 资料不足
    "waiting",                  # 等待
    "in_observation_zone",      # 进入观察区
    "confirm_pending",          # 确认待完成
    "tech_condition_met",       # 技术条件满足
    "above_max_accept_price",   # 超过最高接受价
    "plan_invalid",             # 原计划失效
]
SELL_STATES = [
    "no_new_trigger",           # 无新增技术触发
    "risk_watch",               # 风险观察
    "tech_reduce_triggered",    # 技术减仓条件触发
    "exit_triggered",           # 退出条件触发
]


# ---------- 通用工具 ----------

def _completed(bars):
    """只取已完成K线参与确认。"""
    return [b for b in (bars or []) if b.get("complete")]


def _minute_strength(minute_bars, need=3):
    """分钟转强判定：最近 need 根已完成bar收盘抬升。

    返回 True/False/None（None=数据不足，调用方必须按未确认处理，F02）。
    """
    completed = _completed(minute_bars)
    if len(completed) < need:
        return None
    closes = [b["close"] for b in completed[-need:]]
    return closes[-1] > closes[0]


def _early_hint(m1_bars):
    """1分钟早期提示：只作提示，不能升级为5分钟确认（F06）。"""
    s = _minute_strength(m1_bars)
    if s is True:
        return {"level": "m1", "hint": "1分钟出现收盘抬升（早期提示，未经5分钟确认）"}
    return None


# ---------- 场景1：支撑附近回踩 ----------

def eval_support_retest(quote_price, position, m5_bars, rules, m1_bars=None):
    """支撑附近回踩场景（第三批补齐：确认区/最高接受价/失败条件）。

    必要条件链（任一不满足即停在该状态，不用其他条件抵消）：
    1. 存在有效支撑区域
    2. 现价进入观察区（支撑上沿 ± near_support_pct%）
    3. 卖压减弱（5分钟量收缩）
    4. 止跌迹象（5分钟连续不创新低 + 末根收≥开）
    附带输出：确认区、最高接受价、失败条件，供模型不借用其他场景数字。
    """
    cfg_obs = rules["observation_zone"]
    cfg_sp = rules["selling_pressure"]
    cfg_sf = rules["stop_fall"]
    cfg_fail = rules["support_failure"]

    evidence = []
    counter_evidence = []

    sup = position.get("nearest_support")
    if not sup:
        return {
            "scenario": "support_retest",
            "state": "data_insufficient",
            "state_reason": "相关范围内无已确认支撑区域，无法定义观察区",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "observation_zone": None, "confirm_zone": None,
            "max_accept_price": None, "failure_condition": None,
        }

    obs_low = round(sup["lower"] * (1 - cfg_obs["near_support_pct"] / 100), 3)
    obs_high = round(sup["upper"] * (1 + cfg_obs["near_support_pct"] / 100), 3)
    # 确认区：观察区内且重新站上支撑上沿（回踩后确认强度）
    confirm_zone = {"lower": sup["upper"], "upper": obs_high,
                    "basis": "站上支撑上沿但未离开观察区，回踩确认强度最高"}
    # 最高接受价：观察区上沿（超过则说明已远离支撑，风险收益恶化）
    max_accept = obs_high
    # 失败条件：与支撑失守场景同口径
    failure_line = round(sup["lower"] * (1 - cfg_fail["confirm_close_below_pct"] / 100), 3)
    failure_condition = (f"收盘跌破 {failure_line}（支撑下沿{sup['lower']}下浮"
                         f"{cfg_fail['confirm_close_below_pct']}%）连续"
                         f"{cfg_fail['confirm_bars']}根，原计划失效")

    observation_zone = {"lower": obs_low, "upper": obs_high,
                        "basis": f"支撑区域[{sup['lower']},{sup['upper']}] ± {cfg_obs['near_support_pct']}%",
                        "source_support": sup}
    evidence.append(f"观察区 [{obs_low}, {obs_high}]（依据：{observation_zone['basis']}）")
    evidence.append(f"最高接受价 {max_accept}；失败条件：{failure_condition}")

    if not (obs_low <= quote_price <= obs_high):
        dist = position.get("support_distance_pct")
        state = "above_max_accept_price" if quote_price > obs_high else "waiting"
        reason = (f"现价 {quote_price} 超过最高接受价 {max_accept}，远离支撑不追价"
                  if state == "above_max_accept_price"
                  else f"现价 {quote_price} 未进入观察区（距支撑上沿 {dist}%）")
        return {
            "scenario": "support_retest",
            "state": state, "state_reason": reason,
            "evidence": evidence, "counter_evidence": counter_evidence,
            "observation_zone": observation_zone, "confirm_zone": confirm_zone,
            "max_accept_price": max_accept, "failure_condition": failure_condition,
        }
    evidence.append(f"现价 {quote_price} 位于观察区内"
                    + ("且处于确认区（站上支撑上沿）" if quote_price >= sup["upper"] else ""))

    # 卖压减弱检查（5分钟确认层，F06）
    m5 = _completed(m5_bars)
    w = cfg_sp["volume_window"]
    if len(m5) < w * 2:
        return {
            "scenario": "support_retest",
            "state": "data_insufficient",
            "state_reason": f"已进入观察区，但5分钟已完成bar不足{w * 2}根，卖压条件无法核验",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "observation_zone": observation_zone, "confirm_zone": confirm_zone,
            "max_accept_price": max_accept, "failure_condition": failure_condition,
            "early_hint": _early_hint(m1_bars),
        }

    recent_avg = sum(b["volume"] for b in m5[-w:]) / w
    prior_avg = sum(b["volume"] for b in m5[-2 * w:-w]) / w
    volume_ok = prior_avg > 0 and recent_avg < prior_avg * cfg_sp["volume_shrink_ratio"]
    if volume_ok:
        evidence.append(f"卖压减弱(5分钟)：近{w}根均量{int(recent_avg)}股 "
                        f"< 前{w}根均量{int(prior_avg)}股 × {cfg_sp['volume_shrink_ratio']}")
    else:
        counter_evidence.append(
            f"卖压未明显减弱(5分钟)：近{w}根均量{int(recent_avg)}股，前{w}根均量{int(prior_avg)}股")

    # 止跌迹象检查（5分钟确认层）
    lows = [b["low"] for b in m5]
    n_stable = 0
    for i in range(len(lows) - 1, 0, -1):
        if lows[i] >= lows[i - 1]:
            n_stable += 1
        else:
            break
    last = m5[-1]
    body_ok = (last["close"] >= last["open"]) if cfg_sf["body_reclaim"] else True
    stop_fall_ok = n_stable >= cfg_sf["min_bars_stable"] and body_ok
    if stop_fall_ok:
        evidence.append(f"止跌迹象(5分钟)：连续{n_stable}根未创新低，末根收{last['close']}≥开{last['open']}")
    else:
        counter_evidence.append(
            f"止跌迹象不足(5分钟)：连续未创新低{n_stable}根（需≥{cfg_sf['min_bars_stable']}），"
            f"末根收≥开:{body_ok}")

    if volume_ok and stop_fall_ok:
        state, reason = "tech_condition_met", "观察区内5分钟卖压减弱且止跌，回踩必要条件均满足"
    else:
        state, reason = "confirm_pending", "已进入观察区，但5分钟卖压/止跌确认条件未全部满足，继续等待"

    return {
        "scenario": "support_retest",
        "state": state, "state_reason": reason,
        "evidence": evidence, "counter_evidence": counter_evidence,
        "observation_zone": observation_zone, "confirm_zone": confirm_zone,
        "max_accept_price": max_accept, "failure_condition": failure_condition,
        "early_hint": _early_hint(m1_bars),
    }


# ---------- 场景2：突破后回踩 ----------

def eval_breakout_pullback(quote_price, position, daily_bars, m5_bars, rules, m1_bars=None):
    """突破后回踩场景（F01/F02 修复）。

    跟踪对象：broken_resistance（原计划突破位，现价下方最近压力），
    而不是随现价重选的上方压力。
    必要条件链：
    1. 存在已突破的压力区域（broken_resistance）
    2. 突破已确认（已完成日线收盘 > 突破位 ×(1+confirm%)，连续 confirm_bars 根）
    3. 现价回踩突破位附近（≤ near_breakout_pct%）
    4. 现价 ≥ 突破位（跌破须先收复）
    5. 5分钟转强（rising 必须显式 True；None/False 均不通过）
    6. 未超过最高接受价
    """
    cfg_bo = rules["breakout"]
    cfg_pb = rules["pullback"]

    evidence = []
    counter_evidence = []

    res = position.get("broken_resistance")
    if not res:
        # 现价上方仍有压力，突破尚未发生
        above = position.get("nearest_resistance")
        reason = (f"现价下方无已突破压力；最近压力在上方 {above['lower']}，突破尚未发生"
                  if above else "相关范围内无压力区域，无法定义突破位")
        return {
            "scenario": "breakout_pullback",
            "state": "data_insufficient" if not above else "waiting",
            "state_reason": reason,
            "evidence": evidence, "counter_evidence": counter_evidence,
            "breakout_level": None, "max_accept_price": None,
        }

    breakout_level = res["upper"]
    max_accept = round(breakout_level * (1 + cfg_pb["max_accept_above_breakout_pct"] / 100), 3)
    threshold = breakout_level * (1 + cfg_bo["confirm_close_above_pct"] / 100)
    evidence.append(
        f"跟踪原突破位 {breakout_level}（已突破压力区域上沿，形成于{res.get('first_formed_ts')}），"
        f"确认阈值 {round(threshold, 3)}")

    completed = _completed(daily_bars)
    recent = completed[-cfg_bo["confirm_bars"]:] if len(completed) >= cfg_bo["confirm_bars"] else []
    breakout_confirmed = bool(recent) and all(b["close"] > threshold for b in recent)

    if not breakout_confirmed:
        last_close = completed[-1]["close"] if completed else None
        counter_evidence.append(f"突破未确认：最近收盘 {last_close} 未持续高于阈值 {round(threshold, 3)}")
        return {
            "scenario": "breakout_pullback",
            "state": "confirm_pending",
            "state_reason": "价格曾越过原压力但收盘确认未完成，不视为已确认突破",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "breakout_level": breakout_level, "max_accept_price": max_accept,
        }
    evidence.append(f"突破已确认：最近{len(recent)}根收盘均高于 {round(threshold, 3)}")

    if quote_price > max_accept:
        return {
            "scenario": "breakout_pullback",
            "state": "above_max_accept_price",
            "state_reason": f"现价 {quote_price} 已超过最高接受价 {max_accept}，按计划不追价",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "breakout_level": breakout_level, "max_accept_price": max_accept,
        }

    near_pct = abs(quote_price - breakout_level) / breakout_level * 100 if breakout_level > 0 else 999
    if near_pct > cfg_pb["near_breakout_pct"]:
        return {
            "scenario": "breakout_pullback",
            "state": "confirm_pending",
            "state_reason": f"突破已确认，但现价距原突破位 {round(near_pct, 2)}%"
                            f"（>{cfg_pb['near_breakout_pct']}%），尚未回踩",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "breakout_level": breakout_level, "max_accept_price": max_accept,
        }
    evidence.append(f"现价回踩原突破位附近（距 {breakout_level} 为 {round(near_pct, 2)}%）")

    # 5分钟转强（F02：必须显式 True；F06：确认层为5分钟）
    rising = _minute_strength(m5_bars)
    if rising is True:
        evidence.append("5分钟转强：最近3根已完成bar收盘抬升")
    elif rising is None:
        counter_evidence.append("5分钟已完成bar不足3根，转强条件无法核验")
    else:
        counter_evidence.append("5分钟未转强：最近3根已完成bar收盘未抬升")

    if quote_price < breakout_level:
        counter_evidence.append(
            f"现价 {quote_price} 低于原突破位 {breakout_level}，未收复前按假突破风险处理")
        return {
            "scenario": "breakout_pullback",
            "state": "confirm_pending",
            "state_reason": "现价跌破原突破位，需先收复才谈回踩；按假突破风险等待",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "breakout_level": breakout_level, "max_accept_price": max_accept,
            "early_hint": _early_hint(m1_bars),
        }

    if rising is not True:
        return {
            "scenario": "breakout_pullback",
            "state": "data_insufficient" if rising is None else "confirm_pending",
            "state_reason": ("回踩到位但5分钟数据不足，转强条件无法核验，标记资料不足"
                             if rising is None else
                             "回踩到位但5分钟尚未转强，必要条件未满足，继续等待"),
            "evidence": evidence, "counter_evidence": counter_evidence,
            "breakout_level": breakout_level, "max_accept_price": max_accept,
            "early_hint": _early_hint(m1_bars),
        }

    return {
        "scenario": "breakout_pullback",
        "state": "tech_condition_met",
        "state_reason": "突破已确认、现价回踩原突破位上方且5分钟转强，未超过最高接受价",
        "evidence": evidence, "counter_evidence": counter_evidence,
        "breakout_level": breakout_level, "max_accept_price": max_accept,
        "early_hint": _early_hint(m1_bars),
    }


# ---------- 场景3：支撑失守与反抽失败 ----------

def eval_support_failure(quote_price, position, daily_bars, rules):
    """支撑失守与反抽失败（F01/F03 修复）。

    跟踪对象：broken_support（现价上方最近支撑=已被跌破的原支撑）优先，
    其次 nearest_support（现价下方最近支撑）。
    严格按事件时间顺序：
    1. 找到失守确认事件及确认时点（收盘连续低于确认线）
    2. 收回只统计确认时点之后的K线（破位前的收盘不算收回）
    3. 反抽失败也必须有先失守、后反抽不过的时间证据
    """
    cfg = rules["support_failure"]
    evidence = []
    counter_evidence = []

    sup = position.get("broken_support") or position.get("nearest_support")
    if not sup:
        return {
            "scenario": "support_failure",
            "sell_state": "no_new_trigger",
            "state_reason": "无有效支撑区域可判断失守",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "failure_level": None,
        }

    failure_level = sup["lower"]
    initial_line = round(failure_level * (1 - cfg["initial_cross_pct"] / 100), 3)
    confirm_line = round(failure_level * (1 - cfg["confirm_close_below_pct"] / 100), 3)
    tracked_from = "已跌破的原支撑" if position.get("broken_support") else "现价下方最近支撑"
    evidence.append(
        f"跟踪{tracked_from}：防守位 {failure_level}（形成于{sup.get('first_formed_ts')}），"
        f"初步穿越线 {initial_line}，确认失守线 {confirm_line}")

    completed = _completed(daily_bars)

    # 失守事件必须按"当前正在发生的跌破段"判定（F03）。
    # 第一步：定位最近一次收盘跌破确认线的位置；全历史搜会让
    # 早已收回的旧 episode 误报为当前失守。
    last_below_idx = None
    for i in range(len(completed) - 1, -1, -1):
        if completed[i]["close"] < confirm_line:
            last_below_idx = i
            break

    if last_below_idx is None:
        # 从未（在本数据窗口内）收盘跌破确认线
        if quote_price < initial_line:
            return {
                "scenario": "support_failure",
                "sell_state": "risk_watch",
                "state_reason": f"现价 {quote_price} 初步穿越防守位（<{initial_line}），"
                                "收盘确认未完成，属风险观察而非确认破位",
                "evidence": evidence, "counter_evidence": counter_evidence,
                "failure_level": failure_level,
            }
        return {
            "scenario": "support_failure",
            "sell_state": "no_new_trigger",
            "state_reason": "现价在防守支撑之上，无新增技术触发",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "failure_level": failure_level,
        }

    # 第二步：向前找出本段连续跌破的起点，得到连续跌破根数
    run_start = last_below_idx
    while run_start > 0 and completed[run_start - 1]["close"] < confirm_line:
        run_start -= 1
    run_len = last_below_idx - run_start + 1
    confirmed_failure = run_len >= cfg["confirm_bars"]
    confirm_ts = completed[last_below_idx]["ts"]

    after = completed[last_below_idx + 1:]  # 最近跌破之后的K线

    if not confirmed_failure:
        # 盘中或单根轻触，未达确认条件
        if quote_price < initial_line or not after:
            return {
                "scenario": "support_failure",
                "sell_state": "risk_watch",
                "state_reason": (f"收盘初步跌破确认线但未达连续{cfg['confirm_bars']}根，"
                                 "属风险观察而非确认破位"),
                "evidence": evidence, "counter_evidence": counter_evidence,
                "failure_level": failure_level,
            }
        # 轻触后已回到线上：无新增触发
        return {
            "scenario": "support_failure",
            "sell_state": "no_new_trigger",
            "state_reason": "曾轻触确认线但未确认失守，现价已回到防守位之上",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "failure_level": failure_level,
        }

    evidence.append(f"失守确认于 {confirm_ts}（连续{run_len}根收盘低于 {confirm_line}）")

    # 第三步：收回只统计最近跌破之后的K线（F03：破位前的收盘不算收回）
    reclaim_idx = None
    for j, b in enumerate(after):
        if b["close"] >= failure_level:
            reclaim_idx = j
            break

    if reclaim_idx is not None:
        reclaim_ts = after[reclaim_idx]["ts"]
        bars_since_reclaim = len(after) - 1 - reclaim_idx
        if bars_since_reclaim <= 1:
            counter_evidence.append(f"失守后于 {reclaim_ts} 收盘收回至支撑区域内")
            return {
                "scenario": "support_failure",
                "sell_state": "risk_watch",
                "state_reason": "失守后收回，原退出条件撤销，转入风险观察",
                "evidence": evidence, "counter_evidence": counter_evidence,
                "failure_level": failure_level,
                "failure_confirmed_ts": confirm_ts,
            }
        # 收回已过去多根且价格维持在线上：旧 episode 已了结，不是新增风险
        counter_evidence.append(
            f"失守已于 {reclaim_ts} 收回（距今{bars_since_reclaim}根），属历史事件非新增风险")
        return {
            "scenario": "support_failure",
            "sell_state": "no_new_trigger",
            "state_reason": f"历史失守已于 {reclaim_ts} 收回，当前无新增技术触发",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "failure_level": failure_level,
            "failure_confirmed_ts": confirm_ts,
        }

    # 第四步：反抽失败 = 确认失守后反弹高点仍低于防守位
    bounce_high = max((b["high"] for b in after), default=0)
    bounce_failed = bool(after) and bounce_high < failure_level
    if bounce_failed:
        evidence.append(f"反抽失败：失守后反弹最高 {bounce_high} 未回到防守位 {failure_level}")
        return {
            "scenario": "support_failure",
            "sell_state": "exit_triggered",
            "state_reason": "支撑规则确认失守且反抽失败，退出条件触发",
            "evidence": evidence, "counter_evidence": counter_evidence,
            "failure_level": failure_level,
            "failure_confirmed_ts": confirm_ts,
        }
    return {
        "scenario": "support_failure",
        "sell_state": "tech_reduce_triggered",
        "state_reason": "支撑规则确认失守（收盘连续低于确认线），技术减仓条件触发",
        "evidence": evidence, "counter_evidence": counter_evidence,
        "failure_level": failure_level,
        "failure_confirmed_ts": confirm_ts,
    }


# ---------- 汇总 ----------

def evaluate_signals(quote, position, daily_bars, m5_bars, m1_bars=None,
                     rules=None, holding=None):
    """信号判断统一入口（F06/F12 修复）。

    m5_bars: 5分钟确认层；m1_bars: 仅早期提示。
    holding: {"cost": 成本, "entry_date": "YYYY-MM-DD"}；无 entry_date 时
    只输出历史回撤参考，不归为持仓盈利保护（F12）。
    """
    if rules is None:
        rules = load_rules()

    price = quote["price"]
    retest = eval_support_retest(price, position, m5_bars, rules, m1_bars)
    pullback = eval_breakout_pullback(price, position, daily_bars, m5_bars, rules, m1_bars)
    failure = eval_support_failure(price, position, daily_bars, rules)

    # 盈利保护（F12：需建仓时点，否则仅历史回撤参考）
    profit_protect = None
    completed_daily = _completed(daily_bars)
    if holding and holding.get("cost"):
        entry_date = holding.get("entry_date")
        if entry_date:
            since_entry = [b for b in completed_daily if b["ts"] >= entry_date]
            peak = max((b["high"] for b in since_entry), default=price)
            dd_pct = (peak - price) / peak * 100 if peak > 0 else 0
            threshold = rules["profit_protect"]["drawdown_from_peak_pct"]
            triggered = dd_pct >= threshold
            profit_protect = {
                "applicable": True,
                "basis": f"持仓以来（自{entry_date}）最高点回撤",
                "peak": peak, "drawdown_pct": round(dd_pct, 2),
                "threshold_pct": threshold,
                "state": "risk_watch" if triggered else "no_new_trigger",
                "note": ("自持仓以来高点回撤超阈值，进入盈利保护观察" if triggered
                         else "回撤在阈值内"),
            }
        else:
            peak = max((b["high"] for b in completed_daily), default=price)
            dd_pct = (peak - price) / peak * 100 if peak > 0 else 0
            profit_protect = {
                "applicable": False,
                "basis": "历史窗口回撤参考（缺建仓时点，不能归为持仓盈利保护）",
                "peak": peak, "drawdown_pct": round(dd_pct, 2),
                "state": "no_new_trigger",
                "note": "缺 entry_date，仅作历史回撤参考，不参与卖出状态",
            }

    # 综合买入状态
    buy_priority = ["tech_condition_met", "above_max_accept_price", "confirm_pending",
                    "in_observation_zone", "waiting", "plan_invalid", "data_insufficient"]
    candidates = [retest, pullback]
    best = min(candidates, key=lambda r: buy_priority.index(r["state"]))
    overall_buy = {
        "state": best["state"],
        "from_scenario": best["scenario"],
        "state_reason": best["state_reason"],
        "all_scenarios": {r["scenario"]: r["state"] for r in candidates},
    }

    # 综合卖出状态（取最严重；盈利保护仅在 applicable 时参与）
    sell_severity = ["no_new_trigger", "risk_watch", "tech_reduce_triggered", "exit_triggered"]
    sell_states = [failure["sell_state"]]
    if profit_protect and profit_protect["applicable"]:
        sell_states.append(profit_protect["state"])
    worst = max(sell_states, key=lambda s: sell_severity.index(s))
    overall_sell = {
        "state": worst,
        "state_reason": failure["state_reason"] if failure["sell_state"] == worst
                        else (profit_protect["note"] if profit_protect else ""),
        "sources": sell_states,
    }

    return {
        "buy": overall_buy,
        "sell": overall_sell,
        "scenarios": {
            "support_retest": retest,
            "breakout_pullback": pullback,
            "support_failure": failure,
        },
        "profit_protect": profit_protect,
        "rules_version": rules.get("version"),
        "rules_note": rules.get("_comment"),
    }

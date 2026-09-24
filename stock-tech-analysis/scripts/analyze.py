#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技术分析统一入口（M4，审查修复版）。

固定流程：取数 → 检查 → 计算 → 核验 → 保存。
输出结构化 JSON 到 stdout，完整记录保存到 data/ 目录。

用法：
  python analyze.py <股票代码> [--market sh|sz] [--cost 成本价] [--entry-date 建仓日期]

示例：
  python analyze.py 000636
  python analyze.py 000636 --cost 55.0 --entry-date 2026-09-20

退出码：0=成功（含降级结果），1=取数或输入失败，2=参数错误。

修复要点（对应审查报告）：
- F05：按交易时段分源检查新鲜度；过时数据只产生明确标注的历史分析
- F06：5分钟参与信号确认，1分钟仅早期提示
- F08：复权口径回退或对齐超阈值时降级，约束依赖计算
- F14：保存/加载跨次计划状态与规则快照，区分重复查询与新增升级
- C01：参数校验异常提示使用中文，保留非零退出码
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tencent_client as tc
import data_layer as dl
import structure as st
import signals as sg

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SKILL_DIR, "data")
PREFS_PATH = os.path.join(SKILL_DIR, "config", "user_preferences.json")
RULES_PATH = os.path.join(SKILL_DIR, "config", "rules.json")
PLAN_STATE_PATH = os.path.join(DATA_DIR, "plan_state.json")


# ---------- C01：中文参数校验 ----------

class ChineseArgumentParser(argparse.ArgumentParser):
    """覆盖错误输出为中文，保留退出码2。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误: {self._chinese_error(message)}\n")

    @staticmethod
    def _chinese_error(message):
        mapping = [
            ("the following arguments are required", "缺少必需参数"),
            ("invalid choice", "非法取值"),
            ("invalid float value", "数字格式错误"),
            ("unrecognized arguments", "无法识别的参数"),
            ("expected one argument", "该选项需要一个参数值"),
        ]
        for en, zh in mapping:
            if en in message:
                return f"{zh}（{message}）"
        return message


def _chinese_float(text):
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"成本价须为数字，收到: {text}")


def _chinese_date(text):
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"建仓日期须为 YYYY-MM-DD 格式，收到: {text}")


# ---------- F05：分源新鲜度检查 ----------

def _market_session(now):
    """判断当前所处交易时段。返回 session 标识。"""
    hhmm = now.strftime("%H%M")
    weekday = now.weekday()  # 0=周一
    if weekday >= 5:
        return "weekend"
    if hhmm < "0930":
        return "pre_open"
    if hhmm <= "1130":
        return "morning"
    if hhmm < "1300":
        return "lunch"
    if hhmm <= "1500":
        return "afternoon"
    return "post_close"


def check_freshness(quote, m1, m5, daily, fetch_time):
    """按交易时段和数据周期分别检查各数据源新鲜度（F05）。

    允许正常午休：午休期间上午数据仍属"当日有效"。
    拒绝：下午/盘后仍沿用上午或更早数据确认"当前"信号。
    返回 (freshness_report, is_stale_for_current)。
    """
    session = _market_session(fetch_time)
    report = {"session": session, "sources": {}}

    # 报价快照新鲜度（快照时间≈服务器响应时间，应与请求时刻接近）
    try:
        snap = datetime.strptime(quote["snapshot_time"], "%Y%m%d%H%M%S")
        quote_lag_min = (fetch_time - snap).total_seconds() / 60
    except (ValueError, TypeError):
        quote_lag_min = None
    quote_stale = quote_lag_min is None or quote_lag_min > 5
    report["sources"]["quote"] = {
        "snapshot_time": quote.get("snapshot_time"),
        "lag_minutes": round(quote_lag_min, 1) if quote_lag_min is not None else None,
        "stale": quote_stale,
        "note": "快照时间与请求时刻相差超5分钟" if quote_stale else "快照新鲜",
    }

    # 分钟线新鲜度：按时段给出"应有的最后时间戳"
    def minute_stale(dataset, name):
        if dataset.get("status") != "success" or not dataset.get("bars"):
            return {"stale": True, "note": f"{name}数据缺失"}
        last_ts = dataset["last_ts"]
        try:
            last_dt = datetime.strptime(last_ts, "%Y%m%d%H%M")
        except ValueError:
            return {"stale": True, "note": f"{name}时间戳格式异常: {last_ts}"}
        lag_min = (fetch_time - last_dt).total_seconds() / 60
        today = fetch_time.strftime("%Y%m%d")
        is_today = last_ts.startswith(today)
        # 午休允许最后时间戳停留在11:30；午后/盘中要求接近当前
        if session in ("morning", "afternoon"):
            stale = (not is_today) or lag_min > 10
            note = (f"{name}最后bar {last_ts}，盘中滞后{int(lag_min)}分钟"
                    if stale else f"{name}盘中新鲜")
        elif session == "lunch":
            stale = (not is_today)
            note = (f"{name}非当日数据" if stale else f"{name}午休期间使用上午数据，属正常")
        else:  # pre_open/post_close/weekend
            stale = False
            note = f"{name}非交易时段，使用最近交易日数据（{last_ts}）"
        return {"stale": stale, "last_ts": last_ts,
                "lag_minutes": round(lag_min, 1), "note": note}

    report["sources"]["m1"] = minute_stale(m1, "1分钟线")
    report["sources"]["m5"] = minute_stale(m5, "5分钟线")

    # 日线新鲜度：交易日盘中/盘后应有当日bar（未完成亦可）
    if daily.get("status") == "success" and daily.get("bars"):
        today_dash = fetch_time.strftime("%Y-%m-%d")
        has_today = daily["last_ts"] == today_dash
        if session in ("morning", "lunch", "afternoon", "post_close") and not has_today:
            report["sources"]["daily"] = {
                "stale": True, "last_ts": daily["last_ts"],
                "note": f"交易日无当日日线bar（最后{daily['last_ts']}），数据可能未更新"}
        else:
            report["sources"]["daily"] = {
                "stale": False, "last_ts": daily["last_ts"], "note": "日线日期正常"}
    else:
        report["sources"]["daily"] = {"stale": True, "note": "日线数据缺失"}

    # 盘中场景：任一确认用数据源过时 → 不能宣称"当前已触发"
    is_stale_for_current = (
        session in ("morning", "afternoon")
        and (quote_stale or report["sources"]["m1"]["stale"]
             or report["sources"]["m5"]["stale"] or report["sources"]["daily"]["stale"])
    )
    return report, is_stale_for_current


# ---------- F14：跨次计划状态 ----------

def load_plan_state():
    try:
        with open(PLAN_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_plan_state(code, state):
    os.makedirs(os.path.dirname(PLAN_STATE_PATH), exist_ok=True)
    all_states = load_plan_state()
    all_states[code] = state
    with open(PLAN_STATE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(all_states, f, ensure_ascii=False, indent=2)


def build_plan_state(code, quote, structure_result, signal_result, rules, fetch_time):
    """构建本次计划状态快照（F14）。"""
    pos = structure_result.get("position", {})
    sup = pos.get("nearest_support")
    res = pos.get("broken_resistance") or pos.get("nearest_resistance")
    failure = signal_result["scenarios"].get("support_failure", {})
    return {
        "code": code,
        "updated_at": fetch_time.isoformat(timespec="seconds"),
        "price": quote["price"],
        "key_levels": {
            "support": [sup["lower"], sup["upper"]] if sup else None,
            "resistance": [res["lower"], res["upper"]] if res else None,
            "breakout_level": signal_result["scenarios"]
                              .get("breakout_pullback", {}).get("breakout_level"),
            "failure_level": failure.get("failure_level"),
        },
        "event_state": {
            "buy_state": signal_result["buy"]["state"],
            "sell_state": signal_result["sell"]["state"],
            "failure_confirmed_ts": failure.get("failure_confirmed_ts"),
        },
        "rules_snapshot": rules,          # 完整规则内容，非仅版本号
        "rules_version": rules.get("version"),
        "algo_version": structure_result.get("algo_version"),
    }


def diff_plan_state(prev, current):
    """对比上次与本次计划状态（F14）。

    区分：重复查询同一已触发事实 vs 新增风险升级 vs 关键位变化。
    """
    if not prev:
        return {"first_record": True, "note": "首次记录，无历史状态可对比"}
    changes = []
    repeats = []
    prev_evt = prev.get("event_state", {})
    cur_evt = current.get("event_state", {})

    if prev_evt.get("buy_state") == cur_evt.get("buy_state"):
        repeats.append(f"买入状态维持 {cur_evt.get('buy_state')}（非新增触发）")
    else:
        changes.append(f"买入状态: {prev_evt.get('buy_state')} → {cur_evt.get('buy_state')}")
    if prev_evt.get("sell_state") == cur_evt.get("sell_state"):
        repeats.append(f"卖出状态维持 {cur_evt.get('sell_state')}（非新增触发）")
    else:
        changes.append(f"卖出状态: {prev_evt.get('sell_state')} → {cur_evt.get('sell_state')}")

    prev_levels = prev.get("key_levels", {})
    cur_levels = current.get("key_levels", {})
    for key in ("support", "resistance", "breakout_level", "failure_level"):
        if prev_levels.get(key) != cur_levels.get(key):
            changes.append(f"关键位{key}: {prev_levels.get(key)} → {cur_levels.get(key)}")

    if prev.get("rules_version") != current.get("rules_version"):
        changes.append(f"规则版本: {prev.get('rules_version')} → {current.get('rules_version')}")
    elif prev.get("rules_snapshot") != current.get("rules_snapshot"):
        changes.append("规则内容已修改但版本号未变（同版本不同配置，旧结果不可直接重现）")

    return {
        "first_record": False,
        "prev_updated_at": prev.get("updated_at"),
        "changes": changes,
        "repeats": repeats,
        "note": "无变化" if not changes else f"{len(changes)}项变化",
    }


# ---------- 主流程 ----------

def load_prefs():
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def main():
    parser = ChineseArgumentParser(description="股票技术分析统一入口")
    parser.add_argument("code", help="6位A股代码，如 000636")
    parser.add_argument("--market", choices=["sh", "sz"], default=None,
                        help="市场（默认自动识别）")
    parser.add_argument("--cost", type=_chinese_float, default=None,
                        help="持仓成本价（可选，启用盈利保护）")
    parser.add_argument("--entry-date", type=_chinese_date, default=None,
                        help="建仓日期 YYYY-MM-DD（可选，盈利保护据此计算持仓以来峰值）")
    args = parser.parse_args()

    code = args.code.strip()
    if not (code.isdigit() and len(code) == 6):
        print(json.dumps({"status": "error",
                          "message": f"代码须为6位数字，收到: {code}。名称歧义时请用户提供代码。"},
                         ensure_ascii=False))
        sys.exit(1)

    market = args.market or tc.detect_market(code)
    if not market:
        print(json.dumps({"status": "error",
                          "message": f"无法识别代码 {code} 的市场。第一版仅支持 A 股（sh/sz）。"},
                         ensure_ascii=False))
        sys.exit(1)
    symbol = tc.build_symbol(code, market)

    prefs = load_prefs()
    fetch_cfg = prefs.get("data_fetch", {})
    daily_count = fetch_cfg.get("daily_count", 320)
    daily_adjust = fetch_cfg.get("daily_adjust", "qfq")
    m1_count = fetch_cfg.get("m1_count", 800)
    m5_count = fetch_cfg.get("m5_count", 320)

    holding = None
    cost = args.cost
    entry_date = args.entry_date
    if not cost:
        h = prefs.get("holdings", {})
        if isinstance(h, dict) and h.get("code") == code and h.get("cost"):
            cost = h["cost"]
            entry_date = entry_date or h.get("entry_date")
    if cost:
        holding = {"cost": cost, "entry_date": entry_date}

    fetch_time = datetime.now()
    stamp = fetch_time.strftime("%Y%m%d_%H%M%S")

    # ---------- 1. 取数 ----------
    fetch_errors = []
    raw_quote = daily_resp = m1_resp = m5_resp = None
    try:
        raw_quote = tc.fetch_quote_raw(symbol)
    except tc.FetchError as e:
        fetch_errors.append(f"行情: {e}")
    try:
        daily_resp = tc.fetch_daily_kline(symbol, count=daily_count, adjust=daily_adjust)
    except (tc.FetchError, json.JSONDecodeError) as e:
        fetch_errors.append(f"日线: {e}")
    try:
        m1_resp = tc.fetch_minute_kline(symbol, period=1, count=m1_count)
    except (tc.FetchError, json.JSONDecodeError) as e:
        fetch_errors.append(f"1分钟: {e}")
    try:
        m5_resp = tc.fetch_minute_kline(symbol, period=5, count=m5_count)
    except (tc.FetchError, json.JSONDecodeError) as e:
        fetch_errors.append(f"5分钟: {e}")

    # 保存原始响应（留痕）
    raw_dir = os.path.join(DATA_DIR, "raw", stamp)
    if raw_quote:
        tc.save_text(os.path.join(raw_dir, "quote.txt"), raw_quote)
    if daily_resp:
        tc.save_json(os.path.join(raw_dir, "daily.json"), daily_resp)
    if m1_resp:
        tc.save_json(os.path.join(raw_dir, "m1.json"), m1_resp)
    if m5_resp:
        tc.save_json(os.path.join(raw_dir, "m5.json"), m5_resp)

    if not raw_quote:
        print(json.dumps({"status": "error",
                          "message": "实时行情获取失败，无法分析。",
                          "fetch_errors": fetch_errors}, ensure_ascii=False))
        sys.exit(1)

    # ---------- 2. 解析与检查 ----------
    quote = dl.parse_quote(raw_quote, code, market, fetch_time)
    if quote.get("status") != "success":
        print(json.dumps({"status": "error", "message": quote.get("message")},
                         ensure_ascii=False))
        sys.exit(1)

    daily = dl.parse_daily(daily_resp, symbol, daily_adjust, fetch_time) if daily_resp \
        else {"status": "error", "bars": []}
    m1 = dl.parse_minute(m1_resp, symbol, 1, fetch_time) if m1_resp \
        else {"status": "error", "bars": []}
    m5 = dl.parse_minute(m5_resp, symbol, 5, fetch_time) if m5_resp \
        else {"status": "error", "bars": []}

    quality = {
        "daily": dl.check_bars_quality(daily) if daily["status"] == "success"
                 else {"usable": False, "issues": ["日线获取失败"]},
        "m1": dl.check_bars_quality(m1, period=1) if m1["status"] == "success"
              else {"usable": False, "issues": ["1分钟线获取失败"]},
        "m5": dl.check_bars_quality(m5, period=5) if m5["status"] == "success"
              else {"usable": False, "issues": ["5分钟线获取失败"]},
    }
    if m1["status"] == "success":
        quality["cross_m1_quote"] = dl.cross_check_quote_minute(quote, m1, fetch_time)
    if daily["status"] == "success":
        quality["daily_price_alignment"] = dl.check_quote_daily_price_alignment(
            quote, daily, fetch_time)

    # F05：分源新鲜度
    freshness, is_stale = check_freshness(quote, m1, m5, daily, fetch_time)

    # ---------- 3. 计算（按数据可用性降级） ----------
    degradations = []
    daily_ok = daily["status"] == "success" and quality["daily"]["usable"]
    m1_ok = m1["status"] == "success" and quality["m1"]["usable"]
    m5_ok = m5["status"] == "success" and quality["m5"]["usable"]

    # F08：复权口径回退或对齐超阈值 → 约束依赖计算
    align = quality.get("daily_price_alignment", {})
    if daily.get("adjust_mismatch"):
        daily_ok = False
        degradations.append(
            f"复权口径回退：请求{daily.get('requested_adjust')}实际{daily.get('adjust')}，"
            "暂停跨周期精确价格判断")
    elif align.get("action") == "recheck":
        degradations.append(f"价格口径待核实：{align.get('note')}")

    if not daily_ok:
        if not daily.get("adjust_mismatch"):
            degradations.append("日线不可用：不能可靠判断大周期区域，盘中结果须单独标明范围")
    if not m5_ok:
        degradations.append("5分钟线不可用：不判断盘中承接或触发，仅输出日线位置与条件计划")
    if not m1_ok:
        degradations.append("1分钟线不可用：无早期提示")

    # F05：盘中数据过时 → 只产生标注的历史分析
    stale_note = None
    if is_stale:
        stale_sources = [k for k, v in freshness["sources"].items() if v.get("stale")]
        stale_note = (f"数据过时（{freshness['session']}时段，过时源: {','.join(stale_sources)}）。"
                      "以下为数据截至时点的历史分析，不能宣称'当前已触发'。")
        degradations.append(stale_note)

    rules = sg.load_rules()

    if daily_ok:
        structure_result = st.analyze_structure(daily["bars"], quote["price"], period="day")
        signal_result = sg.evaluate_signals(
            quote, structure_result["position"], daily["bars"],
            m5["bars"] if m5_ok else None,
            m1["bars"] if m1_ok else None,
            rules=rules, holding=holding)
        # F05：盘中过时不宣称当前触发
        if is_stale and signal_result["buy"]["state"] == "tech_condition_met":
            signal_result["buy"]["state"] = "confirm_pending"
            signal_result["buy"]["state_reason"] = (
                "技术条件在数据截至时点成立，但数据已过时，不能宣称当前已触发，降级为待确认")
            signal_result["buy"]["stale_downgraded"] = True
    else:
        structure_result = {"period": "day", "current_price": quote["price"],
                            "supports": [], "resistances": [],
                            "position": {"zone": "unknown", "current_price": quote["price"]},
                            "space_ratio": {"ratio": None, "reason": "日线不可用或口径未对齐"},
                            "not_computable": ["全部结构分析（日线不可用或口径未对齐）"]}
        signal_result = {"buy": {"state": "data_insufficient",
                                 "state_reason": "日线数据不可用或复权口径未对齐，无法进行技术分析"},
                         "sell": {"state": "no_new_trigger", "state_reason": "无数据"},
                         "scenarios": {}, "profit_protect": None}

    # ---------- 4. F14：跨次计划状态 ----------
    current_plan = build_plan_state(code, quote, structure_result, signal_result,
                                    rules, fetch_time)
    prev_plan = load_plan_state().get(code)
    plan_diff = diff_plan_state(prev_plan, current_plan)
    save_plan_state(code, current_plan)

    # ---------- 5. 核验与汇总 ----------
    result = {
        "status": "success",
        "meta": {
            "code": code, "name": quote["name"], "market": market,
            "fetch_time": fetch_time.isoformat(timespec="seconds"),
            "snapshot_time": quote["snapshot_time"],
            "snapshot_time_note": "快照时间为服务器响应时间，非最后成交时间（M0 实证）",
            "session": freshness["session"],
            "is_stale_for_current": is_stale,
            "stale_note": stale_note,
            "degradations": degradations,
            "fetch_errors": fetch_errors,
        },
        "quote": quote,
        "quality": quality,
        "freshness": freshness,
        "structure": structure_result,
        "signals": signal_result,
        "plan_tracking": {
            "diff": plan_diff,
            "state_path": PLAN_STATE_PATH,
        },
        "provenance": {
            "rules_version": rules.get("version"),
            "algo_version": structure_result.get("algo_version"),
            "data_dir": raw_dir,
        },
    }

    # ---------- 6. 保存 ----------
    record_path = os.path.join(DATA_DIR, "analysis", f"{code}_{stamp}.json")
    tc.save_json(record_path, result)
    result["meta"]["record_path"] = record_path

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

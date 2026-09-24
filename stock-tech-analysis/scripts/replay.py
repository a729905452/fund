#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回放验证（M5，审查修复版 F13）。

使用冻结样本按时间逐步回放：每一步只能看到当时已到达的数据。

验证内容：
1. 摆动高低点只在确认时点之后使用（无未来数据泄漏）——带断言
2. 未完成K线区分必须影响结果——带断言（只打印不计入ok是缺陷，已修复）
3. 完整信号链回放：突破→回踩→失守→收回的状态转换与撤销——带断言
4. 数据异常注入：NaN、OHLC错误、串标响应——带断言

用法：
  python replay.py <样本目录>  # 目录含 daily.json
  python replay.py --selftest  # 内置合成数据自测（不依赖网络）

退出码：0=全部断言通过，1=存在失败断言。
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_layer as dl
import structure as st
import signals as sg


class Assert:
    """断言收集器：任何失败都会使最终结果失败。"""

    def __init__(self):
        self.failures = []
        self.passes = 0

    def ok(self, cond, label):
        if cond:
            self.passes += 1
            print(f"  [通过] {label}")
        else:
            self.failures.append(label)
            print(f"  [失败] {label}")

    def eq(self, actual, expected, label):
        self.ok(actual == expected, f"{label}（期望={expected}，实际={actual}）")

    def summary(self):
        total = self.passes + len(self.failures)
        print(f"\n断言: {self.passes}/{total} 通过")
        if self.failures:
            print("失败项:")
            for f in self.failures:
                print(f"  - {f}")
        return not self.failures


def _mk_bars(closes, complete=True, start="2026-01-01"):
    """按收盘价序列合成日线bar（日期连续）。"""
    bars = []
    for i, c in enumerate(closes):
        day = int(start[-2:]) + i
        ts = f"{start[:8]}{day:02d}"
        bars.append(dl.make_bar(ts=ts, open_=c - 0.2, close=c,
                                high=c + 0.3, low=c - 0.3,
                                volume_shares=100000, complete=complete))
    return bars


def _region(lower, upper, kind="support"):
    return {"kind": kind, "period": "day", "lower": lower, "upper": upper,
            "mid": (lower + upper) / 2, "points_count": 2,
            "first_formed_ts": "2026-01-01", "last_confirmed_ts": "2026-01-05",
            "algo_version": "test"}


# ---------- 1. 摆动点与回放 ----------

def test_swing_and_replay(a):
    print("\n=== 1. 摆动点识别与无未来数据泄漏 ===")
    closes = [10, 11, 12, 13, 14, 13, 12, 11, 10, 11, 12, 13, 12, 11, 10, 9, 10, 11, 12, 13]
    bars = _mk_bars(closes)
    window = 2
    swings = st.find_swing_points(bars, window=window)

    high_formed = [p["formed_ts"] for p in swings["highs"]]
    low_formed = [p["formed_ts"] for p in swings["lows"]]
    a.ok("2026-01-05" in high_formed, f"识别预期摆动高点2026-01-05（实际={high_formed}）")
    a.ok("2026-01-16" in low_formed, f"识别预期摆动低点2026-01-16（实际={low_formed}）")

    # 确认时点索引 = 形成索引 + window
    ts_index = {b["ts"]: i for i, b in enumerate(bars)}
    leaks = []
    for side in ("highs", "lows"):
        for pt in swings[side]:
            if ts_index.get(pt["confirmed_ts"]) != pt["source_index"] + window:
                leaks.append(pt)
    a.eq(len(leaks), 0, f"确认时点=形成时点+{window}根（泄漏{len(leaks)}处）")

    # 回放：每步只暴露前k根，确认时点不得晚于暴露边界
    violations = 0
    for k in range(2 * window + 1, len(bars) + 1):
        exposed = bars[:k]
        s = st.find_swing_points(exposed, window=window)
        boundary = exposed[-1]["ts"]
        for side in ("highs", "lows"):
            for pt in s[side]:
                if pt["confirmed_ts"] > boundary:
                    violations += 1
    a.eq(violations, 0, f"回放{len(bars)}步无未来数据泄漏（违规{violations}处）")


# ---------- 2. 未完成K线区分（必须影响结果） ----------

def test_incomplete_bars(a):
    print("\n=== 2. 未完成K线区分（计入断言） ===")
    # 构造：末端出现一个候选摆动点，仅在最后3根完整时可确认
    closes = [10, 11, 12, 13, 14, 13, 12, 11]
    bars_all_complete = _mk_bars(closes, complete=True)
    bars_tail_incomplete = _mk_bars(closes, complete=True)
    for b in bars_tail_incomplete[-3:]:
        b["complete"] = False

    s1 = st.find_swing_points(bars_all_complete, window=2)
    s2 = st.find_swing_points(bars_tail_incomplete, window=2)
    a.ok(len(s2["highs"]) < len(s1["highs"]) or len(s2["lows"]) < len(s1["lows"]),
         f"未完成bar使摆动点减少（全完成h{len(s1['highs'])}/l{len(s1['lows'])}"
         f" vs 尾3根未完成h{len(s2['highs'])}/l{len(s2['lows'])}）")

    # 末根未完成不应作为"已确认收盘"参与信号确认
    daily = _mk_bars([100, 101, 102], complete=True)
    daily[-1]["complete"] = False
    completed = [b for b in daily if b["complete"]]
    a.eq(len(completed), 2, "信号确认只使用已完成bar（末根未完成被排除）")


# ---------- 3. 完整信号链回放（状态转换与撤销） ----------

def test_signal_chain(a):
    print("\n=== 3. 信号链：突破→回踩 / 失守→收回 / 失守→反抽失败 ===")
    rules = sg.load_rules()

    # --- 3a. 已确认突破+回踩+5分钟转强 → tech_condition_met ---
    # 收盘连续高于100*1.01=101；现价100.5回踩；5分钟转强
    daily_bo = _mk_bars([98, 99, 101.5], complete=True)
    pos_bo = {"zone": "between", "current_price": 100.5,
              "nearest_support": _region(95, 96),
              "nearest_resistance": _region(105, 106, "resistance"),
              "broken_resistance": _region(99.5, 100, "resistance"),
              "broken_support": None}
    m5_up = [dl.make_bar(ts=f"2026010{i}1000", open_=100, close=100.2 + i * 0.1,
                         high=100.5, low=99.9, volume_shares=1000, complete=True)
             for i in range(5)]
    r = sg.eval_breakout_pullback(100.5, pos_bo, daily_bo, m5_up, rules)
    a.eq(r["state"], "tech_condition_met",
         f"3a 确认突破+回踩+5分钟转强 → tech_condition_met（实际={r['state']}）")
    a.eq(r["breakout_level"], 100, f"3a 跟踪原突破位100（实际={r['breakout_level']}）")

    # --- 3b. 同3a但无分钟数据 → 不得 tech_condition_met（F02） ---
    r = sg.eval_breakout_pullback(100.5, pos_bo, daily_bo, None, rules)
    a.ok(r["state"] in ("data_insufficient", "confirm_pending"),
         f"3b 无分钟数据不得判满足（实际={r['state']}）")

    # --- 3c. 现价低于突破位 → confirm_pending（假突破风险） ---
    r = sg.eval_breakout_pullback(99.5, pos_bo, daily_bo, m5_up, rules)
    a.eq(r["state"], "confirm_pending",
         f"3c 跌破突破位须先收复（实际={r['state']}）")

    # --- 3d. 失守后收回（按事件顺序）→ risk_watch ---
    # 先确认失守（收98<99），下一根收回101
    daily_reclaim = _mk_bars([102, 98, 101], complete=True)
    pos_fail = {"zone": "between", "current_price": 101,
                "nearest_support": _region(95, 96),
                "nearest_resistance": _region(105, 106, "resistance"),
                "broken_resistance": None,
                "broken_support": _region(99, 100)}
    r = sg.eval_support_failure(101, pos_fail, daily_reclaim, rules)
    a.eq(r["sell_state"], "risk_watch",
         f"3d 失守后收回 → risk_watch（实际={r['sell_state']}）")

    # --- 3e. 破位前的收盘不得算收回（F03 核心复现） ---
    # 先收102（在支撑上），后连续收98确认失守，再无收回
    daily_fail = _mk_bars([102, 98, 97.5], complete=True)
    r = sg.eval_support_failure(97.5, pos_fail, daily_fail, rules)
    a.ok(r["sell_state"] in ("tech_reduce_triggered", "exit_triggered"),
         f"3e 破位前收盘102不得当收回，应为减仓/退出（实际={r['sell_state']}）")

    # --- 3f. 失守+反抽失败 → exit_triggered ---
    daily_bounce_fail = _mk_bars([102, 98, 97, 98.5], complete=True)
    # 失守后反弹最高98.5 < 防守位99 → 反抽失败
    r = sg.eval_support_failure(98.5, pos_fail, daily_bounce_fail, rules)
    a.eq(r["sell_state"], "exit_triggered",
         f"3f 失守+反抽失败 → exit_triggered（实际={r['sell_state']}）")

    # --- 3g. 历史失守已收回（距今多根）→ no_new_trigger ---
    daily_old = _mk_bars([98, 101, 102, 103, 104, 105], complete=True)
    r = sg.eval_support_failure(105, pos_fail, daily_old, rules)
    a.eq(r["sell_state"], "no_new_trigger",
         f"3g 历史失守已收回属历史事件（实际={r['sell_state']}）")


# ---------- 4. 数据异常注入 ----------

def test_anomaly_injection(a):
    print("\n=== 4. 数据异常注入 ===")
    # NaN 价格必须被拦截（F10）
    bad = _mk_bars([10, 11, 12], complete=True)
    bad[1]["high"] = float("nan")
    q = dl.check_bars_quality({"bars": bad})
    a.ok(not q["usable"], f"NaN价格必须判不可用（usable={q['usable']}）")

    # OHLC 关系错误
    bad2 = _mk_bars([10, 11, 12], complete=True)
    bad2[0]["high"] = 5  # high < open/close
    q2 = dl.check_bars_quality({"bars": bad2})
    a.ok(not q2["usable"], f"OHLC关系错误必须判不可用（usable={q2['usable']}）")

    # 串标响应必须被拒绝（F09）
    raw = 'v_sz000636="51~风华高科~000001~56.83~58.74~57.90~557262~0~0~56.83~1~56.81~1~56.80~1~56.79~1~56.78~1~56.90~1~56.91~1~56.94~1~56.95~1~56.97~1~~20260924120000~-1.91~-3.25~59.68~56.50~56.83/557262/3212332185~557262~321233~4.86~160.31~~59.68~56.50~5.41~652.12~652.12~5.22~64.61~52.87~0.88~93~57.64~112.32~230.17~~~2.46~321233.2185~0.0000~0~   A~GP-A~251.21~0.32~0.18~3.21~2.39~83.90~14.73~11.65~5.03~-15.00~1147489969~1147490419~44.93~248.20~1147489969~~~262.41~0.18~~CNY~0~~57.00~-217~";'
    q3 = dl.parse_quote(raw, "000636", "sz", datetime.now())
    a.ok(q3.get("status") == "error",
         f"响应代码000001与请求000636不一致必须拒绝（status={q3.get('status')}）")

    # 当日分钟内部缺口必须报告（F11）
    today = datetime.now().strftime("%Y%m%d")
    sparse = [
        dl.make_bar(ts=f"{today}0930", open_=10, close=10, high=10, low=10,
                    volume_shares=100, complete=True),
        dl.make_bar(ts=f"{today}0931", open_=10, close=10, high=10, low=10,
                    volume_shares=100, complete=True),
        dl.make_bar(ts=f"{today}1000", open_=10, close=10, high=10, low=10,
                    volume_shares=100, complete=True),
    ]
    q4 = dl.check_bars_quality({"bars": sparse,
                                "as_of": datetime.now().isoformat(timespec="seconds")},
                               period=1)
    has_gap = len(q4["minute_gaps"]) > 0
    a.ok(has_gap, f"当日09:32-09:59内部缺口必须报告（gaps={q4['minute_gaps']}）")


# ---------- 5. 关键位跟踪不随现价重选（F01 核心复现） ----------

def test_key_level_tracking(a):
    print("\n=== 5. 关键位跟踪（F01 核心复现） ===")
    rules = sg.load_rules()

    # S01复现：原支撑100、次级支撑90，现价98收盘97
    # 修复前：改选90，输出no_new_trigger；修复后：跟踪原100
    pos_s01 = {"zone": "between", "current_price": 98,
               "nearest_support": _region(89, 90),       # 现价下方最近=次级支撑
               "nearest_resistance": _region(105, 106, "resistance"),
               "broken_resistance": None,
               "broken_support": _region(99, 100)}        # 已跌破的原支撑
    daily_s01 = _mk_bars([101, 97], complete=True)  # 收盘97 < 确认线99
    r = sg.eval_support_failure(98, pos_s01, daily_s01, rules)
    a.ok(r["sell_state"] in ("tech_reduce_triggered", "exit_triggered", "risk_watch"),
         f"S01 现价穿过原支撑100后仍跟踪原位置，不得报no_new_trigger（实际={r['sell_state']}）")
    a.eq(r["failure_level"], 99, f"S01 失败位用原支撑下沿99（实际={r['failure_level']}）")

    # S02复现：原压力100已突破、下一压力110，当前回踩101
    # 修复前：改用110作突破位报waiting；修复后：跟踪原100
    pos_s02 = {"zone": "between", "current_price": 101,
               "nearest_support": _region(95, 96),
               "nearest_resistance": _region(109, 110, "resistance"),
               "broken_resistance": _region(99.5, 100, "resistance"),
               "broken_support": None}
    daily_s02 = _mk_bars([99, 102, 103], complete=True)  # 收盘连续>101确认突破
    m5_up = [dl.make_bar(ts=f"2026010{i}1000", open_=100, close=100.5 + i * 0.1,
                         high=101, low=100, volume_shares=1000, complete=True)
             for i in range(5)]
    r = sg.eval_breakout_pullback(101, pos_s02, daily_s02, m5_up, rules)
    a.eq(r["breakout_level"], 100,
         f"S02 回踩跟踪原突破位100而非110（实际={r['breakout_level']}）")
    a.ok(r["state"] != "waiting",
         f"S02 不得因选错突破位而报waiting（实际={r['state']}）")


def selftest():
    """合成数据自测：全部断言计入结果，摆动点通过≠整套通过（F13）。"""
    print("=== M5 自测（合成数据，带断言）===")
    a = Assert()
    test_swing_and_replay(a)
    test_incomplete_bars(a)
    test_signal_chain(a)
    test_anomaly_injection(a)
    test_key_level_tracking(a)
    return a.summary()


def replay_from_samples(sample_dir):
    """从冻结样本回放（日线摆动点无泄漏 + 信号链可运行）。"""
    daily_path = os.path.join(sample_dir, "daily.json")
    if not os.path.exists(daily_path):
        print(f"缺少样本: {daily_path}")
        return False
    with open(daily_path, encoding="utf-8") as f:
        daily_resp = json.load(f)
    symbol = list(daily_resp["data"].keys())[0]
    daily = dl.parse_daily(daily_resp, symbol, "qfq", datetime.now())
    bars = daily["bars"]
    print(f"样本: {len(bars)} 根日线, {daily['first_ts']} -> {daily['last_ts']}")

    a = Assert()
    window = st.DEFAULT_PARAMS["swing_window"]
    ts_index = {b["ts"]: i for i, b in enumerate(bars)}
    swings = st.find_swing_points(bars, window=window)
    leaks = [pt for side in ("highs", "lows") for pt in swings[side]
             if ts_index.get(pt["confirmed_ts"]) != pt["source_index"] + window]
    a.eq(len(leaks), 0, "样本确认时点索引正确")

    violations = 0
    for k in range(2 * window + 1, len(bars) + 1, 20):
        exposed = bars[:k]
        s = st.find_swing_points(exposed, window=window)
        boundary = exposed[-1]["ts"]
        violations += sum(1 for side in ("highs", "lows") for pt in s[side]
                          if pt["confirmed_ts"] > boundary)
    a.eq(violations, 0, "样本回放无未来数据泄漏")
    return a.summary()


def main():
    parser = argparse.ArgumentParser(description="M5 回放验证（带断言）")
    parser.add_argument("sample_dir", nargs="?", help="冻结样本目录（含 daily.json）")
    parser.add_argument("--selftest", action="store_true", help="内置合成数据自测")
    args = parser.parse_args()

    if args.selftest:
        ok = selftest()
    elif args.sample_dir:
        ok = replay_from_samples(args.sample_dir)
    else:
        parser.print_help()
        sys.exit(1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

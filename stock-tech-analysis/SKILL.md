---
name: stock-tech-analysis
description: A股技术面分析技能。当用户需要分析某只A股的技术位置、趋势区域、支撑压力位、买入观察区/确认区、减仓或退出信号时使用。输入股票名称或代码，自动取数（实时行情+日线+1/5分钟K线）、校验数据质量、识别摆动高低点与关键区域、按三类场景（支撑回踩/突破回踩/支撑失守）判断买卖状态，输出可追溯的技术参考。仅技术面提示，不生成买卖指令，不预测涨跌概率。第一版仅支持普通A股（沪深），不支持ETF/港股/美股。
agent_created: true
---

# 股票技术分析 Skill

## 适用范围

- 仅普通 A 股（沪市 sh、深市 sz），6 位数字代码。**不把规则推广到 ETF、港股、美股**。
- 仅技术面提示：趋势区域、关键价位、条件状态。**不生成买卖指令、不推荐仓位、不把技术成熟度解释成上涨概率**。
- 主动查询模式：用户发起一次，完成一次分析并保存记录。

## 固定流程（不可跳过）

严格按"取数 → 检查 → 计算 → 解释 → 核验 → 保存"顺序执行：

1. **解析标的**：从用户输入提取 6 位代码。名称有歧义时请用户选择或提供代码，**不猜代码**。
2. **调用统一入口**：
   ```bash
   python {SKILL_DIR}/scripts/analyze.py <代码> [--market sh|sz] [--cost 成本价]
   ```
   脚本完成取数（实时行情+日线+1/5分钟）、质量检查、结构分析、信号判断，输出结构化 JSON 并保存记录。
3. **解释结果**：基于脚本输出的 `structure` 与 `signals` 字段组织回答。数值与状态以脚本为准，只补充简短解释与反证，**不覆盖脚本的原始结果**。
4. **核验一致性**：检查解释中的数值和状态与结构化结果一致；不一致时重写一次或直接使用脚本字段。
5. **保存**：脚本已自动保存到 `data/analysis/` 与 `data/raw/`，告知用户记录路径。

## 脚本输出解读

`analyze.py` 输出 JSON 的关键字段：

- `meta.degradations`：数据降级说明（日线/分钟线不可用时如何降级）
- `meta.stale_note`：数据过旧警告（非 None 时**必须**在回答开头声明，不能伪装成实时结果）
- `quote`：实时行情（price/volume=股/amount=元/snapshot_time=快照时间）
- `quality`：各数据集质量报告（usable/issues/warnings）
- `structure.supports / resistances`：关键区域（lower/upper/mid/points_count/形成与确认时间）
- `structure.position.zone`：位置分类（between/inside_support/inside_resistance/above_all/below_all/no_reference）
- `structure.space_ratio`：空间比（ratio 为 None 时说明原因，不编造）
- `signals.buy.state`：买入状态（见下）
- `signals.sell.state`：卖出状态（独立于买点评级）
- `signals.scenarios`：三类场景的证据（evidence）与反面证据（counter_evidence）

### 买入状态（buy.state）

| 状态 | 含义 |
|------|------|
| data_insufficient | 资料不足（无有效区域/数据缺失） |
| waiting | 等待（未到观察区/突破位） |
| in_observation_zone | 进入观察区 |
| confirm_pending | 确认待完成（必要条件未全部满足） |
| tech_condition_met | 技术条件满足（仍需人工决定） |
| above_max_accept_price | 超过最高接受价（不追价） |
| plan_invalid | 原计划失效 |

### 卖出状态（sell.state，独立判断）

| 状态 | 含义 |
|------|------|
| no_new_trigger | 无新增技术触发 |
| risk_watch | 风险观察（初步穿越/失守后收回/盈利保护观察） |
| tech_reduce_triggered | 技术减仓条件触发（规则确认失守） |
| exit_triggered | 退出条件触发（确认失守+反抽失败） |

## 回答组织要求

1. **先声明数据时点与降级**：snapshot_time、last_trade_ts、degradations、stale_note。
2. **区分日线背景与盘中状态**：日线判断背景，5分钟判断盘中结构，1分钟仅辅助较早提示（1分钟提示不自动升级为5分钟或日线确认）。
3. **每个价位注明依据**：区域上下界、形成/确认时间、点数（points_count）。
4. **状态+证据+反证**：同时展示 evidence 与 counter_evidence，不隐藏反面证据。
5. **确认条件未满足时明确说"等待"**，不用"中期仍看好"等表述覆盖。
6. **tech_condition_met 也要强调"技术参考，人工决定"**。

## 配置

- `config/rules.json`：规则阈值（V0.1 初始占位，待样例校准；**不冒充 V3.4 策略原文**）
- `config/user_preferences.json`：默认周期、数据量、可选持仓成本

## 降级原则

- 行情过时：显示数据截至时间，不宣称"当前已触发"
- 日线不足：不判断大周期区域，盘中结果单独标明范围
- 分钟线缺失：仅输出日线位置与条件计划，不判断盘中承接/触发
- 分钟成交额不明（字段[7]已实证不可用）：不计算依赖它的均价或金额指标
- 接口异常：明确报错或展示带时间的缓存状态，**不用虚构数据补齐**
- 模型与脚本冲突：展示冲突或退回脚本字段，不静默采纳更乐观/悲观结论

## 参考资料

- `references/FIELD_CONTRACT.md`：数据字段契约（M0 实证核定），接口字段语义的唯一依据

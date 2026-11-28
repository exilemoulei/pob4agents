# PoE2 0.5.5 Unverified / Unmeasured Mechanics Quarantine

本文件严格隔离**尚未取得 PoB2 `.lua` 源码、官方补丁说明或 `poe2db.tw` 确定性出处**的社区假设、未实装表项与估算值。
**铁律**：本文件所列条目**严禁**写入 `pob2_cli/src/pob2_cli/guardrails/` 或 `pob2_cli/src/pob2_cli/data/mechanics_facts.json` 知识卡。

| 条目 | 当前状态 | 隔离原因与缺失证据 | 工程处置 |
|---|---|---|---|
| 无 Boss 预兆裸点深渊亵渎词缀权重（社区常用 `2500`） | `empirical_estimate` | `poe2db.tw` 仅标 `1` 占位，缺乏官方或大样本可复现权重表 | 仅用于 `craft` 离线估算并强制标注 `weight_confidence="empirical_estimate"`，绝不冒充精确概率 |
| 深渊印记等级公式 `floor(ilvl * 0.4)` | `unverified` | 仅见社区覆盖层推测，无 PoB2 源码或 GGG 补丁实证 | 隔离不采用，不写入机制卡片 |
| 护符 / 引路石 / 药剂 / 珠宝 / 未实测武器常规词缀权重 | `unmeasured` | `modsview_weights.json.gz` 中 `has_measured_normal_weights=false` | `craft` 强制披露 `weight_confidence="unmeasured"`，禁止伪造精确赔率 |
| `Undying Hate` / `Heroic Tragedy` 永恒珠宝大点种子 LUT | `upstream_unimplemented` | `PathOfBuilding-PoE2` 上游未实装大点替换种子查找表 | 仅可评估基石替换与 `+3 Tribute`，严禁承诺或反查具体种子大点 |
| 护符（`Charm`）Boss 战常驻覆盖率百分比 | `unverified` | 依赖战斗节奏、充能获取与被击频率，无统一确定性公式 | 禁止硬编码覆盖率或将护符计入常驻抗性面板 |
| `Tame Beast` 野外随机金怪光环自动打分 | `unverified` | 无权威全量词缀组合权重数据库 | 拒绝自动打分 |
| `Tempest Bell` 敲钟频率、`Meta` 能量触发率、`Infernal Cry` 增伤循环 | `PoB2未模拟` | PoB2 引擎尚未完整模拟（上游 Issue `#2123`, `#181`, `#2166`, `#714`） | 一律标注 `[PoB2未模拟]` 红牌，严禁外部编造假 DPS |
| PoE2 `1.0` 正式版具体上线日期与未实装职业数值 | `unverified` | 未合入 `0.5.5` 客户端数据与 `TreeData/0_5/tree.lua` | 仅以 `TreeData/0_5/tree.lua` 实装的 8 职业 23 升华为准 |

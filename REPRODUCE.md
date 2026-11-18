# 断链修复 · 二验复现说明纸（watermark-bug-03）

## 一、缺陷现象（修复前）

1. 下游批次已落印（已盖印），但 `lineage_edges` 没有边状态字段，血缘边永远停在“待盖印”，追索走到上游缺口即断。
2. `stamp_batch` 盖印不回写边；`lineage_gate.allow_stamp` 恒为 `True`，边印与下游落印不同源。
3. `gaps.py` 中 `gap = expected - stamped + 2`，断链时缺口被人为加 2，缺口入库虚高。
4. 无修复能力、无流水、无排他锁，前端看不到血缘队列与修复结局。

## 二、修复后的领域规则

- 边三态：`待盖印 / 已盖印 / 已隔离`；断链判定 = 边“待盖印”且下游“已盖印”。
- **同源**：下游落印与边印在同一事务、用同一水印值写入（`stamp_batch`）；
  发现已盖印边水印与下游落印不一致（拧了）直接 409 拦截。
- 两种工程命运（`POST /api/lineage/edges/{id}/repair`）：
  - **回滚**：下游整笔恢复为待盖印（清水印/盖印时间），同批入边退回待盖印；上游缺口仍待盖印、未放行。
  - **隔离**：上游缺口落隔离凭证（`ISO-CERT-...`）后放行，边置“已隔离”；保留下游已盖印不动。
- 修复在 `BEGIN IMMEDIATE` 排他事务内一次改库，失败整体回滚并回白话；锁冲突返回 409，不双写。
- 流水表 `repair_journals` 逐条记录结局；追索席回看的边结局与血缘队列芯片取自同一数据。
- 缺口 = 应盖印 − 已盖印（非负），不再虚高；摘要附注“已隔离放行 N”。

## 三、复现步骤

### 1. 自动化二验（推荐）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q                       # 23 passed
```

关键用例（`tests/test_lineage_repair.py`、`tests/test_lineage_api.py`）：

| 用例 | 验证点 |
|------|--------|
| `test_seed_has_broken_edges_and_one_healthy` | 预置 2 条断链 + 1 条同源健康边 |
| `test_repair_rollback_restores_downstream_pending` | 回滚命运：下游恢复待盖、上游仍待盖 |
| `test_repair_isolate_keeps_downstream_stamped_and_certifies` | 隔离命运：凭证放行、下游保持已盖 |
| `test_twisted_source_is_blocked` | 边印与下游不同源时回滚被拦 |
| `test_journal_records_each_outcome_and_trace_matches` | 流水可核，追索回看与芯片一致 |
| `test_gap_count_not_inflated` | 缺口=应盖−已盖，不再 +2 |
| `test_concurrent_repair_exclusive_lock_no_double_write` | 两员并发仅一个成功、下游态不双写 |
| `test_held_write_lock_blocks_second_repair` | 持锁时修复失败且未改库，释放后可修 |

### 2. 手工界面复现

```bash
python run.py        # http://127.0.0.1:7000  账号 admin / 123456
```

1. 登录后进入底栏「血缘」页：可见 2 条红色“断链·待盖”边，上游卡片为待盖印、下游附页为已盖印。
2. 对一条断点「隔离」：边变蓝芯片并显示 `ISO-CERT-...`，下游仍已盖印；流水新增“隔离”行。
3. 对另一条断点「回滚」：下游 `gmv-20261114-01` 恢复待盖印、水印清空；流水新增“回滚”行。
4. 对已隔离边再次点修复：弹出白话错误“该缺口已落隔离凭证，无需重复修复”。
5. 切「追索」页，用 `WM-ORD-131` 追索：边结局芯片与血缘页一致。
6. 切「缺口」页生成对账：缺口数 = 应盖 − 已盖，摘要含“已隔离放行 N”。

### 3. 并发排他锁复现

```python
import threading
from app.services import lineage
# 两名血缘员对同一条断链同时点修复
ts = [threading.Thread(target=lineage.repair_edge, args=(edge_id, act,),
                       kwargs={"db_path": db}) for act in ("回滚", "隔离")]
[t.start() for t in ts]; [t.join() for t in ts]
# 仅一个成功，repair_journals 中该边只有 1 条，下游态只被写一次
```

## 四、存量库升级

老库 `lineage_edges` 无 `edge_status` 等列时，`init_db` 自动 `ALTER TABLE` 补齐并默认“待盖印”，
同时新建 `repair_journals`；无需手工迁移、不丢存量数据。

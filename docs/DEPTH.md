# 深度登记 公交刷卡换乘优惠与日封顶回算

规模：中型。使用者：公交清分中心复核员。核心规则：换乘按上次有效付费行程的时间窗计算，优惠与日封顶按确定顺序裁决并保留分币守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 刷卡采信 | 查看并运行刷卡采信 | `/api/taps` | `app/modules/m1_taps/rules.py` | `tests/test_m1.py` |
| M2 线路换乘 | 查看并运行线路换乘 | `/api/routes` | `app/modules/m2_routes/rules.py` | `tests/test_m2.py` |
| M3 票价版本 | 查看并运行票价版本 | `/api/fares` | `app/modules/m3_fares/rules.py` | `tests/test_m3.py` |
| M4 行程配对 | 查看并运行行程配对 | `/api/journeys` | `app/modules/m4_journeys/rules.py` | `tests/test_m4.py` |
| M5 优惠回算 | 查看并运行优惠回算 | `/api/discounts` | `app/modules/m5_discounts/rules.py` | `tests/test_m5.py` |
| M6 清分对账 | 查看并运行清分对账 | `/api/clearing` | `app/modules/m6_clearing/rules.py` | `tests/test_m6.py` |
| M7 日结归档 | 查看并运行日结归档 | `/api/settlement` | `app/modules/m7_settlement/rules.py` | `tests/test_m7.py` |
| M8 快照裁决 | 摄入累计快照并裁决撤回 | `/snapshots` | `app/snapshot/rules.py` `app/snapshot/store.py` | `tests/test_snapshot_rules.py` `tests/test_snapshot_store.py` |
| M9 卡日账册 | 发布/封存每卡日换乘封顶日账 | `/ledgers` | `app/snapshot/store.py` | `tests/test_snapshot_store.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 保留首条并隔离冲突行 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-06 | 全角数字 | 转换失败 | NFKC与Decimal | `app/values.py` | `tests/test_m3.py::test_thousands_and_fullwidth` |
| TRAP-07 | 金额哨兵 | 零参与扣费 | 空值隔离 | `app/values.py` | `tests/test_m3.py::test_real_sentinel_quarantined` |
| TRAP-11 | 混合偏移 | 落错版本 | UTC归整 | `app/values.py` | `tests/test_m3.py::test_timezone_equal` |
| TRAP-23 | 换乘截止点 | 重复优惠 | 截止点排除 | `app/modules/m5_discounts/rules.py` | `tests/test_m5.py::test_exact_window_excluded` |
| TRAP-25 | 票价重叠 | 随意取版 | 冲突隔离 | `app/modules/m3_fares/rules.py` | `tests/test_m3.py::test_overlap_rejected` |
| TRAP-30 | 快照缺席即撤回 | 掉线丢记录 | 宽限内 offline_pending 继续占额 | `app/snapshot/rules.py` | `tests/test_snapshot_rules.py::test_single_absence_is_offline_pending_and_kept_in_ledger` |
| TRAP-31 | 误刷长期占位 | 全保留不释放 | 连续完整快照缺席达阈值且超宽限才 withdrawn | `app/snapshot/rules.py` | `tests/test_snapshot_rules.py::test_consecutive_complete_absence_past_grace_withdraws` |
| TRAP-32 | 序号断档误判撤回 | 自动撤回覆盖掉线 | stream_gap 永久挂起待人工 | `app/snapshot/rules.py` | `tests/test_snapshot_rules.py::test_sequence_gap_holds_forever_not_auto_withdrawn` |
| TRAP-33 | 同快照重试重复占额 | 重复入库 | 内容哈希幂等，冲突 409 不覆盖 | `app/snapshot/store.py` | `tests/test_snapshot_store.py::test_retry_is_idempotent_no_double_share` |
| TRAP-34 | 缺件上传替换已发布日账 | 静默覆盖 | 版本化发布，漂移只追加新版，旧版可查 | `app/snapshot/store.py` | `tests/test_snapshot_store.py::test_publish_creates_then_drifts_then_new_version_keeps_old` |
| TRAP-35 | 未对平封存 | 带差异封账 | 对平+无挂起+乐观锁三重守卫 | `app/snapshot/store.py` | `tests/test_snapshot_store.py::test_seal_requires_balance_and_pending_clearance` |
| TRAP-36 | 发布中断污染日账 | 半成品可见 | 事务内重算回读，异常整体回滚 | `app/snapshot/store.py` | `tests/test_snapshot_store.py::test_publish_failure_keeps_old_ledger_intact` |
| TRAP-37 | 撤回后重现未复活 | 额度永久丢失 | revived 重新占额，挂起后重现解除 | `app/snapshot/rules.py` | `tests/test_snapshot_rules.py::test_revived_after_withdrawal_reoccupies` |

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按输入摘要、规则版本及参数产生唯一批次，事务写入；同源重跑只复用已有批次。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录。

# 数据定义

刷卡事件以event_id去重，card保留前导零。route支持1路/01等别名。event_time必须带偏移；业务日为Asia/Shanghai。result仅success可作为行程，失败刷卡隔离。reported为实扣人民币元。票价F1至2026-11-01前为2元，F2为2.50元。

跨线路换乘必须在上一次实际付费行程后60分钟内，恰好60分钟排除。同线路无优惠；零付费行程不能延长换乘锚点。先扣1元换乘优惠，再对卡号业务日施加6元封顶。每条基本票价=实算扣费+换乘优惠+封顶优惠。

业务ID和超大整数按文本保形；Decimal参与数量金额计算，对外序列化为字符串。哨兵集合定义在app/values.py，缺失值不能当零。所有时间带UTC偏移，先转UTC比较，业务日按指定地区划分。区间统一[start,end)。重复业务键保留第一条、完全相同行计重复；值冲突行隔离且保留原值。空文件、全空白、仅表头分开拒绝。

运行清单包括输入sha256、规则版本、参数、生成时刻与耗时。源行编号以CSV解析器的物理结束行号表示，引号内换行不增加记录数。输入超过4MB返回413，非法表头和类型返回422，失效版本复核返回409。

## 累计快照输入（M8 快照裁决）

列：`snapshot_seq,device_id,snapshot_time,is_complete,event_id,card,route,event_time,result,reported`。
一份文件只能描述同一 `(device_id, snapshot_seq)` 的一份快照，`snapshot_time` 必须带时区，`is_complete` 取 true/false（亦接受 1/是/完整）。完整快照允许零事件，但需携带一行仅含快照元数据、事件字段留空的声明行；不完整快照零事件直接拒绝（无法区分掉线与空传）。坏行进 `snap_quarantine`，不污染快照事实。

撤回裁决（只针对“完整快照中的缺席”，部分快照无缺席证明力）：

| 情形 | 状态 | 是否占换乘/封顶 |
|---|---|---|
| 首次出现、内容一致保留 | active | 占 |
| 字段或金额变化 | active + versions 留痕（UPDATE） | 按新值占 |
| 序号连续、宽限 48h 内缺席 | offline_pending | 占（防短暂掉线丢记录） |
| 连续 ≥2 份完整快照缺席且超宽限 | withdrawn | 释放，进排除清单 |
| 最后出现后存在序号断档 | stream_gap | 占，挂起待人工，永不自动撤回 |
| 撤回后重现 | revived | 重新占额 |
| 人工 withdraw / retain | manual_withdraw / manual_retain | 按裁决执行并留痕 |

同一 `(device_id,business_day,snapshot_seq)` 重试必须逐字节一致，内容冲突返回 409 且不覆盖；重复摄入幂等。日账以 `(card,business_day)` 为唯一键版本化发布：候选与现版一致不产生新版本；漂移只追加新版本，旧版永久可查。封存要求版本乐观锁匹配、`balanced=true`（分币守恒且应扣合计=申报合计）且无 stream_gap/offline_pending 事件；已封存日账拒绝任何改写。
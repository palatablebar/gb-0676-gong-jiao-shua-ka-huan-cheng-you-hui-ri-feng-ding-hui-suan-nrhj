# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 累计快照与卡日账（SQLite：snapshots.db）

- `POST /api/snapshots/ingest`：multipart 字段 file，摄入一份累计快照；同号重试幂等（`duplicated=true`），内容冲突 409，序号倒退 409。
- `POST /api/snapshots/ingest-text`：JSON `{"csv": "..."}`，同构文本入口，供页面演示序列使用。
- `GET /api/snapshots/streams`：全部车机×业务日快照流。
- `GET /api/snapshots/<device_id>/<business_day>`：重放该流，返回每份快照、每个事件的状态/版本/裁决轨迹、每卡日账候选与已发布版本漂移情况、隔离行与未裁决事件。
- `POST /api/snapshots/<device_id>/<business_day>/events/<event_id>/decision`：JSON `{"decision":"withdraw|retain"}` 人工裁决挂起事件。
- `POST /api/ledgers/<device_id>/<business_day>/<card>/publish`：事务内重算并发布；返回 action=created|unchanged|new_version，发布中断回滚、旧版不动。
- `GET /api/ledgers/<card>/<business_day>`：最新日账及全部版本历史（含 sha256、发布/封存时刻）。
- `POST /api/ledgers/<card>/<business_day>/seal`：JSON `{"version":N}` 乐观锁封存；未发布 422、版本不符 409、未对平或有未裁决事件 422、封存后改写 409。
- 页面：`/snapshots`（逐快照保留/更新/缺席矩阵与事件裁决轨迹）、`/ledgers`（唯一卡日账、排除事件、版本历史与封存）。

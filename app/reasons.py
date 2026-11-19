REASONS={"BAD_COLUMNS":"列数不匹配","EMPTY_VALUE":"必填值为空","INVALID_NUMBER":"数字格式错误","INVALID_TIME":"时间缺少偏移或格式错误","DUPLICATE_CONFLICT":"同键取值冲突","UNKNOWN_REFERENCE":"引用不存在","STALE_VERSION":"版本已更新","EMPTY_FILE":"文件为空","HEADER_ONLY":"仅有表头","BLANK_ONLY":"全部为空行",
"SNAP_BAD_HEADER":"累计快照表头不匹配","SNAP_META_CONFLICT":"同快照号元数据冲突（重试内容必须一致）","SNAP_OUT_OF_ORDER":"快照序号倒退","SNAP_BAD_META":"快照元数据缺失或非法","SNAP_EMPTY":"快照不含任何记录且未声明完整","EVENT_CONFLICT":"同事件号取值冲突，保留首版并隔离","STREAM_GAP":"上游序号断档，掉线怀疑永久挂起","OFFLINE_PENDING":"最新完整快照缺席，离线宽限内保留","WITHDRAWN":"连续完整快照缺席达阈值，裁决撤回","REVIVED":"撤回后重现，按新事件复活","MANUAL_WITHDRAW":"人工裁决撤回","MANUAL_RETAIN":"人工裁决保留","LEDGER_NOT_BALANCED":"账未对平，禁止封存","LEDGER_PENDING_EVENTS":"存在未裁决事件，禁止封存","LEDGER_SEALED":"已封存日账不可改写","LEDGER_NO_PUBLISH":"日账尚未发布，不能封存","LEDGER_STALE":"日账版本已更新"}
def reject(row, code, detail):
    return {**row,"reason":code,"detail":detail}
def sources(row):
    return row.get("_sources", [row.get("_line",0)])

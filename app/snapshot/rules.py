"""累计快照规则：撤回裁决 + 每卡日换乘/封顶日账。

裁决原则（回答“旧记录怎样才算撤回”）：
1. 快照是某车机（device）某业务日（business_day）的累计视图，以 (device, business_day, seq)
   唯一标识；同一标识重试必须内容一致，否则拒绝，重复上传天然幂等、不重复占额。
2. 最新快照“缺席”绝不立即等同撤回，分三种情形：
   - 事件最后出现之后，上游序号断档（seq 不连续）：无法区分掉线与撤回，缺席事件挂起为
     STREAM_GAP，直到人工裁决或事件重现，绝不自动撤回。
   - 序号连续、宽限期内（默认 48h，按快照接收时刻）缺席：OFFLINE_PENDING，继续占额，
     防止车机短暂掉线丢记录。
   - 序号连续、连续 >= withdrawal_threshold 份“完整快照”缺席且超出宽限：裁决 WITHDRAWN，
     事件释放换乘锚点与封顶额度。
3. 撤回后事件在后续完整快照重现：REVIVED 复活，重新进入占额集合（日账需重新发布）。
4. 人工可对挂起事件显式裁决（撤回/保留），全程留痕。
日账只对每张卡每个业务日产生唯一版本：对平前不可封存，封存后不可改写；
发布/封存为事务动作，中断不会替换已发布旧版。
"""
import csv
import io
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.errors import DomainError
from app.reasons import reject
from app.values import instant, number, money

SNAP_COLUMNS = ["snapshot_seq", "device_id", "snapshot_time", "is_complete",
                "event_id", "card", "route", "event_time", "result", "reported"]
DEFAULT_OFFLINE_GRACE_HOURS = 48
DEFAULT_WITHDRAWAL_THRESHOLD = 2  # 连续缺席多少份“完整快照”才裁决撤回
ACTIVE_STATES = {"active", "updated", "offline_pending", "revived", "manual_retain"}
SHANGHAI = timezone(timedelta(hours=8))
ROUTE_ALIASES = {"01": "01", "1路": "01", "02": "02", "2路": "02", "03": "03", "3路": "03"}
FARES = [
    {"start": "2026-01-01T00:00:00+08:00", "end": "2026-11-01T00:00:00+08:00", "version": "F1", "amount": Decimal("2.00")},
    {"start": "2026-11-01T00:00:00+08:00", "end": "2027-01-01T00:00:00+08:00", "version": "F2", "amount": Decimal("2.50")},
]


def _decode(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise DomainError("ENCODING", "无法识别文件编码")


def _truthy(text):
    return str(text).strip().lower() in {"1", "true", "yes", "y", "是", "完整"}


def parse_snapshot(raw):
    """解析一份累计快照 CSV，返回 (meta, rows, quarantine, metrics)。

    完整快照允许零事件（表示该累计视图当前为空）；不完整快照零事件无法区分掉线与空传，拒绝。
    """
    text, encoding = _decode(raw)
    if not text or not text.strip():
        raise DomainError("EMPTY_FILE", "快照为空")
    stream = csv.reader(io.StringIO(text, newline=""))
    header = next(stream, None)
    if header != SNAP_COLUMNS:
        raise DomainError("SNAP_BAD_HEADER", "累计快照表头不匹配，需为：" + ",".join(SNAP_COLUMNS))
    rows, bad = [], []
    seq = device = snap_time = None
    is_complete = None
    complete_declared = False
    metadata_only = False
    seen_events = set()
    line_count = 0
    for cells in stream:
        line_count += 1
        line = stream.line_num
        if not cells or not any(c.strip() for c in cells):
            continue
        if len(cells) != len(SNAP_COLUMNS):
            bad.append(reject({"_line": line, "_sources": [line]}, "BAD_COLUMNS", "列数不匹配"))
            continue
        item = dict(zip(SNAP_COLUMNS, [c.strip() for c in cells]))
        item["_line"] = line
        item["_sources"] = [line]
        try:
            item_seq = number(item["snapshot_seq"])
            if item_seq != int(item_seq) or item_seq < 0:
                raise ValueError
            item_seq = int(item_seq)
            if not item["device_id"]:
                raise DomainError("EMPTY_VALUE", "device_id 为空")
            item_time = instant(item["snapshot_time"])
            item_complete = _truthy(item["is_complete"])
        except (DomainError, ValueError):
            bad.append(reject(item, "SNAP_BAD_META", "快照序号/车机/时间/完整标记非法"))
            continue
        if seq is None:
            seq, device, snap_time, is_complete = item_seq, item["device_id"], item_time, item_complete
            complete_declared = True
        elif (item_seq, item["device_id"], item_time.isoformat(), item_complete) != (seq, device, snap_time.isoformat(), is_complete):
            bad.append(reject(item, "SNAP_BAD_META", "同一文件内快照元数据不一致"))
            continue
        if not item["event_id"]:
            # 完整快照可用“纯元数据行”承载零事件视图
            if all(not item[k] for k in ("event_id", "card", "route", "event_time", "result", "reported")):
                metadata_only = True
                continue
            bad.append(reject(item, "EMPTY_VALUE", "事件业务键为空"))
            continue
        if item["event_id"] in seen_events:
            bad.append(reject(item, "DUPLICATE_CONFLICT", "快照内事件键重复"))
            continue
        seen_events.add(item["event_id"])
        try:
            at = instant(item["event_time"])
            reported = number(item["reported"])
            if reported < 0:
                raise DomainError("NEGATIVE_FARE", "实扣金额不能为负")
            if item["result"] not in {"success", "failed"}:
                raise DomainError("INVALID_VALUE", "result 仅支持 success/failed")
            if not item["card"]:
                raise DomainError("EMPTY_CARD", "卡号为空")
            if ROUTE_ALIASES.get(item["route"]) is None:
                raise DomainError("UNKNOWN_ROUTE", "线路未登记")
        except DomainError as e:
            bad.append(reject(item, e.code, e.message))
            continue
        item.update({"event_time": at.isoformat(), "reported": money(reported),
                     "snapshot_time": snap_time.isoformat(), "snapshot_seq": seq,
                     "is_complete": is_complete,
                     "business_day": at.astimezone(SHANGHAI).date().isoformat(),
                     "route": ROUTE_ALIASES[item["route"]]})
        rows.append(item)
    if not complete_declared:
        if bad:
            raise DomainError("SNAP_BAD_META", "没有可识别的快照元数据行")
        raise DomainError("HEADER_ONLY", "仅有表头")
    if not rows and not is_complete:
        raise DomainError("SNAP_EMPTY", "不完整快照不允许零记录（无法区分掉线与空传）")
    if not rows and is_complete and not metadata_only:
        raise DomainError("SNAP_EMPTY", "完整空快照需携带一行纯元数据以声明完整性")
    meta = {"snapshot_seq": seq, "device_id": device,
            "snapshot_time": snap_time.isoformat(), "is_complete": is_complete,
            "business_day": snap_time.astimezone(SHANGHAI).date().isoformat()}
    metrics = {"input": line_count, "rows": len(rows), "quarantine": len(bad),
               "encoding": encoding, "is_complete": is_complete}
    return meta, rows, bad, metrics


def _fare(at):
    hits = [f for f in FARES if instant(f["start"]) <= at < instant(f["end"])]
    if len(hits) != 1:
        raise DomainError("FARE_CONFLICT", "票价无效或生效窗重叠")
    return hits[0]


def _gap_between(snap_seqs, low, high):
    return any(x not in snap_seqs for x in range(low + 1, high))


def _version(seq, rec):
    return {"seq": seq, "reported": rec["reported"], "route": rec["route"],
            "event_time": rec["event_time"], "result": rec["result"]}


def _fingerprint(rec):
    return tuple(rec.get(k, "") for k in ("card", "route", "event_time", "result", "reported"))


def adjudicate(stream_snapshots, grace_hours=DEFAULT_OFFLINE_GRACE_HOURS,
               threshold=DEFAULT_WITHDRAWAL_THRESHOLD, now=None, overrides=None):
    """对同一 (device, business_day) 的全部快照重放，裁决每个事件的生命周期。

    纯函数：输入为已持久化的快照（含 received_at）与人工裁决 overrides，
    随时可从事实表重建，不依赖易失状态机。
    """
    now = now or datetime.now(timezone.utc)
    overrides = overrides or {}
    snaps = sorted(stream_snapshots, key=lambda s: (s["snapshot_seq"], s["received_at"]))
    snap_seqs = {s["snapshot_seq"] for s in snaps}
    first_received = {s["snapshot_seq"]: s["received_at"] for s in snaps}
    events = {}
    for snap in snaps:
        seq = snap["snapshot_seq"]
        present = {r["event_id"]: r for r in snap["rows"]}
        # 1) 本快照出现的事件：新增 / 更新 / 复活
        for eid, rec in present.items():
            if eid not in events:
                events[eid] = {"event_id": eid, "state": "active", "first_seen_seq": seq,
                               "last_seen_seq": seq, "absent_complete_count": 0,
                               "versions": [_version(seq, rec)], "current": rec,
                               "history": [{"seq": seq, "action": "RETAIN", "reason": "首次出现"}],
                               "manual": None}
            else:
                ev = events[eid]
                if _fingerprint(rec) != _fingerprint(ev["current"]):
                    ev["versions"].append(_version(seq, rec))
                    ev["history"].append({"seq": seq, "action": "UPDATE",
                                          "reason": "金额/字段变更，旧版留痕"})
                else:
                    ev["history"].append({"seq": seq, "action": "RETAIN",
                                          "reason": "快照保留，内容一致"})
                if ev["state"] in {"withdrawn", "manual_withdraw"}:
                    ev["state"] = "revived"
                    ev["history"].append({"seq": seq, "action": "REVIVE",
                                          "reason": "撤回后在快照重现，复活占额"})
                elif ev["state"] in {"offline_pending", "stream_gap"}:
                    ev["state"] = "active"
                    ev["history"].append({"seq": seq, "action": "RETAIN",
                                          "reason": "缺席后重现，挂起解除并恢复占额"})
                ev["current"], ev["last_seen_seq"], ev["absent_complete_count"] = rec, seq, 0
        # 2) 本快照缺席的既有事件（仅完整快照有裁决意义）
        if snap["is_complete"]:
            for eid, ev in events.items():
                if eid in present or ev["state"] in {"withdrawn", "manual_withdraw"}:
                    continue
                ev["absent_complete_count"] += 1
                age_hours = (now - instant(first_received[ev["last_seen_seq"]])).total_seconds() / 3600
                if _gap_between(snap_seqs, ev["last_seen_seq"], seq):
                    if ev["state"] != "stream_gap":
                        ev["history"].append({"seq": seq, "action": "SUSPEND",
                                              "reason": "STREAM_GAP 上游序号断档，缺席挂起待人工"})
                    ev["state"] = "stream_gap"
                elif ev["absent_complete_count"] >= threshold and age_hours > grace_hours:
                    ev["state"] = "withdrawn"
                    ev["history"].append({"seq": seq, "action": "WITHDRAW",
                                          "reason": f"连续{ev['absent_complete_count']}份完整快照缺席且超{grace_hours}h宽限"})
                else:
                    if ev["state"] != "offline_pending":
                        ev["history"].append({"seq": seq, "action": "SUSPEND",
                                              "reason": f"OFFLINE_PENDING 宽限内缺席（{age_hours:.1f}h），继续占额"})
                    ev["state"] = "offline_pending"
    # 3) 人工裁决覆盖（对挂起事件；复活事件的旧人工标记已在重现时自然失效）
    for eid, decision in overrides.items():
        ev = events.get(eid)
        if ev is None or ev["state"] in {"withdrawn", "manual_withdraw"}:
            continue
        if decision == "withdraw" and ev["state"] in {"stream_gap", "offline_pending", "active", "updated", "revived", "manual_retain"}:
            ev["state"] = "manual_withdraw"
            ev["manual"] = "withdraw"
            ev["history"].append({"seq": None, "action": "MANUAL_WITHDRAW", "reason": "人工裁决撤回，释放占额"})
        elif decision == "retain" and ev["state"] in {"stream_gap", "offline_pending"}:
            ev["state"] = "manual_retain"
            ev["manual"] = "retain"
            ev["history"].append({"seq": None, "action": "MANUAL_RETAIN", "reason": "人工裁决保留，继续占额"})
    return events


def build_ledger(events, card, business_day, params=None):
    """对一张卡一个业务日的事件集合，生成唯一可发布的换乘+封顶日账。"""
    params = params or {}
    cap = number(params.get("daily_cap", "6.00"))
    discount = number(params.get("transfer_discount", "1.00"))
    window = number(params.get("transfer_minutes", "60"))
    card_events = [ev for ev in events.values() if ev["current"].get("card") == card
                   and ev["current"].get("business_day") == business_day]
    usable = [ev for ev in card_events
              if ev["state"] in ACTIVE_STATES and ev["current"]["result"] == "success"]
    records = []
    spent = Decimal("0")
    anchor = None
    transfer_hits = 0
    for ev in sorted(usable, key=lambda e: (instant(e["current"]["event_time"]), e["event_id"])):
        rec = ev["current"]
        at = instant(rec["event_time"])
        fare = _fare(at)
        base = fare["amount"]
        eligible = False
        if anchor is not None:
            delta = Decimal(str((at - instant(anchor["event_time"])).total_seconds())) / 60
            eligible = anchor["route"] != rec["route"] and Decimal(0) <= delta < window
        transfer = min(base, discount) if eligible else Decimal(0)
        after_transfer = base - transfer
        remaining = max(Decimal(0), cap - spent)
        charge = min(after_transfer, remaining)
        cap_discount = after_transfer - charge
        spent += charge
        if eligible:
            transfer_hits += 1
        line = {
            "event_id": ev["event_id"], "card": rec["card"], "route": rec["route"],
            "at": at.isoformat(), "state": ev["state"], "fare_version": fare["version"],
            "base_fare": money(base), "transfer_anchor": anchor["event_id"] if eligible else "—",
            "transfer_discount": money(transfer), "cap_discount": money(cap_discount),
            "expected": money(charge), "reported": rec["reported"],
            "difference": money(charge - number(rec["reported"])), "day_paid": money(spent),
        }
        line["status"] = "通过" if line["difference"] == "0.00" else "差异"
        records.append(line)
        if charge > 0:
            anchor = rec
    expected_total = sum((number(r["expected"]) for r in records), Decimal(0))
    reported_total = sum((number(r["reported"]) for r in records), Decimal(0))
    conserved = all(number(r["base_fare"]) == number(r["transfer_discount"]) +
                    number(r["cap_discount"]) + number(r["expected"]) for r in records)
    balanced = conserved and expected_total == reported_total
    excluded = []
    for ev in card_events:
        if ev in usable:
            continue
        if ev["current"]["result"] != "success":
            reason = "FAILED_TAP 失败刷卡不构成行程"
        else:
            reason = next((h["reason"] for h in reversed(ev["history"])
                           if h["action"] in {"WITHDRAW", "SUSPEND", "MANUAL_WITHDRAW"}), ev["state"])
        excluded.append({"event_id": ev["event_id"], "state": ev["state"], "reason": reason})
    return {
        "card": card, "business_day": business_day, "records": records, "excluded": excluded,
        "metrics": {
            "events_total": len(records), "transfer_hits": transfer_hits,
            "expected_total": money(expected_total), "reported_total": money(reported_total),
            "difference_total": money(expected_total - reported_total),
            "differences": sum(r["status"] == "差异" for r in records),
            "balanced": balanced, "capped": any(r["cap_discount"] != "0.00" for r in records),
        },
    }

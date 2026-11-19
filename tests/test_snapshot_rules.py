"""累计快照撤回裁决与卡日账的规则测试。"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.errors import DomainError
from app.snapshot.rules import (
    parse_snapshot, adjudicate, build_ledger,
    DEFAULT_OFFLINE_GRACE_HOURS, DEFAULT_WITHDRAWAL_THRESHOLD, SNAP_COLUMNS,
)
from app.snapshot import store

HEADER = ",".join(SNAP_COLUMNS)


def snap(seq, complete, time, rows, device="D1"):
    lines = [HEADER]
    if not rows:  # 零事件快照：用纯元数据行声明快照身份与完整标记
        lines.append(",".join([str(seq), device, time, str(complete).lower(), "", "", "", "", "", ""]))
        return ("\n".join(lines) + "\n").encode()
    for r in rows:
        lines.append(",".join([str(seq), device, time, str(complete).lower(), *r]))
    return ("\n".join(lines) + "\n").encode()


T = "2026-11-16T09:00:00+08:00"
EV = ["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]


def test_parse_basic_and_bad_header():
    meta, rows, bad, metrics = parse_snapshot(snap(1, True, T, [EV]))
    assert meta == {"snapshot_seq": 1, "device_id": "D1", "snapshot_time": "2026-11-16T01:00:00+00:00",
                    "is_complete": True, "business_day": "2026-11-16"}
    assert rows[0]["business_day"] == "2026-11-16" and metrics["quarantine"] == 0
    with pytest.raises(DomainError) as e:
        parse_snapshot(b"wrong,header\n1,x\n")
    assert e.value.code == "SNAP_BAD_HEADER"


def test_bad_rows_quarantined_not_poisoning_snapshot():
    raw = snap(1, True, T, [
        EV,
        ["E2", "C1", "99", "2026-11-16T08:10:00+08:00", "success", "2.50"],   # 未登记线路
        ["E3", "C1", "02", "2026-11-16T08:20:00", "success", "2.50"],         # 缺时区
    ])
    meta, rows, bad, metrics = parse_snapshot(raw)
    assert {b["reason"] for b in bad} == {"UNKNOWN_ROUTE", "INVALID_TIME"}
    assert [r["event_id"] for r in rows] == ["E1"]


def test_incomplete_empty_snapshot_rejected():
    with pytest.raises(DomainError) as e:
        parse_snapshot(snap(1, False, T, []))
    assert e.value.code == "SNAP_EMPTY"


def test_complete_zero_row_snapshot_is_valid_empty_view():
    # 完整快照用纯元数据行承载“当前零事件”视图（10 列，后 6 个事件字段留空）
    raw = (HEADER + f"\n1,D1,{T},true,,,,,,").encode()
    meta, rows, bad, metrics = parse_snapshot(raw)
    assert meta["is_complete"] is True and rows == [] and metrics["quarantine"] == 0


def _snaps(seq_rows, base=None):
    """构造带 received_at 的内存快照列表，base 为起始 UTC 时刻。"""
    base = base or datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    out = []
    for i, (seq, complete, rows) in enumerate(seq_rows):
        meta_seq_rows = rows
        out.append({"snapshot_seq": seq, "is_complete": complete,
                    "snapshot_time": base.isoformat(),
                    "received_at": (base + timedelta(hours=i)).isoformat(),
                    "rows": [_rec(seq, r, complete) for r in meta_seq_rows]})
    return out


def _rec(seq, r, complete=True):
    return {"snapshot_seq": seq, "device_id": "D1", "is_complete": complete,
            "event_id": r[0], "card": r[1], "route": r[2], "event_time": r[3],
            "result": r[4], "reported": r[5], "business_day": "2026-11-16"}


def test_single_absence_is_offline_pending_and_kept_in_ledger():
    now = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
    events = adjudicate(_snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (2, True, []),
    ]), now=now)
    assert events["E1"]["state"] == "offline_pending"
    ledger = build_ledger(events, "C1", "2026-11-16")
    assert ledger["metrics"]["events_total"] == 1  # 挂起仍占额


def test_consecutive_complete_absence_past_grace_withdraws():
    base = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    # 宽限 48h：让最后见到的快照距 now 超过 48h
    snaps = _snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (2, True, []),
        (3, True, []),
    ], base=base)
    now = base + timedelta(hours=DEFAULT_OFFLINE_GRACE_HOURS + 3)
    events = adjudicate(snaps, now=now)
    assert events["E1"]["state"] == "withdrawn"
    assert events["E1"]["history"][-1]["action"] == "WITHDRAW"
    ledger = build_ledger(events, "C1", "2026-11-16")
    assert ledger["metrics"]["events_total"] == 0
    assert ledger["excluded"][0]["state"] == "withdrawn"


def test_withdrawal_threshold_requires_two_complete_snapshots():
    base = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    snaps = _snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (2, True, []),  # 仅 1 份缺席
    ], base=base)
    now = base + timedelta(hours=DEFAULT_OFFLINE_GRACE_HOURS + 5)
    events = adjudicate(snaps, now=now)
    # 只缺席一份完整快照，未达阈值 2，即使超宽限也不能撤回
    assert events["E1"]["state"] == "offline_pending"


def test_incomplete_snapshot_never_counts_as_withdrawal_evidence():
    base = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    snaps = _snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (2, False, []),
        (3, False, []),
    ], base=base)
    now = base + timedelta(hours=100)
    events = adjudicate(snaps, now=now)
    # 部分快照不具缺席证明力：事件保持 active，不挂起更不能撤回
    assert events["E1"]["state"] == "active"
    assert events["E1"]["absent_complete_count"] == 0


def test_sequence_gap_holds_forever_not_auto_withdrawn():
    base = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    snaps = _snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (4, True, []),  # 2、3 断档
        (5, True, []),
        (6, True, []),
    ], base=base)
    now = base + timedelta(days=30)
    events = adjudicate(snaps, now=now)
    assert events["E1"]["state"] == "stream_gap"
    # 人工裁决可解锁
    events2 = adjudicate(snaps, now=now, overrides={"E1": "withdraw"})
    assert events2["E1"]["state"] == "manual_withdraw"


def test_revived_after_withdrawal_reoccupies():
    base = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    row = ["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]
    snaps = _snaps([
        (1, True, [row]), (2, True, []), (3, True, []),
        (4, True, [row]),  # 重现
    ], base=base)
    now = base + timedelta(hours=DEFAULT_OFFLINE_GRACE_HOURS + 5)
    events = adjudicate(snaps, now=now)
    assert events["E1"]["state"] == "revived"
    assert any(h["action"] == "REVIVE" for h in events["E1"]["history"])
    assert build_ledger(events, "C1", "2026-11-16")["metrics"]["events_total"] == 1


def test_amount_change_creates_version_history():
    snaps = _snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (2, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "1.50"]]),
    ])
    events = adjudicate(snaps)
    assert len(events["E1"]["versions"]) == 2
    assert [h["action"] for h in events["E1"]["history"]] == ["RETAIN", "UPDATE"]


def test_pending_event_clears_on_reappearance():
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    row = ["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]
    events = adjudicate(_snaps([
        (1, True, [row]), (2, True, []), (3, True, [row]),  # 掉线后恢复
    ]), now=now)
    assert events["E1"]["state"] == "active"
    assert events["E1"]["absent_complete_count"] == 0
    assert "挂起解除" in events["E1"]["history"][-1]["reason"]


def test_manual_retain_keeps_share():
    base = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    snaps = _snaps([
        (1, True, [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"]]),
        (4, True, []),
    ], base=base)
    events = adjudicate(snaps, now=base + timedelta(days=10), overrides={"E1": "retain"})
    assert events["E1"]["state"] == "manual_retain"
    assert build_ledger(events, "C1", "2026-11-16")["metrics"]["events_total"] == 1


def test_ledger_transfer_cap_and_conservation():
    # 三次成功：01→02→03，窗口内换乘各减 1；合计 2.50+1.50+1.50=5.50（未触 6 元封顶）
    rows = [
        ["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"],
        ["E2", "C1", "02", "2026-11-16T08:15:00+08:00", "success", "1.50"],
        ["E3", "C1", "03", "2026-11-16T08:40:00+08:00", "success", "1.50"],
    ]
    events = adjudicate(_snaps([(1, True, rows)]))
    ledger = build_ledger(events, "C1", "2026-11-16")
    r = ledger["records"]
    assert r[0]["transfer_discount"] == "0.00" and r[0]["expected"] == "2.50"
    assert r[1]["transfer_discount"] == "1.00" and r[1]["expected"] == "1.50"
    assert r[2]["transfer_discount"] == "1.00" and r[2]["expected"] == "1.50"
    assert r[2]["day_paid"] == "5.50"
    assert ledger["metrics"]["expected_total"] == "5.50"
    assert ledger["metrics"]["balanced"] is True
    for line in r:
        assert Decimal(line["base_fare"]) == Decimal(line["transfer_discount"]) + \
            Decimal(line["cap_discount"]) + Decimal(line["expected"])


def test_daily_cap_holds_after_withdrawal_releases_share():
    # 4 次行程：2.50+1.50+1.50=5.50，第四次（换线、窗口外无换乘）基础 2.50 只剩 0.50 额度
    rows = [
        ["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "2.50"],
        ["E2", "C1", "02", "2026-11-16T08:15:00+08:00", "success", "1.50"],
        ["E3", "C1", "03", "2026-11-16T08:40:00+08:00", "success", "1.50"],
        ["E4", "C1", "02", "2026-11-16T10:30:00+08:00", "success", "0.50"],
    ]
    ledger = build_ledger(adjudicate(_snaps([(1, True, rows)])), "C1", "2026-11-16")
    assert ledger["metrics"]["capped"] is True
    assert ledger["records"][-1]["cap_discount"] == "2.00"
    assert ledger["records"][-1]["day_paid"] == "6.00"
    assert ledger["metrics"]["expected_total"] == "6.00"


def test_unbalanced_ledger_detected():
    rows = [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "success", "9.00"]]
    ledger = build_ledger(adjudicate(_snaps([(1, True, rows)])), "C1", "2026-11-16")
    assert ledger["metrics"]["balanced"] is False
    assert ledger["records"][0]["status"] == "差异"


def test_failed_tap_excluded_from_ledger():
    rows = [["E1", "C1", "01", "2026-11-16T08:00:00+08:00", "failed", "0.00"]]
    ledger = build_ledger(adjudicate(_snaps([(1, True, rows)])), "C1", "2026-11-16")
    assert ledger["metrics"]["events_total"] == 0
    assert "FAILED_TAP" in ledger["excluded"][0]["reason"]

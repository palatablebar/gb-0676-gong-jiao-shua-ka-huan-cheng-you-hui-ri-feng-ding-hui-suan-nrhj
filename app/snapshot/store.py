"""累计快照持久化：SQLite 事务保证幂等摄入、版本化日账发布与封存。"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.config import DATA_DIR
from app.errors import DomainError
from app.snapshot.rules import (
    DEFAULT_OFFLINE_GRACE_HOURS, DEFAULT_WITHDRAWAL_THRESHOLD,
    adjudicate, build_ledger, parse_snapshot,
)


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "snapshots.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS snap_streams(
        device_id TEXT NOT NULL, business_day TEXT NOT NULL,
        snapshot_seq INTEGER NOT NULL, snapshot_time TEXT NOT NULL,
        is_complete INTEGER NOT NULL, content_sha TEXT NOT NULL,
        received_at TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(device_id,business_day,snapshot_seq))""")
    db.execute("""CREATE TABLE IF NOT EXISTS snap_quarantine(
        id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, business_day TEXT,
        snapshot_seq INTEGER, reason TEXT, detail TEXT, payload TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS event_overrides(
        device_id TEXT NOT NULL, business_day TEXT NOT NULL, event_id TEXT NOT NULL,
        decision TEXT NOT NULL, decided_at TEXT NOT NULL,
        PRIMARY KEY(device_id,business_day,event_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS card_ledgers(
        card TEXT NOT NULL, business_day TEXT NOT NULL, version INTEGER NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL, published_at TEXT, sealed_at TEXT,
        PRIMARY KEY(card,business_day,version))""")
    return db


def _content_sha(meta, rows):
    encoded = json.dumps({"meta": meta, "rows": rows}, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def ingest(raw):
    """摄入一份快照。重试（同 device/day/seq 且内容一致）幂等返回；内容冲突 409。"""
    meta, rows, bad, metrics = parse_snapshot(raw)
    sha = _content_sha(meta, rows)
    received = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        existing = db.execute(
            "SELECT content_sha,received_at FROM snap_streams WHERE device_id=? AND business_day=? AND snapshot_seq=?",
            (meta["device_id"], meta["business_day"], meta["snapshot_seq"])).fetchone()
        duplicated = False
        if existing:
            if existing[0] != sha:
                raise DomainError("SNAP_META_CONFLICT", "同快照号内容不一致，拒绝覆盖（重试必须逐字节一致）", 409)
            duplicated = True
            received = existing[1]
        else:
            last = db.execute(
                "SELECT MAX(snapshot_seq) FROM snap_streams WHERE device_id=? AND business_day=?",
                (meta["device_id"], meta["business_day"])).fetchone()[0]
            if last is not None and meta["snapshot_seq"] < last:
                raise DomainError("SNAP_OUT_OF_ORDER", f"快照序号倒退（当前流最大序号 {last}）", 409)
            db.execute("INSERT INTO snap_streams VALUES(?,?,?,?,?,?,?,?)",
                       (meta["device_id"], meta["business_day"], meta["snapshot_seq"],
                        meta["snapshot_time"], int(meta["is_complete"]), sha, received,
                        json.dumps({"meta": meta, "rows": rows, "metrics": metrics}, ensure_ascii=False)))
        for item in bad:
            db.execute("INSERT INTO snap_quarantine(device_id,business_day,snapshot_seq,reason,detail,payload) VALUES(?,?,?,?,?,?)",
                       (meta["device_id"], meta["business_day"], meta["snapshot_seq"],
                        item.get("reason", ""), item.get("detail", ""), json.dumps(item, ensure_ascii=False, default=str)))
    return {"snapshot": {**meta, "received_at": received, "content_sha": sha,
                         "duplicated": duplicated}, "metrics": metrics, "quarantine": bad}


def _load_stream(db, device_id, business_day):
    out = []
    for payload, in db.execute(
            "SELECT payload FROM snap_streams WHERE device_id=? AND business_day=? ORDER BY snapshot_seq",
            (device_id, business_day)):
        out.append(json.loads(payload))
    return out


def _overrides(db, device_id, business_day):
    return {eid: decision for eid, decision in db.execute(
        "SELECT event_id,decision FROM event_overrides WHERE device_id=? AND business_day=?",
        (device_id, business_day))}


def list_streams():
    with connect() as db:
        rows = db.execute(
            "SELECT device_id,business_day,COUNT(*),SUM(is_complete),MIN(received_at),MAX(received_at) "
            "FROM snap_streams GROUP BY device_id,business_day ORDER BY business_day,device_id").fetchall()
    return [{"device_id": d, "business_day": day, "snapshots": c, "complete_snapshots": f or 0,
             "first_received": a, "last_received": b} for d, day, c, f, a, b in rows]


def stream_detail(device_id, business_day, params=None):
    """重建一个车机业务日的全部事件裁决，并汇总每卡日账候选。"""
    with connect() as db:
        stream = _load_stream(db, device_id, business_day)
        if not stream:
            raise DomainError("NOT_FOUND", "快照流不存在", 404)
        overrides = _overrides(db, device_id, business_day)
        quarantine = [dict(zip(["id", "device_id", "business_day", "snapshot_seq", "reason", "detail", "payload"], r))
                      for r in db.execute(
                          "SELECT id,device_id,business_day,snapshot_seq,reason,detail,payload FROM snap_quarantine "
                          "WHERE device_id=? AND business_day=? ORDER BY id", (device_id, business_day))]
        ledger_rows = db.execute(
            "SELECT card,version,status,published_at,sealed_at,payload FROM card_ledgers WHERE business_day=? ORDER BY card,version",
            (business_day,)).fetchall()
        received_map = dict(db.execute(
            "SELECT snapshot_seq,received_at FROM snap_streams WHERE device_id=? AND business_day=?",
            (device_id, business_day)).fetchall())
    snaps = [{"snapshot_seq": s["meta"]["snapshot_seq"], "is_complete": s["meta"]["is_complete"],
              "snapshot_time": s["meta"]["snapshot_time"],
              "received_at": received_map[s["meta"]["snapshot_seq"]],
              "rows": s["rows"], "metrics": s["metrics"]} for s in stream]
    events = adjudicate(snaps, overrides=overrides)
    cards = sorted({ev["current"]["card"] for ev in events.values()})
    ledgers = []
    for card in cards:
        candidate = build_ledger(events, card, business_day, params)
        published = [r for r in ledger_rows if r[0] == card]
        current = _latest_published(published)
        ledgers.append({
            "card": card, "candidate": candidate["metrics"],
            "published": None if current is None else
            {"version": current[1], "status": current[2], "published_at": current[3], "sealed_at": current[4]},
            "drifted": current is not None and _ledger_sha(json.loads(current[5])) != _ledger_sha(candidate),
        })
    return {"device_id": device_id, "business_day": business_day,
            "snapshots": snaps, "events": sorted(events.values(), key=lambda e: e["event_id"]),
            "ledgers": ledgers, "quarantine": quarantine,
            "overrides": overrides,
            "pending": [e["event_id"] for e in events.values() if e["state"] in {"stream_gap", "offline_pending"}]}


def _latest_published(rows):
    return max(rows, key=lambda r: r[1]) if rows else None


def _ledger_sha(ledger):
    encoded = json.dumps(ledger, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def get_ledger(card, business_day):
    with connect() as db:
        if not db.execute("SELECT 1 FROM card_ledgers WHERE card=? AND business_day=?",
                          (card, business_day)).fetchone():
            raise DomainError("NOT_FOUND", "日账尚未生成", 404)
        return _read_latest(db, card, business_day)


def publish_ledger(device_id, business_day, card, params=None):
    """发布（或在漂移时追加新版本）。事务内重算并回读；失败不触碰已发布旧版。"""
    with connect() as db:
        stream = _load_stream(db, device_id, business_day)
        if not stream:
            raise DomainError("NOT_FOUND", "快照流不存在", 404)
        snaps = [{"snapshot_seq": s["meta"]["snapshot_seq"], "is_complete": s["meta"]["is_complete"],
                  "snapshot_time": s["meta"]["snapshot_time"],
                  "received_at": db.execute("SELECT received_at FROM snap_streams WHERE device_id=? AND business_day=? AND snapshot_seq=?",
                                            (device_id, business_day, s["meta"]["snapshot_seq"])).fetchone()[0],
                  "rows": s["rows"]} for s in stream]
        events = adjudicate(snaps, overrides=_overrides(db, device_id, business_day))
        if not any(ev["current"]["card"] == card for ev in events.values()):
            raise DomainError("UNKNOWN_REFERENCE", "该卡在此业务日没有事件", 404)
        candidate = build_ledger(events, card, business_day, params)
        rows = db.execute("SELECT version,status,payload FROM card_ledgers WHERE card=? AND business_day=?",
                          (card, business_day)).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        if not rows:
            db.execute("INSERT INTO card_ledgers VALUES(?,?,?,?,?,?,?)",
                       (card, business_day, 1, "已发布", json.dumps(candidate, ensure_ascii=False, default=str), now, None))
            action = "created"
        else:
            latest_version, latest_status, latest_payload = max(rows, key=lambda r: r[0])
            if latest_status == "已封存":
                raise DomainError("LEDGER_SEALED", "已封存日账不可改写，请对新业务日开账", 409)
            if _ledger_sha(json.loads(latest_payload)) == _ledger_sha(candidate):
                action = "unchanged"
            else:
                db.execute("INSERT INTO card_ledgers VALUES(?,?,?,?,?,?,?)",
                           (card, business_day, latest_version + 1, "已发布",
                            json.dumps(candidate, ensure_ascii=False, default=str), now, None))
                action = "new_version"
        saved = _read_latest(db, card, business_day)
    return {"card": card, "business_day": business_day, "action": action, "ledger": saved}


def _read_latest(db, card, business_day):
    version, status, payload, published_at, sealed_at = db.execute(
        "SELECT version,status,payload,published_at,sealed_at FROM card_ledgers "
        "WHERE card=? AND business_day=? ORDER BY version DESC LIMIT 1", (card, business_day)).fetchone()
    versions = [{"version": v, "status": st, "published_at": pa, "sealed_at": sa,
                 "sha256": _ledger_sha(json.loads(pl))}
                for v, st, pl, pa, sa in db.execute(
                    "SELECT version,status,payload,published_at,sealed_at FROM card_ledgers "
                    "WHERE card=? AND business_day=? ORDER BY version", (card, business_day))]
    return {**json.loads(payload), "version": version, "status": status,
            "published_at": published_at, "sealed_at": sealed_at, "versions": versions}


def seal_ledger(card, business_day, version):
    """封存：版本乐观锁 + 必须对平 + 无未裁决事件。未对平不得封存。"""
    with connect() as db:
        row = db.execute("SELECT version,status,payload FROM card_ledgers WHERE card=? AND business_day=?",
                         (card, business_day)).fetchall()
        if not row:
            raise DomainError("LEDGER_NO_PUBLISH", "日账尚未发布，不能封存", 422)
        latest = max(row, key=lambda r: r[0])
        if latest[0] != version:
            raise DomainError("LEDGER_STALE", "日账版本已更新，请基于最新版封存", 409)
        ledger = json.loads(latest[2])
        # 先查未裁决缺席事件：候选若已漂移，旧版同样不能封存，必须重新发布
        pending_devices = db.execute(
            "SELECT DISTINCT device_id FROM snap_streams WHERE business_day=?", (business_day,)).fetchall()
        pending = []
        for (device_id,) in pending_devices:
            stream = _load_stream(db, device_id, business_day)
            snaps = [{"snapshot_seq": s["meta"]["snapshot_seq"], "is_complete": s["meta"]["is_complete"],
                      "snapshot_time": s["meta"]["snapshot_time"],
                      "received_at": db.execute("SELECT received_at FROM snap_streams WHERE device_id=? AND business_day=? AND snapshot_seq=?",
                                                (device_id, business_day, s["meta"]["snapshot_seq"])).fetchone()[0],
                      "rows": s["rows"]} for s in stream]
            events = adjudicate(snaps, overrides=_overrides(db, device_id, business_day))
            pending.extend(e["event_id"] for e in events.values()
                           if e["current"]["card"] == card and e["state"] in {"stream_gap", "offline_pending"})
        if pending:
            raise DomainError("LEDGER_PENDING_EVENTS",
                              "存在未裁决的缺席事件 " + ",".join(sorted(pending)) + "，不得封存", 422)
        # 内容漂移检查：事实已变而旧版未重发时，禁止把过期日账封存
        candidate = None
        for (device_id,) in pending_devices:
            stream = _load_stream(db, device_id, business_day)
            if not any(any(r["card"] == card for r in s["rows"]) for s in stream):
                continue
            snaps = [{"snapshot_seq": s["meta"]["snapshot_seq"], "is_complete": s["meta"]["is_complete"],
                      "snapshot_time": s["meta"]["snapshot_time"],
                      "received_at": db.execute("SELECT received_at FROM snap_streams WHERE device_id=? AND business_day=? AND snapshot_seq=?",
                                                (device_id, business_day, s["meta"]["snapshot_seq"])).fetchone()[0],
                      "rows": s["rows"]} for s in stream]
            candidate = build_ledger(adjudicate(snaps, overrides=_overrides(db, device_id, business_day)),
                                     card, business_day)
            break
        if candidate is not None and _ledger_sha(candidate) != _ledger_sha(ledger):
            raise DomainError("LEDGER_STALE", "快照事实已变化，旧版日账已漂移，请重新发布后再封存", 409)
        if not ledger["metrics"]["balanced"]:
            raise DomainError("LEDGER_NOT_BALANCED", "账未对平（预期合计≠申报合计或分币不守恒），不得封存", 422)
        sealed_at = datetime.now(timezone.utc).isoformat()
        cursor = db.execute("UPDATE card_ledgers SET status='已封存',sealed_at=? WHERE card=? AND business_day=? AND version=? AND status='已发布'",
                            (sealed_at, card, business_day, version))
        if cursor.rowcount != 1:
            raise DomainError("LEDGER_STALE", "封存条件已变化，请刷新", 409)
    return get_ledger(card, business_day)


def decide_event(device_id, business_day, event_id, decision):
    if decision not in {"withdraw", "retain"}:
        raise DomainError("BAD_DECISION", "人工裁决仅支持 withdraw/retain")
    with connect() as db:
        if not db.execute("SELECT 1 FROM snap_streams WHERE device_id=? AND business_day=?",
                          (device_id, business_day)).fetchone():
            raise DomainError("NOT_FOUND", "快照流不存在", 404)
        db.execute("INSERT INTO event_overrides VALUES(?,?,?,?,?) "
                   "ON CONFLICT(device_id,business_day,event_id) DO UPDATE SET decision=excluded.decision,decided_at=excluded.decided_at",
                   (device_id, business_day, event_id, decision, datetime.now(timezone.utc).isoformat()))
    return stream_detail(device_id, business_day)

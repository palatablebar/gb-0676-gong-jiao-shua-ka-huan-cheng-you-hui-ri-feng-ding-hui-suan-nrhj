"""累计快照存储与 HTTP 端到端：幂等、版本发布、封存守卫、页面可见。"""
import pytest

from app.errors import DomainError
from app.snapshot import store
from app.snapshot.rules import SNAP_COLUMNS

HEADER = ",".join(SNAP_COLUMNS)


def snap_bytes(seq, rows, complete="true", device="D1", time="2026-11-16T09:00:00+08:00", mutate_meta=False):
    lines = [HEADER]
    if not rows:
        # 零事件快照：完整快照用纯元数据行承载；不完整快照直接给表头
        if complete == "true":
            lines.append(",".join([str(seq), device, time, complete, "", "", "", "", "", ""]))
        return ("\n".join(lines) + "\n").encode()
    for r in rows:
        lines.append(",".join([str(seq), device, time, complete, *r]))
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def client():
    from app.main import create_app
    return create_app().test_client()


@pytest.fixture(autouse=True)
def isolated_snapshot_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    yield


def _row(eid, card="C1", route="01", t="2026-11-16T08:00:00+08:00", reported="2.50", result="success"):
    return [eid, card, route, t, result, reported]


def test_retry_is_idempotent_no_double_share():
    raw = snap_bytes(1, [_row("E1"), _row("E2", route="02", t="2026-11-16T08:15:00+08:00", reported="1.50")])
    a = store.ingest(raw)
    b = store.ingest(raw)
    assert a["snapshot"]["content_sha"] == b["snapshot"]["content_sha"]
    assert a["snapshot"]["duplicated"] is False and b["snapshot"]["duplicated"] is True
    assert len(store.list_streams()) == 1
    detail = store.stream_detail("D1", "2026-11-16")
    assert len(detail["snapshots"]) == 1 and len(detail["events"]) == 2


def test_same_seq_conflicting_content_rejected_409():
    store.ingest(snap_bytes(1, [_row("E1", reported="2.50")]))
    with pytest.raises(DomainError) as e:
        store.ingest(snap_bytes(1, [_row("E1", reported="1.00")]))
    assert e.value.status == 409 and e.value.code == "SNAP_META_CONFLICT"
    # 旧值不被覆盖
    detail = store.stream_detail("D1", "2026-11-16")
    assert detail["events"][0]["current"]["reported"] == "2.50"


def test_out_of_order_seq_rejected():
    store.ingest(snap_bytes(2, [_row("E1")]))
    with pytest.raises(DomainError) as e:
        store.ingest(snap_bytes(1, [_row("E1")]))
    assert e.value.code == "SNAP_OUT_OF_ORDER"


def test_publish_creates_then_drifts_then_new_version_keeps_old():
    r1 = [_row("E1"), _row("E2", route="02", t="2026-11-16T08:15:00+08:00", reported="1.50")]
    store.ingest(snap_bytes(1, r1))
    pub = store.publish_ledger("D1", "2026-11-16", "C1")
    assert pub["action"] == "created" and pub["ledger"]["version"] == 1
    # 再次发布、候选未变 → 不产生新版本
    again = store.publish_ledger("D1", "2026-11-16", "C1")
    assert again["action"] == "unchanged"
    # 金额修正 → 漂移 → 追加 V2，V1 仍在版本历史
    store.ingest(snap_bytes(2, [_row("E1"), _row("E2", route="02", t="2026-11-16T08:15:00+08:00", reported="2.50")]))
    v2 = store.publish_ledger("D1", "2026-11-16", "C1")
    assert v2["action"] == "new_version" and v2["ledger"]["version"] == 2
    ledger = store.get_ledger("C1", "2026-11-16")
    assert [v["version"] for v in ledger["versions"]] == [1, 2]
    assert ledger["status"] == "已发布"


def test_seal_requires_balance_and_pending_clearance():
    # 申报金额与应扣不一致 → 未对平 → 封存拒绝
    store.ingest(snap_bytes(1, [_row("E1", reported="9.00")]))
    store.publish_ledger("D1", "2026-11-16", "C1")
    with pytest.raises(DomainError) as e:
        store.seal_ledger("C1", "2026-11-16", 1)
    assert e.value.code == "LEDGER_NOT_BALANCED"


def test_seal_blocked_by_pending_absence_then_manual_decision_unblocks():
    store.ingest(snap_bytes(1, [_row("E1")]))
    store.ingest(snap_bytes(2, []))  # E1 掉线挂起
    store.publish_ledger("D1", "2026-11-16", "C1")
    with pytest.raises(DomainError) as e:
        store.seal_ledger("C1", "2026-11-16", 1)
    assert e.value.code == "LEDGER_PENDING_EVENTS"
    store.decide_event("D1", "2026-11-16", "E1", "withdraw")
    # 撤回后候选变了：需重新发布 V2，旧 V1 不能被悄悄替换
    with pytest.raises(DomainError) as e:
        store.seal_ledger("C1", "2026-11-16", 1)
    assert e.value.code == "LEDGER_STALE"
    pub = store.publish_ledger("D1", "2026-11-16", "C1")
    assert pub["action"] == "new_version"
    sealed = store.seal_ledger("C1", "2026-11-16", 2)
    assert sealed["status"] == "已封存" and sealed["sealed_at"]


def test_sealed_ledger_is_immutable():
    store.ingest(snap_bytes(1, [_row("E1")]))
    store.publish_ledger("D1", "2026-11-16", "C1")
    store.seal_ledger("C1", "2026-11-16", 1)
    store.ingest(snap_bytes(2, [_row("E1"), _row("E2", route="02", t="2026-11-16T08:20:00+08:00", reported="1.50")]))
    with pytest.raises(DomainError) as e:
        store.publish_ledger("D1", "2026-11-16", "C1")
    assert e.value.code == "LEDGER_SEALED"


def test_seal_optimistic_lock_stale_version():
    store.ingest(snap_bytes(1, [_row("E1")]))
    store.publish_ledger("D1", "2026-11-16", "C1")
    with pytest.raises(DomainError) as e:
        store.seal_ledger("C1", "2026-11-16", 99)
    assert e.value.code == "LEDGER_STALE"


def test_quarantine_rows_persisted_per_snapshot():
    raw = snap_bytes(1, [_row("E1"), _row("E2", route="99")])
    store.ingest(raw)
    detail = store.stream_detail("D1", "2026-11-16")
    assert [q["reason"] for q in detail["quarantine"]] == ["UNKNOWN_ROUTE"]
    assert len(detail["events"]) == 1


def test_api_flow_and_pages(client):
    raw = snap_bytes(1, [_row("E1"), _row("E2", route="02", t="2026-11-16T08:15:00+08:00", reported="1.50")])
    r = client.post("/api/snapshots/ingest-text", json={"csv": raw.decode()})
    assert r.status_code == 200 and r.get_json()["snapshot"]["duplicated"] is False
    assert client.get("/api/snapshots/streams").get_json()["streams"][0]["device_id"] == "D1"
    detail = client.get("/api/snapshots/D1/2026-11-16")
    assert detail.status_code == 200 and len(detail.get_json()["events"]) == 2
    pub = client.post("/api/ledgers/D1/2026-11-16/C1/publish")
    assert pub.status_code == 200 and pub.get_json()["ledger"]["version"] == 1
    got = client.get("/api/ledgers/C1/2026-11-16")
    assert got.get_json()["metrics"]["balanced"] is True
    # 页面可打开
    assert client.get("/snapshots").status_code == 200
    assert client.get("/ledgers").status_code == 200


def test_api_manual_decision_and_404s(client):
    store.ingest(snap_bytes(1, [_row("E1")]))
    r = client.post("/api/snapshots/D1/2026-11-16/events/E1/decision", json={"decision": "withdraw"})
    assert r.status_code == 200
    assert client.post("/api/snapshots/D1/2026-11-16/events/E1/decision", json={"decision": "nope"}).status_code == 422
    assert client.get("/api/snapshots/NOPE/2026-11-16").status_code == 404
    assert client.get("/api/ledgers/C9/2026-11-16").status_code == 404


def test_publish_failure_keeps_old_ledger_intact(client, monkeypatch):
    store.ingest(snap_bytes(1, [_row("E1")]))
    store.publish_ledger("D1", "2026-11-16", "C1")
    before = store.get_ledger("C1", "2026-11-16")
    # 让追加版本后的读取环节抛错，模拟发布中断：事务回滚，旧版仍唯一可用
    original = store._read_latest

    def boom(*a, **k):
        raise RuntimeError("publish interrupted")
    monkeypatch.setattr(store, "_read_latest", boom)
    store.ingest(snap_bytes(2, [_row("E1", reported="2.00")]))
    with pytest.raises(RuntimeError):
        store.publish_ledger("D1", "2026-11-16", "C1")
    monkeypatch.setattr(store, "_read_latest", original)
    after = store.get_ledger("C1", "2026-11-16")
    assert after["version"] == before["version"] == 1
    assert after["metrics"] == before["metrics"]

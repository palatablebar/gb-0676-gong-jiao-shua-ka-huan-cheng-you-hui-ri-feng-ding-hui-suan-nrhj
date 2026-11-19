"""累计快照日账的 HTTP 入口。"""
from flask import Blueprint, jsonify, request

from app.errors import DomainError
from app.snapshot import store

bp = Blueprint("snapshot", __name__)


@bp.get("/api/snapshots/streams")
def streams():
    return jsonify({"streams": store.list_streams()})


@bp.post("/api/snapshots/ingest")
def ingest():
    file = request.files.get("file")
    if not file:
        raise DomainError("NO_FILE", "请选择累计快照 CSV 文件")
    return jsonify(store.ingest(file.read()))


@bp.post("/api/snapshots/ingest-text")
def ingest_text():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("csv"), str):
        raise DomainError("BAD_BODY", "请求需要包含 csv 文本")
    return jsonify(store.ingest(payload["csv"].encode()))


@bp.get("/api/snapshots/<device_id>/<business_day>")
def detail(device_id, business_day):
    return jsonify(store.stream_detail(device_id, business_day))


@bp.post("/api/snapshots/<device_id>/<business_day>/events/<event_id>/decision")
def decide(device_id, business_day, event_id):
    payload = request.get_json(silent=True) or {}
    if payload.get("decision") not in {"withdraw", "retain"}:
        raise DomainError("BAD_DECISION", "decision 必须是 withdraw 或 retain")
    return jsonify(store.decide_event(device_id, business_day, event_id, payload["decision"]))


@bp.get("/api/ledgers/<card>/<business_day>")
def ledger(card, business_day):
    return jsonify(store.get_ledger(card, business_day))


@bp.post("/api/ledgers/<device_id>/<business_day>/<card>/publish")
def publish(device_id, business_day, card):
    return jsonify(store.publish_ledger(device_id, business_day, card))


@bp.post("/api/ledgers/<card>/<business_day>/seal")
def seal(card, business_day):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("version"), int):
        raise DomainError("BAD_VERSION", "请提供要封存的版本号")
    return jsonify(store.seal_ledger(card, business_day, payload["version"]))

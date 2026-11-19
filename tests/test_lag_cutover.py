"""滞后切断失败测 / 验收测。

顺序要求：
1. 先复现「策略改口后未结到达仍按旧档计分」——本文件首测在旧实现上必须红。
2. 翻档失败测（注入重评分异常）必须红。
3. 双席抢改失败测必须红，且缺「时限滞后争用」留痕即红。
4. 实现后同一批测试转绿，五处（源名录/到达卡片/策略列表/水位牌/历史附页）跟新档。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app as backend
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def login(c: TestClient) -> dict:
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def post_arrival(c, h, source, lag_minutes, expected=None, seat="补采席"):
    base = datetime(2026, 11, 19, 8, 0, 0)
    ingest = base + timedelta(minutes=lag_minutes)
    body = {
        "source": source,
        "source_clock": base.isoformat(timespec="seconds"),
        "ingest_clock": ingest.isoformat(timespec="seconds"),
        "seat": seat,
    }
    if expected is not None:
        body["expected_config_version"] = expected
    return c.post("/api/arrivals", headers=h, json=body)


def patch_source(c, h, source, expected=None, seat="配置席", **fields):
    body = {"seat": seat}
    if expected is not None:
        body["expected_config_version"] = expected
    body.update(fields)
    return c.patch(f"/api/sources/{source}", headers=h, json=body)


def source_row(c, h, name):
    return next(x for x in c.get("/api/sources", headers=h).json()["items"] if x["name"] == name)


def events_of(c, h, kind):
    return [e for e in c.get("/api/lag/events", headers=h).json()["items"] if e["kind"] == kind]


def test_policy_change_rescores_unsettled_and_aligns_five_surfaces():
    c = TestClient(app)
    h = login(c)

    src = source_row(c, h, "订单源")
    assert src["sla_minutes"] == 15
    assert src["policy_version"] == 1
    assert src["config_version"] == 1

    a = post_arrival(c, h, "订单源", 20, expected=1)
    assert a.status_code == 200, a.text
    assert a.json()["level"] == "告警"
    assert a.json()["output_mode"] == "降级只读"

    # 策略改口：15 -> 30，20 分滞后应从「告警」翻成「正常」
    r = patch_source(c, h, "订单源", expected=1, sla_minutes=30)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["policy_version"] == 2
    assert j["resecored"] == 1
    assert j["path"] == "甲"

    # 一、源名录
    s = source_row(c, h, "订单源")
    assert (s["sla_minutes"], s["policy_version"], s["config_version"]) == (30, 2, 2)
    assert s["output_mode"] == "可写"

    # 二、到达卡片
    arr = [x for x in c.get("/api/arrivals", headers=h).json()["items"] if x["source"] == "订单源"]
    assert len(arr) == 1
    card = arr[0]
    assert card["level"] == "正常"
    assert card["policy_version"] == 2
    assert card["scored_sla"] == 30
    assert card["settled"] == 0

    # 三、策略列表
    pol = c.get("/api/policies", headers=h, params={"source": "订单源"}).json()["items"]
    assert [p["version"] for p in pol] == [1, 2]
    assert pol[-1]["sla_minutes"] == 30
    assert pol[-1]["status"] == "sealed"

    # 四、水位牌
    wm = c.get("/api/watermark", headers=h).json()
    row = next(x for x in wm["items"] if x["source"] == "订单源")
    assert row["mode"] == "可写"
    assert (row["normal"], row["alert"], row["breach"], row["settled"]) == (1, 0, 0, 0)

    # 五、历史附页（未结到达不应进入历史册）
    assert c.get("/api/history", headers=h).json()["items"] == []

    cards = c.get("/api/cutover-cards", headers=h, params={"source": "订单源"}).json()["items"]
    assert len(cards) == 1
    assert cards[0]["path"] == "甲"
    assert cards[0]["status"] == "sealed"
    assert (cards[0]["from_version"], cards[0]["to_version"]) == (1, 2)


def test_settled_arrivals_keep_old_tier_in_history_appendix():
    c = TestClient(app)
    h = login(c)

    first = post_arrival(c, h, "订单源", 20, expected=1)
    assert first.status_code == 200
    first_id = first.json()["id"]

    assert c.post("/api/catchup/订单源", headers=h).status_code == 200
    second = post_arrival(c, h, "订单源", 20, expected=1)
    assert second.status_code == 200
    second_id = second.json()["id"]

    r = patch_source(c, h, "订单源", expected=1, sla_minutes=30)
    assert r.status_code == 200, r.text
    assert r.json()["resecored"] == 1  # 只翻未结那条

    cards = {x["id"]: x for x in c.get("/api/arrivals", headers=h).json()["items"]}
    assert cards[first_id]["level"] == "告警"
    assert cards[first_id]["policy_version"] == 1
    assert cards[first_id]["scored_sla"] == 15
    assert cards[first_id]["settled"] == 1
    assert cards[second_id]["level"] == "正常"
    assert cards[second_id]["policy_version"] == 2
    assert cards[second_id]["settled"] == 0

    hist = c.get("/api/history", headers=h, params={"source": "订单源"}).json()["items"]
    assert [x["id"] for x in hist] == [first_id]
    assert hist[0]["level"] == "告警"
    assert hist[0]["policy_version"] == 1

    wm = next(x for x in c.get("/api/watermark", headers=h).json()["items"] if x["source"] == "订单源")
    assert wm["settled"] == 1
    assert wm["normal"] == 1
    assert wm["alert"] == 0


def test_cutover_failure_rolls_back_policy_and_scores(monkeypatch):
    c = TestClient(app)
    h = login(c)

    a = post_arrival(c, h, "订单源", 20, expected=1)
    assert a.status_code == 200 and a.json()["level"] == "告警"

    def boom(*args, **kwargs):
        raise RuntimeError("injected cutover failure")

    monkeypatch.setattr(backend, "_rescore_pending", boom)

    r = patch_source(c, h, "订单源", expected=1, sla_minutes=30)
    assert r.status_code == 500, r.text

    # 正式档位与未结计分都必须保持旧值
    s = source_row(c, h, "订单源")
    assert (s["sla_minutes"], s["policy_version"], s["config_version"]) == (15, 1, 1)
    assert s["output_mode"] == "降级只读"

    arr = [x for x in c.get("/api/arrivals", headers=h).json()["items"] if x["source"] == "订单源"]
    assert arr[0]["level"] == "告警"
    assert arr[0]["policy_version"] == 1

    assert c.get("/api/policies", headers=h, params={"source": "订单源"}).json()["items"] == [
        p for p in c.get("/api/policies", headers=h, params={"source": "订单源"}).json()["items"]
        if p["version"] == 1
    ]
    assert c.get("/api/cutover-cards", headers=h, params={"source": "订单源"}).json()["items"] == []
    assert events_of(c, h, "翻档失败"), "翻档失败必须留痕"


def test_dual_seat_race_records_lag_contention():
    c = TestClient(app)
    h = login(c)

    # 配置席先交，成功翻档
    assert patch_source(c, h, "订单源", expected=1, sla_minutes=30).status_code == 200

    # 补采席后交，持旧滞后锁令牌，只记争用，不得落采集配置
    late = patch_source(
        c, h, "订单源", expected=1, seat="补采席", collect_window_minutes=90
    )
    assert late.status_code == 409, late.text
    s = source_row(c, h, "订单源")
    assert s["collect_window_minutes"] == 60
    assert s["sla_minutes"] == 30

    # 补采席后交到达，同样只记争用，不得落到达
    before = len(c.get("/api/arrivals", headers=h).json()["items"])
    late_arrival = post_arrival(c, h, "订单源", 20, expected=1, seat="补采席")
    assert late_arrival.status_code == 409, late_arrival.text
    assert len(c.get("/api/arrivals", headers=h).json()["items"]) == before

    traces = events_of(c, h, "时限滞后争用")
    assert len(traces) >= 2, "缺滞后争用留痕即红"
    assert all(t["seat"] == "补采席" for t in traces)


def test_dual_seat_race_without_token_still_detected():
    """未显式带版本号的后交同样必须被滞后锁识别，不能静默落库。"""
    c = TestClient(app)
    h = login(c)

    assert patch_source(c, h, "订单源", expected=1, sla_minutes=30).status_code == 200

    late = c.patch("/api/sources/订单源", headers=h,
                   json={"seat": "补采席", "collect_window_minutes": 90})
    assert late.status_code == 409, late.text
    s = source_row(c, h, "订单源")
    assert s["collect_window_minutes"] == 60 and s["sla_minutes"] == 30
    assert events_of(c, h, "时限滞后争用")


def test_deviating_from_cutover_path_is_blocked_and_logged():
    c = TestClient(app)
    h = login(c)

    # 甲/乙不得混用：本仓已封甲路径，乙路径必须拦下
    r = patch_source(c, h, "订单源", expected=1, sla_minutes=30, path="乙")
    assert r.status_code == 409, r.text
    assert source_row(c, h, "订单源")["sla_minutes"] == 15

    # 只改显示 / 不改计分必须拦下
    r2 = patch_source(c, h, "订单源", expected=1, sla_minutes=30, display_only=True)
    assert r2.status_code == 409, r2.text
    r3 = patch_source(c, h, "订单源", expected=1, sla_minutes=30, rescore=False)
    assert r3.status_code == 409, r3.text

    assert len(events_of(c, h, "滞后偏了")) == 3
    assert source_row(c, h, "订单源")["sla_minutes"] == 15


def test_sealed_cutover_card_is_immutable_on_same_day():
    c = TestClient(app)
    h = login(c)

    assert patch_source(c, h, "订单源", expected=1, sla_minutes=30).status_code == 200
    first = c.get("/api/cutover-cards", headers=h, params={"source": "订单源"}).json()["items"]
    assert len(first) == 1
    first_snapshot = dict(first[0])

    # 同日再次改口，只能追加新卡，旧卡封档不得改口
    assert patch_source(c, h, "订单源", expected=2, sla_minutes=40).status_code == 200
    cards = c.get("/api/cutover-cards", headers=h, params={"source": "订单源"}).json()["items"]
    assert len(cards) == 2
    old = next(x for x in cards if x["id"] == first_snapshot["id"])
    assert old == first_snapshot
    new = next(x for x in cards if x["id"] != first_snapshot["id"])
    assert (new["from_version"], new["to_version"]) == (2, 3)

    # 没有改口切断卡的更新入口
    assert c.put(f"/api/cutover-cards/{first_snapshot['id']}", headers=h, json={}).status_code == 405


def test_collect_config_change_is_accepted_without_touching_policy_tier():
    c = TestClient(app)
    h = login(c)

    r = patch_source(
        c, h, "订单源", expected=1, seat="补采席", collect_window_minutes=90
    )
    assert r.status_code == 200, r.text
    s = source_row(c, h, "订单源")
    assert s["collect_window_minutes"] == 90
    assert s["sla_minutes"] == 15
    assert s["policy_version"] == 1
    assert s["config_version"] == 2
    assert c.get("/api/policies", headers=h, params={"source": "订单源"}).json()["items"][-1]["version"] == 1

"""测量登记与基于可能位置的井归属：端到端测试。

覆盖：
- 测量登记/订正（UPSERT）、矩形校验（有限、正面积）、未知井/重复编号；
- 共同范围交集为面/线段/单点时的归属（assigned/pending/uncovered）；
- 交集为空拒绝并返回一对互不相交的测量编号，原版保持不变；
- 未复测井继续用登记点；登记点在所有版本响应中保留；
- 整批改图沿用基准版测量、按新边界重算归属；
- 测量与归属同版发布、历史版本快照、同版并发只接受一份、崩溃原子性；
- 查询返回所查版本的可能位置与归属。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db


def square(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def rect(minx, miny, maxx, maxy):
    return {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}


REGION = square(0, 0, 100, 100)
P1 = square(0, 0, 50, 100)
P2 = square(50, 0, 100, 100)

# 井均放整数点，便于构造误差矩形
WELLS = [
    {"id": "W1", "geometry": point(25, 50)},     # P1 内部
    {"id": "W2", "geometry": point(75, 50)},     # P2 内部
    {"id": "W3", "geometry": point(50, 50)},     # 共边上
    {"id": "W4", "geometry": point(150, 50)},    # 区域外
]


def init_ok(client):
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P2", "geometry": P2}],
        "wells": WELLS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def wells_by_id(payload):
    return {w["id"]: w for w in payload["wells"]}


def post_measurements(client, base_revision, measurements):
    return client.post("/v1/measurements",
                       json={"base_revision": base_revision, "measurements": measurements})


def move_boundary_to(x):
    return [{"id": "P1", "geometry": square(0, 0, x, 100)},
            {"id": "P2", "geometry": square(x, 0, 100, 100)}]


# ---------------------------------------------------------------- 未复测基线

def test_unmeasured_wells_keep_registered_point(env):
    client, _ = env
    init_ok(client)
    data = client.get("/v1/wells").json()
    assert data["revision"] == 1
    for w in data["wells"]:
        assert w["measured"] is False
        assert w["measurement_ids"] == []
        # 可能位置即登记点
        assert w["possible_position"] == w["geometry"] == w["registered_point"]
    wells = wells_by_id(data)
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"
    assert wells["W3"]["status"] == "pending" and wells["W3"]["candidates"] == ["P1", "P2"]
    assert wells["W4"]["status"] == "uncovered"


# ---------------------------------------------------------------- 登记与归属

def test_register_measurement_assigned_when_fully_inside(env):
    """整个误差矩形都在 P1 内部 → assigned。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1",
        "rectangle": rect(20, 40, 30, 60),
    }])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["registered"] == [{"well_id": "W1", "measurement_id": "M1"}]
    assert body["measurement_count"] == 1

    wells = wells_by_id(client.get("/v1/wells").json())
    w1 = wells["W1"]
    assert w1["status"] == "assigned" and w1["parcel_id"] == "P1"
    assert w1["measured"] is True and w1["measurement_ids"] == ["M1"]
    assert w1["possible_position"]["type"] == "Polygon"
    # 登记点保留且不变
    assert w1["registered_point"] == point(25, 50) == w1["geometry"]
    # 其他井未复测，仍用登记点
    assert wells["W2"]["measured"] is False


def test_rectangle_touching_boundary_is_pending(env):
    """矩形碰到共边 x=50 但整体不在唯一宗地内部 → pending，候选列出相交宗地。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1",
        "rectangle": rect(40, 40, 60, 60),     # 横跨 x=50
    }])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "pending"
    assert wells["W1"]["parcel_id"] is None
    assert wells["W1"]["candidates"] == ["P1", "P2"]


def test_rectangle_within_parcel_but_sharing_edge_is_assigned(env):
    """矩形整体位于 P1 一侧、仅一条边与共边重合：内部全在 P1 内 → assigned P1。

    （归属只确认"整个可能位置是否都在同一宗地内部"：该矩形内部完全属于 P1，
    与 P2 仅共边、无内部重叠，因此归属 P1 是确定的。）
    """
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1",
        "rectangle": rect(40, 40, 50, 60),     # maxx == 50 与共边重合
    }])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"


def test_rectangle_straddling_two_parcels_is_pending(env):
    """矩形内部同时伸进两块宗地 → 无法确认唯一归属 → pending，候选为两块。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1",
        "rectangle": rect(40, 40, 50 + 1, 60),   # 内部越过共边
    }])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "pending"
    assert wells["W1"]["candidates"] == ["P1", "P2"]


def test_rectangle_outside_all_parcels_is_uncovered(env):
    """可能位置与所有宗地都不相交 → uncovered。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [{
        "well_id": "W4", "measurement_id": "M1",
        "rectangle": rect(140, 40, 160, 60),   # 完全在区域外
    }])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W4"]["status"] == "uncovered"
    assert wells["W4"]["candidates"] == []


def test_intersection_of_two_rectangles_is_possible_position(env):
    """同井两次测量：可能位置是两个矩形的交集，而非登记点。"""
    client, _ = env
    init_ok(client)
    # W3 登记点在共边上；两次测量的交集完全落在 P2 内部 → assigned P2
    r = post_measurements(client, 1, [
        {"well_id": "W3", "measurement_id": "M1", "rectangle": rect(50, 40, 70, 60)},
        {"well_id": "W3", "measurement_id": "M2", "rectangle": rect(55, 45, 65, 55)},
    ])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    w3 = wells["W3"]
    assert w3["status"] == "assigned" and w3["parcel_id"] == "P2"
    # 交集 bbox = (55,45)-(65,55)
    coords = w3["possible_position"]["coordinates"][0]
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    assert min(xs) == 55 and max(xs) == 65 and min(ys) == 45 and max(ys) == 55
    assert w3["measurement_ids"] == ["M1", "M2"]


def test_line_intersection_is_valid_and_pending(env):
    """两次测量交集退化为线段（共边 x=50 上的竖线）→ 有效，pending。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(40, 40, 50, 60)},
        {"well_id": "W1", "measurement_id": "M2", "rectangle": rect(50, 40, 60, 60)},
    ])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    pos = wells["W1"]["possible_position"]
    assert pos["type"] == "LineString"
    assert pos["coordinates"] == [[50, 40], [50, 60]]
    assert wells["W1"]["status"] == "pending"
    assert wells["W1"]["candidates"] == ["P1", "P2"]


def test_line_intersection_inside_parcel_is_assigned(env):
    """交集退化为线段但整条线段都在 P1 内部 → assigned（线段/单点同样能确认归属）。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 25, 60)},
        {"well_id": "W1", "measurement_id": "M2", "rectangle": rect(25, 45, 30, 55)},
    ])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    pos = wells["W1"]["possible_position"]
    assert pos["type"] == "LineString"
    assert pos["coordinates"] == [[25, 45], [25, 55]]
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"


def test_point_intersection_is_valid_and_assigned(env):
    """两次测量交集退化为单点且在宗地内部 → assigned。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 25, 50)},
        {"well_id": "W1", "measurement_id": "M2", "rectangle": rect(25, 50, 30, 60)},
    ])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    pos = wells["W1"]["possible_position"]
    assert pos["type"] == "Point" and pos["coordinates"] == [25, 50]
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"


def test_point_intersection_on_boundary_is_pending(env):
    """单点交集落在共边上 → pending，两块宗地都是候选。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(40, 40, 50, 50)},
        {"well_id": "W1", "measurement_id": "M2", "rectangle": rect(50, 50, 60, 60)},
    ])
    assert r.status_code == 200, r.text
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["possible_position"]["type"] == "Point"
    assert wells["W1"]["possible_position"]["coordinates"] == [50, 50]
    assert wells["W1"]["status"] == "pending"
    assert wells["W1"]["candidates"] == ["P1", "P2"]


# ---------------------------------------------------------------- 订正（UPSERT）

def test_correction_same_well_same_measurement_id_overwrites(env):
    client, _ = env
    init_ok(client)
    # 首版测量横跨共边 → pending
    assert post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1", "rectangle": rect(40, 40, 60, 60),
    }]).status_code == 200
    # 同井同编号订正为完全在 P1 内部 → assigned，测量总数仍为 1
    r = post_measurements(client, 2, [{
        "well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60),
    }])
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3
    assert r.json()["measurement_count"] == 1
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"
    assert wells["W1"]["measurement_ids"] == ["M1"]

    meas = client.get("/v1/measurements").json()
    assert meas["revision"] == 3
    assert len(meas["measurements"]) == 1
    assert meas["measurements"][0]["well_id"] == "W1"
    assert meas["measurements"][0]["measurement_id"] == "M1"


def test_correction_can_fix_empty_intersection(env):
    """先造成空交集被拒绝；订正其中一个编号后交集恢复，登记成功。"""
    client, _ = env
    init_ok(client)
    # 第一次登记 M1
    assert post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1", "rectangle": rect(0, 40, 10, 60),
    }]).status_code == 200
    # M2 与 M1 完全不相交 → 拒绝
    r = post_measurements(client, 2, [{
        "well_id": "W1", "measurement_id": "M2", "rectangle": rect(40, 40, 48, 60),
    }])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "empty_intersection"
    assert detail["well_id"] == "W1"
    assert set(detail["disjoint_measurement_ids"]) == {"M1", "M2"}
    # 原版保持不变（仍只有 M1，修订号停在 2）
    assert client.get("/v1/wells").json()["revision"] == 2
    meas = client.get("/v1/measurements").json()
    assert [m["measurement_id"] for m in meas["measurements"]] == ["M1"]

    # 订正 M2 为与 M1 相交的矩形 → 成功
    r = post_measurements(client, 2, [{
        "well_id": "W1", "measurement_id": "M2", "rectangle": rect(5, 45, 15, 55),
    }])
    assert r.status_code == 200, r.text
    assert r.json()["measurement_count"] == 2
    wells = wells_by_id(client.get("/v1/wells").json())
    # 交集 (5,45)-(10,55) 全在 P1 内部
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"


def test_empty_intersection_within_single_request(env):
    """同一请求里同井两个互不相交矩形 → 422 并返回这对编号，不发布新版本。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W2", "measurement_id": "A", "rectangle": rect(70, 40, 80, 60)},
        {"well_id": "W2", "measurement_id": "B", "rectangle": rect(90, 40, 95, 60)},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "empty_intersection"
    assert set(detail["disjoint_measurement_ids"]) == {"A", "B"}
    assert client.get("/v1/current").json()["revision"] == 1


# ---------------------------------------------------------------- 校验错误

def test_register_unknown_well_rejected(env):
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [{
        "well_id": "NOPE", "measurement_id": "M1", "rectangle": rect(0, 0, 1, 1),
    }])
    assert r.status_code == 422
    assert any(x["code"] == "unknown_well_id" for x in r.json()["detail"]["reasons"])


def test_register_duplicate_measurement_in_request(env):
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)},
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(21, 41, 29, 59)},
    ])
    assert r.status_code == 422
    assert any(x["code"] == "duplicate_measurement_id"
               for x in r.json()["detail"]["reasons"])
    assert client.get("/v1/current").json()["revision"] == 1


def test_register_invalid_rectangle_rules(env):
    client, _ = env
    init_ok(client)
    bad_rects = [
        rect(1, 0, 1, 1),       # 零宽
        rect(0, 2, 1, 2),       # 零高
        rect(5, 5, 4, 6),       # 上界小于下界
    ]
    for bad in bad_rects:
        r = post_measurements(client, 1, [
            {"well_id": "W1", "measurement_id": "M1", "rectangle": bad}])
        assert r.status_code == 422, (bad, r.text)
        assert any(x["code"] == "invalid_rectangle"
                   for x in r.json()["detail"]["reasons"])


def test_register_non_finite_coordinates_rejected(env):
    client, _ = env
    init_ok(client)
    for token in ("Infinity", "-Infinity", "NaN"):
        r = client.post("/v1/measurements", content=(
            '{"base_revision":1,"measurements":[{"well_id":"W1","measurement_id":"M1",'
            f'"rectangle":{{"minx":0,"miny":0,"maxx":{token},"maxy":1}}}}}}'
        ), headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (token, r.status_code, r.text)


def test_register_before_init_404(env):
    client, _ = env
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(0, 0, 1, 1)}])
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_initialized"


def test_register_stale_base_revision_409(env):
    client, _ = env
    init_ok(client)
    # 先用整批改图推进到 v2
    assert client.put("/v1/parcels", json={
        "base_revision": 1, "parcels": move_boundary_to(60)}).status_code == 200
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)}])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict" and detail["current_revision"] == 2


# ---------------------------------------------------------------- 与改图的交互

def test_parcel_replace_keeps_measurements_and_recomputes(env):
    """常规改图沿用基准版测量，按新边界重算归属。"""
    client, _ = env
    init_ok(client)
    # W1 测量矩形 (20,40)-(70,60)：横跨 x=50 → v2 pending(P1,P2)
    assert post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 70, 60),
    }]).status_code == 200
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "pending"

    # 共边移到 x=80：矩形 (20..70) 整体落在新 P1（0..80）内部 → assigned P1
    r = client.put("/v1/parcels", json={
        "base_revision": 2, "parcels": move_boundary_to(80)})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"
    # 测量沿用：测量编号与可能位置不变
    assert wells["W1"]["measurement_ids"] == ["M1"]
    meas = client.get("/v1/measurements").json()
    assert meas["revision"] == 3 and len(meas["measurements"]) == 1

    # 未复测井 W3 仍按登记点（共边现在是 x=80，W3 在 x=50 → P1 内部）
    assert wells["W3"]["status"] == "assigned" and wells["W3"]["parcel_id"] == "P1"


def test_measurements_and_attributions_published_in_same_revision(env):
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)}])
    new_rev = r.json()["revision"]
    current = client.get("/v1/current").json()
    # 同一响应里测量后的归属与边界同版
    assert current["revision"] == new_rev
    w1 = wells_by_id(current)["W1"]
    assert w1["measured"] is True and w1["status"] == "assigned"
    assert client.get("/v1/wells").json()["revision"] == new_rev
    assert client.get("/v1/measurements").json()["revision"] == new_rev


def test_historical_revision_keeps_measurement_snapshot(env):
    """历史版本保留当时的测量与可能位置。"""
    client, _ = env
    init_ok(client)
    # v2：W1 跨边矩形 → pending
    post_measurements(client, 1, [{
        "well_id": "W1", "measurement_id": "M1", "rectangle": rect(40, 40, 60, 60)}])
    # v3：订正为 P1 内部 → assigned
    post_measurements(client, 2, [{
        "well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)}])

    v2 = client.get("/v1/revisions/2").json()
    w1_v2 = wells_by_id(v2)["W1"]
    assert w1_v2["status"] == "pending"
    assert w1_v2["measurement_ids"] == ["M1"]
    assert w1_v2["possible_position"]["type"] == "Polygon"
    assert w1_v2["registered_point"] == point(25, 50)

    v1 = client.get("/v1/revisions/1").json()
    w1_v1 = wells_by_id(v1)["W1"]
    assert w1_v1["measured"] is False
    assert w1_v1["possible_position"] == point(25, 50)

    v3 = client.get("/v1/revisions/3").json()
    assert wells_by_id(v3)["W1"]["status"] == "assigned"


# ---------------------------------------------------------------- 并发与原子性

def test_concurrent_measurements_same_base_only_one_wins(env):
    """同版并发测量登记：恰好一份成功，另一份 409。"""
    client, _ = env
    init_ok(client)
    results = []

    payload1 = {"base_revision": 1, "measurements": [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)}]}
    payload2 = {"base_revision": 1, "measurements": [
        {"well_id": "W2", "measurement_id": "M1", "rectangle": rect(70, 40, 80, 60)}]}

    with TestClient(app) as c1, TestClient(app) as c2:
        barrier = threading.Barrier(2)

        def run(c, payload):
            barrier.wait()
            results.append(c.post("/v1/measurements", json=payload).status_code)

        t1 = threading.Thread(target=run, args=(c1, payload1))
        t2 = threading.Thread(target=run, args=(c2, payload2))
        t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409], results
    assert client.get("/v1/current").json()["revision"] == 2
    rows = client.get("/v1/revisions").json()["revisions"]
    assert [r["revision"] for r in rows] == [1, 2]


def test_concurrent_measurement_and_parcel_replace_only_one_wins(env):
    """测量登记与整批改图基于同一旧版并发：只接受一份。"""
    client, _ = env
    init_ok(client)
    results = []

    with TestClient(app) as c1, TestClient(app) as c2:
        barrier = threading.Barrier(2)

        def run_measurements(c):
            barrier.wait()
            r = post_measurements(c, 1, [
                {"well_id": "W1", "measurement_id": "M1",
                 "rectangle": rect(20, 40, 30, 60)}])
            results.append(("measurement", r.status_code))

        def run_replace(c):
            barrier.wait()
            r = c.put("/v1/parcels", json={
                "base_revision": 1, "parcels": move_boundary_to(60)})
            results.append(("replace", r.status_code))

        t1 = threading.Thread(target=run_measurements, args=(c1,))
        t2 = threading.Thread(target=run_replace, args=(c2,))
        t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(code for _, code in results)
    assert statuses == [200, 409], results
    assert client.get("/v1/current").json()["revision"] == 2


def test_failed_measurement_commit_leaves_no_partial_version(env, monkeypatch):
    """测量登记提交中途崩溃：回滚干净，测量与版本指针都不变。"""
    client, db = env
    init_ok(client)

    import app.main as main_mod
    original = main_mod._insert_version

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-commit")

    monkeypatch.setattr(main_mod, "_insert_version", boom)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)}])
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "_insert_version", original)

    assert client.get("/v1/current").json()["revision"] == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
        assert conn.execute("SELECT current_revision FROM meta").fetchone()[0] == 1
    finally:
        conn.close()

    # 恢复后服务照常可用
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)}])
    assert r.status_code == 200 and r.json()["revision"] == 2


def test_restart_keeps_measurement_snapshot(env):
    client, db = env
    init_ok(client)
    assert post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)},
    ]).status_code == 200

    with TestClient(app) as client2:
        current = client2.get("/v1/current").json()
        assert current["revision"] == 2
        w1 = wells_by_id(current)["W1"]
        assert w1["measured"] is True and w1["status"] == "assigned"
        meas = client2.get("/v1/measurements").json()
        assert len(meas["measurements"]) == 1
        rows = client2.get("/v1/revisions").json()["revisions"]
        assert [r["revision"] for r in rows] == [1, 2]


def test_batch_multiple_wells_measurements_together(env):
    """一次请求登记多口井的测量，各自独立求交与归属。"""
    client, _ = env
    init_ok(client)
    r = post_measurements(client, 1, [
        {"well_id": "W1", "measurement_id": "M1", "rectangle": rect(20, 40, 30, 60)},
        {"well_id": "W2", "measurement_id": "M1", "rectangle": rect(70, 40, 80, 60)},
        {"well_id": "W4", "measurement_id": "M1", "rectangle": rect(140, 40, 160, 60)},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["measurement_count"] == 3
    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W1"]["status"] == "assigned" and wells["W1"]["parcel_id"] == "P1"
    assert wells["W2"]["status"] == "assigned" and wells["W2"]["parcel_id"] == "P2"
    assert wells["W4"]["status"] == "uncovered"
    # W3 未复测
    assert wells["W3"]["measured"] is False
    assert wells["W3"]["status"] == "pending"   # 登记点在共边上

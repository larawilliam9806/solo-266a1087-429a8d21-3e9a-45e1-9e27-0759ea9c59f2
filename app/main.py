"""FastAPI 入口：版本化发布、整批替换、同版一致性查询。

并发与原子性：
- 重活（几何核查、井归属重算）在写事务之外基于 base 版本快照完成；
- 写事务用 BEGIN IMMEDIATE 串行化，进入后先核对 meta.current_revision
  仍等于 base_revision，不等则回滚返回 409 —— 同版并发修订只成功一份；
- 版本行、宗地行、井归属行、版本指针在同一个事务里提交，失败/重启不留半版。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from shapely.geometry import shape as shapely_shape

from .db import connect, init_db
from .geo import (
    Rectangle,
    attribute_locations,
    bbox_to_geometry,
    intersect_rectangles,
    mapping_geometry,
    parse_point,
    parse_polygon,
    parse_rectangle,
    validate_tiling,
)
from .schemas import InitRequest, MeasurementRequest, ReplaceRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="宗地边界与地下水采样井归属发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 工具

def _err(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": payload})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_revision(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
    return int(row[0]) if row is not None else None


def _load_region(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT geojson FROM regions WHERE id = 1").fetchone()
    return json.loads(row[0])


def _load_parcels(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT parcel_id, geojson FROM parcels WHERE revision = ? ORDER BY parcel_id",
        (revision,),
    ).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_well_geoms(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT well_id, geojson FROM wells ORDER BY well_id").fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_attributions(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT well_id, status, parcel_id, candidates FROM well_attributions "
        "WHERE revision = ? ORDER BY well_id",
        (revision,),
    ).fetchall()
    return {
        r[0]: {"status": r[1], "parcel_id": r[2], "candidates": json.loads(r[3])}
        for r in rows
    }


def _load_measurements(conn: sqlite3.Connection, revision: int) -> dict[str, dict[str, Rectangle]]:
    """读取某版本的测量快照：well_id -> measurement_id -> Rectangle。"""
    rows = conn.execute(
        "SELECT well_id, measurement_id, geojson FROM measurements "
        "WHERE revision = ? ORDER BY well_id, measurement_id",
        (revision,),
    ).fetchall()
    out: dict[str, dict[str, Rectangle]] = {}
    for wid, mid, gj in rows:
        geom = shapely_shape(json.loads(gj))
        minx, miny, maxx, maxy = geom.bounds
        out.setdefault(wid, {})[mid] = Rectangle(minx, miny, maxx, maxy)
    return out


def _possible_locations(well_geoms: dict[str, Any],
                        measurements: dict[str, dict[str, Rectangle]]
                        ) -> tuple[dict[str, Any], dict[str, dict]]:
    """计算每口井在所查版本的可能位置几何与附加信息。

    有有效测量的井取所有测量矩形的交集（点/线/面）；未复测的井沿用登记点。
    测量交集在登记时已保证非空，这里做防御性断言。
    返回 (well_id -> 几何, well_id -> {possible_position, measured, measurement_ids})。
    """
    locations: dict[str, Any] = {}
    extra: dict[str, dict] = {}
    for wid, pt in well_geoms.items():
        rects = measurements.get(wid)
        if not rects:
            locations[wid] = pt
            extra[wid] = {
                "measured": False,
                "measurement_ids": [],
                "possible_position": mapping_geometry(pt),
            }
            continue
        report = intersect_rectangles(rects)
        if not report.ok:  # 存储层不变量：登记时空交集已被拒绝
            raise RuntimeError(f"井 {wid} 的测量矩形交集为空，数据不变量被破坏")
        geom = bbox_to_geometry(report.bbox)
        locations[wid] = geom
        extra[wid] = {
            "measured": True,
            "measurement_ids": sorted(rects),
            "possible_position": mapping_geometry(geom),
        }
    return locations, extra


def _measurements_to_snapshot(measurements: dict[str, dict[str, Rectangle]]
                              ) -> dict[tuple[str, str], dict]:
    """嵌套测量集合拍平成 (well_id, measurement_id) -> 矩形 GeoJSON。"""
    snapshot: dict[tuple[str, str], dict] = {}
    for wid, rects in measurements.items():
        for mid, rect in rects.items():
            snapshot[(wid, mid)] = rect.as_geojson()
    return snapshot


def _insert_version(conn: sqlite3.Connection, revision: int,
                    parcels: dict[str, dict], attributions: dict[str, dict],
                    region: Optional[dict], wells: Optional[dict[str, dict]],
                    measurements: Optional[dict[tuple[str, str], dict]] = None) -> None:
    """在已开启的事务内写入一个完整版本（不含 meta 指针）。"""
    if region is not None:
        conn.execute("INSERT INTO regions (id, geojson) VALUES (1, ?)",
                     (json.dumps(region),))
    if wells is not None:
        conn.executemany(
            "INSERT INTO wells (well_id, geojson) VALUES (?, ?)",
            [(wid, json.dumps(g)) for wid, g in wells.items()],
        )
    conn.execute(
        "INSERT INTO versions (revision, created_at, parcel_count, well_count) "
        "VALUES (?, ?, ?, ?)",
        (revision, _now(), len(parcels),
         len(wells) if wells is not None else len(attributions)),
    )
    conn.executemany(
        "INSERT INTO parcels (revision, parcel_id, geojson) VALUES (?, ?, ?)",
        [(revision, pid, json.dumps(g)) for pid, g in parcels.items()],
    )
    conn.executemany(
        "INSERT INTO well_attributions (revision, well_id, status, parcel_id, candidates) "
        "VALUES (?, ?, ?, ?, ?)",
        [(revision, wid, a["status"], a["parcel_id"], json.dumps(a["candidates"]))
         for wid, a in attributions.items()],
    )
    if measurements is not None:
        conn.executemany(
            "INSERT INTO measurements (revision, well_id, measurement_id, geojson) "
            "VALUES (?, ?, ?, ?)",
            [(revision, wid, mid, json.dumps(g))
             for (wid, mid), g in sorted(measurements.items())],
        )


def _validation_error(reasons: list[dict], *, gap_area: Optional[float] = None,
                      overlaps: Optional[list] = None,
                      overlap_parcel_ids: Optional[list] = None) -> JSONResponse:
    return _err(422, {
        "error": "validation_failed",
        "message": "整批新边界未通过核查，当前版本保持不变",
        "gap_area": gap_area,
        "overlaps": overlaps or [],
        "overlap_parcel_ids": overlap_parcel_ids or [],
        "reasons": reasons,
    })


def _well_summary(attributions: dict[str, dict]) -> dict:
    summary = {"assigned": 0, "pending": 0, "uncovered": 0}
    for a in attributions.values():
        summary[a["status"]] += 1
    return summary


def _parse_parcels(items, reasons: list[dict]) -> tuple[dict[str, Any], dict[str, dict]]:
    """解析一批宗地几何，把重复编号与几何错误追加到 reasons。

    返回 (id -> shapely Polygon, id -> 原始 GeoJSON)。
    """
    geoms: dict[str, Any] = {}
    geojson_by_id: dict[str, dict] = {}
    for p in items:
        if p.id in geojson_by_id:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            geoms[p.id] = geom
            geojson_by_id[p.id] = p.geometry
    return geoms, geojson_by_id


# ---------------------------------------------------------------- 首次提交

@app.post("/v1/init", status_code=201)
def initialize(req: InitRequest):
    reasons: list[dict] = []

    region_geom, errs = parse_polygon(req.region, label="区域")
    reasons.extend(errs)

    parcel_geoms, parcel_gj = _parse_parcels(req.parcels, reasons)

    well_geoms: dict[str, Any] = {}
    well_gj: dict[str, dict] = {}
    for w in req.wells:
        if w.id in well_gj:
            reasons.append({"code": "duplicate_well_id", "well_id": w.id,
                            "message": f"井编号 {w.id} 在请求中重复"})
            continue
        geom, errs = parse_point(w.geometry, well_id=w.id, label=f"井 {w.id}")
        reasons.extend(errs)
        if geom is not None:
            well_geoms[w.id] = geom
            well_gj[w.id] = w.geometry

    tiling = None
    if region_geom is not None and len(parcel_geoms) == len(req.parcels):
        tiling = validate_tiling(parcel_geoms, region_geom)
        reasons.extend(tiling.reasons)
    if reasons:
        return _validation_error(
            reasons,
            gap_area=tiling.gap_area if tiling else None,
            overlaps=tiling.overlaps if tiling else None,
            overlap_parcel_ids=tiling.overlap_parcel_ids if tiling else None,
        )

    # 首次提交尚无测量，可能位置即登记点
    attributions = attribute_locations(well_geoms, parcel_geoms)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) is not None:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "already_initialized",
                                  "message": "数据集已完成首次提交，不能重复初始化"})
            _insert_version(conn, 1, parcel_gj, attributions,
                            region=req.region, wells=well_gj)
            conn.execute("INSERT INTO meta (id, current_revision) VALUES (1, 1)")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": 1, "parcel_count": len(parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 整批替换

@app.put("/v1/parcels")
def replace_parcels(req: ReplaceRequest):
    # 1) 读取 base 版本快照（读事务，不阻塞他人）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _err(409, {
                "error": "revision_conflict",
                "message": f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                           "请重新读取后再提交",
                "current_revision": current,
                "base_revision": req.base_revision,
            })
        region_gj = _load_region(conn)
        old_parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
        base_measurements = _load_measurements(conn, current)
    finally:
        conn.close()

    # 2) 应用整批替换：只允许替换当前版本中存在的宗地
    reasons: list[dict] = []
    region_geom, _ = parse_polygon(region_gj, label="区域")
    old_geoms = {}
    for pid, gj in old_parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        old_geoms[pid] = geom

    new_geoms = dict(old_geoms)
    new_parcel_gj = dict(old_parcel_gj)
    seen: set[str] = set()
    for p in req.parcels:
        if p.id in seen:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        seen.add(p.id)
        if p.id not in old_parcel_gj:
            reasons.append({"code": "unknown_parcel_id", "parcel_id": p.id,
                            "message": f"宗地 {p.id} 在当前版本中不存在，"
                                       "只能整批替换已有宗地"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            new_geoms[p.id] = geom
            new_parcel_gj[p.id] = p.geometry
    if reasons:
        return _validation_error(reasons)

    # 3) 相邻宗地联改按整批新边界核查
    tiling = validate_tiling(new_geoms, region_geom)
    if not tiling.ok:
        return _validation_error(
            tiling.reasons,
            gap_area=tiling.gap_area,
            overlaps=tiling.overlaps,
            overlap_parcel_ids=tiling.overlap_parcel_ids,
        )

    # 4) 依新版边界重算全部井归属：沿用基准版测量，可能位置=测量矩形交集，
    #    未复测的井继续用登记点
    well_geoms = {}
    for wid, gj in well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_geoms[wid] = geom
    locations, _ = _possible_locations(well_geoms, base_measurements)
    attributions = attribute_locations(locations, new_geoms)

    # 5) 乐观并发提交：同版并发修订只成功一份
    new_revision = current + 1
    measurement_snapshot = _measurements_to_snapshot(base_measurements)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _err(409, {
                    "error": "revision_conflict",
                    "message": "提交期间已有其他修订发布，本次修订作废，请基于新版重试",
                    "current_revision": _current_revision(conn),
                    "base_revision": req.base_revision,
                })
            _insert_version(conn, new_revision, new_parcel_gj, attributions,
                            region=None, wells=None,
                            measurements=measurement_snapshot)
            cur = conn.execute(
                "UPDATE meta SET current_revision = ? WHERE id = 1 AND current_revision = ?",
                (new_revision, req.base_revision),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "revision_conflict",
                                  "message": "版本指针已被其他修订更新",
                                  "base_revision": req.base_revision})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": new_revision, "parcel_count": len(new_parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 测量登记

@app.post("/v1/measurements")
def register_measurements(req: MeasurementRequest):
    """登记/订正测量并发布新版本：测量与重算的归属同版发布。

    - 提交所见修订号、已登记井号、测量编号和轴对齐误差矩形；
    - 同井同编号视为订正（UPSERT 覆盖）；
    - 以该井所有有效矩形的交集为可能位置（线段/单点也有效），交集为空则
      整体拒绝并返回一对互不相交的测量编号，原版保持不变；
    - 同版并发提交只接受一份（与整批改图共用同一版本闸门）。
    """
    # 1) 读取 base 版本快照
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _err(409, {
                "error": "revision_conflict",
                "message": f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                           "请重新读取后再提交",
                "current_revision": current,
                "base_revision": req.base_revision,
            })
        parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
        measurements = _load_measurements(conn, current)
    finally:
        conn.close()

    # 2) 结构/语义校验：井号必须已登记、矩形有限且正面积、同井同编号去重
    reasons: list[dict] = []
    known_wells = set(well_gj)
    seen: set[tuple[str, str]] = set()
    incoming: list[tuple[str, str, Rectangle]] = []
    for m in req.measurements:
        key = (m.well_id, m.measurement_id)
        if key in seen:
            reasons.append({
                "code": "duplicate_measurement_id",
                "well_id": m.well_id, "measurement_id": m.measurement_id,
                "message": f"井 {m.well_id} 的测量编号 {m.measurement_id} 在请求中重复",
            })
            continue
        seen.add(key)
        if m.well_id not in known_wells:
            reasons.append({
                "code": "unknown_well_id",
                "well_id": m.well_id, "measurement_id": m.measurement_id,
                "message": f"井 {m.well_id} 尚未登记，不能登记测量",
            })
            continue
        rect, errs = parse_rectangle(
            m.rectangle.model_dump(),
            well_id=m.well_id, measurement_id=m.measurement_id)
        reasons.extend(errs)
        if rect is not None:
            incoming.append((m.well_id, m.measurement_id, rect))
    if reasons:
        return _err(422, {"error": "validation_failed",
                          "message": "测量登记未通过校验，当前版本保持不变",
                          "reasons": reasons})

    # 3) 应用 UPSERT 订正，逐井检查所有有效矩形的共同范围
    updated = {wid: dict(rects) for wid, rects in measurements.items()}
    for wid, mid, rect in incoming:
        updated.setdefault(wid, {})[mid] = rect

    for wid in sorted(updated):
        report = intersect_rectangles(updated[wid])
        if not report.ok:
            a, b = report.disjoint_pair
            return _err(422, {
                "error": "empty_intersection",
                "message": (f"井 {wid} 的测量矩形共同范围为空，登记被拒绝，"
                            "当前版本保持不变"),
                "well_id": wid,
                "disjoint_measurement_ids": [a, b],
                "reasons": [{
                    "code": "empty_intersection",
                    "well_id": wid,
                    "measurement_ids": [a, b],
                    "message": f"测量 {a} 与 {b} 的误差矩形互不相交，共同范围为空",
                }],
            })

    # 4) 以新测量集合重算全部井归属（可能位置=矩形交集，未复测井用登记点）
    parcel_geoms = {}
    for pid, gj in parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        parcel_geoms[pid] = geom
    well_geoms = {}
    for wid, gj in well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_geoms[wid] = geom
    locations, _ = _possible_locations(well_geoms, updated)
    attributions = attribute_locations(locations, parcel_geoms)

    # 5) 乐观并发提交：同版并发提交（含与整批改图竞争）只接受一份
    new_revision = current + 1
    snapshot = _measurements_to_snapshot(updated)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _err(409, {
                    "error": "revision_conflict",
                    "message": "提交期间已有其他修订发布，本次测量登记作废，请基于新版重试",
                    "current_revision": _current_revision(conn),
                    "base_revision": req.base_revision,
                })
            _insert_version(conn, new_revision, parcel_gj, attributions,
                            region=None, wells=None, measurements=snapshot)
            cur = conn.execute(
                "UPDATE meta SET current_revision = ? WHERE id = 1 AND current_revision = ?",
                (new_revision, req.base_revision),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "revision_conflict",
                                  "message": "版本指针已被其他修订更新",
                                  "base_revision": req.base_revision})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": new_revision,
        "registered": [{"well_id": wid, "measurement_id": mid}
                       for wid, mid, _ in incoming],
        "measurement_count": len(snapshot),
        "well_count": len(well_gj),
        "well_summary": _well_summary(attributions),
    }


# ---------------------------------------------------------------- 查询

def _wells_payload(well_gj: dict[str, dict], attributions: dict[str, dict],
                   measurements: dict[str, dict[str, Rectangle]]) -> list[dict]:
    """组装井响应：保留登记点 geometry，并附所查版本的可能位置与归属。"""
    well_geoms: dict[str, Any] = {}
    for wid, gj in well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_geoms[wid] = geom
    _, extra = _possible_locations(well_geoms, measurements)
    return [
        {
            "id": wid,
            "geometry": well_gj[wid],                       # 登记点（不可变）
            "registered_point": well_gj[wid],
            "possible_position": extra[wid]["possible_position"],
            "measured": extra[wid]["measured"],
            "measurement_ids": extra[wid]["measurement_ids"],
            **attributions[wid],
        }
        for wid in sorted(well_gj)
    ]


@app.get("/v1/current")
def get_current():
    """当前完整版本：区域 + 边界 + 井归属，同一读事务取出，保证同版一致。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            revision = _current_revision(conn)
            if revision is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "not_initialized",
                                  "message": "尚未首次提交"})
            region = _load_region(conn)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            measurements = _load_measurements(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": revision,
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions, measurements),
    }


@app.get("/v1/parcels")
def get_parcels():
    """当前宗地边界（响应携带所属修订号）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        parcels = _load_parcels(conn, revision)
    finally:
        conn.close()
    return {"revision": revision,
            "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)]}


@app.get("/v1/wells")
def get_wells():
    """当前井归属（响应携带所属修订号，与 /v1/parcels 对号即可验证同版）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        well_gj = _load_well_geoms(conn)
        attributions = _load_attributions(conn, revision)
        measurements = _load_measurements(conn, revision)
    finally:
        conn.close()
    return {"revision": revision,
            "wells": _wells_payload(well_gj, attributions, measurements)}


@app.get("/v1/measurements")
def get_measurements():
    """当前版本的测量快照（每口井各次测量的误差矩形）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        measurements = _load_measurements(conn, revision)
    finally:
        conn.close()
    items = [
        {"well_id": wid, "measurement_id": mid, "rectangle": rect.as_geojson()}
        for wid in sorted(measurements)
        for mid, rect in sorted(measurements[wid].items())
    ]
    return {"revision": revision, "measurements": items}


@app.get("/v1/revisions")
def list_revisions():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT revision, created_at, parcel_count, well_count "
            "FROM versions ORDER BY revision"
        ).fetchall()
    finally:
        conn.close()
    return {"revisions": [dict(r) for r in rows]}


@app.get("/v1/revisions/{revision}")
def get_revision(revision: int):
    """读取指定历史版本（含被替换前的旧版）。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT revision, created_at, parcel_count, well_count "
                "FROM versions WHERE revision = ?", (revision,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "revision_not_found",
                                  "message": f"版本 {revision} 不存在"})
            region = _load_region(conn)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            measurements = _load_measurements(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": row[0],
        "created_at": row[1],
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions, measurements),
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

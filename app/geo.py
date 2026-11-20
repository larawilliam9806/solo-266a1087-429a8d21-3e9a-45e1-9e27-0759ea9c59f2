"""几何校验与井归属计算（Shapely）。

规则：
- 边界：无洞 GeoJSON Polygon，同一平面（2D）整数坐标，几何必须有效。
- 井：2D Point，有限数值坐标。
- 宗地集合：内部互不重叠（共边/共点允许），合起来恰好覆盖区域。
- 井归属：严格落在唯一宗地内部 → assigned；落在一条/多条宗地边界上
  → pending 并列出全部候选宗地；不沾任何宗地 → uncovered。

容差说明：整数顶点多边形的面积是 0.5 的整数倍（鞋带公式），因此真实的
空缺/重叠面积至少为 0.5，而浮点噪声远小于 1e-6，用 AREA_TOL 即可可靠区分。

测量与可能位置：
- 测量提交同一坐标系内的轴对齐误差矩形（bbox），坐标须有限、面积须为正。
- 同井所有有效矩形的交集即"可能位置"（轴对齐，交集为线段/单点也有效）；
  交集为空时整次登记拒绝。
- 归属以可能位置判断：整个可能位置都严格位于唯一宗地内部才 assigned；
  与宗地相交但无法确认唯一归属时 pending，candidates 列出所有相交宗地；
  与所有宗地都不相交则 uncovered。未复测的井继续使用登记点。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import explain_validity

AREA_TOL = 1e-6


# ---------------------------------------------------------------- 基础检查

def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_position(pos: Any, require_integer: bool) -> Optional[str]:
    if not isinstance(pos, (list, tuple)) or len(pos) != 2:
        return "坐标必须是 [x, y] 二元组（同一平面，不接受高程维）"
    x, y = pos
    if not (_is_finite_number(x) and _is_finite_number(y)):
        return "坐标必须是有限数值"
    if require_integer and not (float(x).is_integer() and float(y).is_integer()):
        return f"边界坐标必须为整数，得到 ({x}, {y})"
    return None


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"code": code, "message": message, **extra}


# ---------------------------------------------------------------- 几何解析

def parse_polygon(geom: Any, *, parcel_id: Optional[str] = None,
                  label: str = "geometry") -> tuple[Optional[Polygon], list[dict]]:
    """解析并校验无洞、整数坐标、有效的 GeoJSON Polygon。"""
    errors: list[dict] = []
    tag = {"parcel_id": parcel_id} if parcel_id is not None else {}

    def fail(code: str, message: str, **extra: Any) -> None:
        errors.append(_err(code, message, **tag, **extra))

    if not isinstance(geom, dict) or geom.get("type") != "Polygon":
        fail("invalid_geometry", f"{label}：必须是 GeoJSON Polygon 几何对象")
        return None, errors

    rings = geom.get("coordinates")
    if not isinstance(rings, (list, tuple)) or not rings:
        fail("invalid_geometry", f"{label}：coordinates 必须是非空环数组")
        return None, errors
    if len(rings) > 1:
        fail("hole_not_allowed", f"{label}：边界不允许带洞（内环），发现 {len(rings) - 1} 个内环")
        return None, errors

    ring = rings[0]
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        fail("invalid_geometry", f"{label}：外环至少需要 4 个坐标点（首尾闭合）")
        return None, errors
    for pos in ring:
        problem = _check_position(pos, require_integer=True)
        if problem:
            fail("non_integer_coordinate", f"{label}：{problem}")
            return None, errors

    try:
        poly = shape({"type": "Polygon", "coordinates": [list(ring)]})
    except Exception as exc:  # noqa: BLE001 - shapely 抛出的异常类型不固定
        fail("invalid_geometry", f"{label}：无法构造多边形：{exc}")
        return None, errors

    if poly.is_empty or poly.area <= 0:
        fail("invalid_geometry", f"{label}：多边形面积为 0")
        return None, errors
    if not poly.is_valid:
        fail("invalid_geometry", f"{label}：多边形无效（{explain_validity(poly)}）")
        return None, errors
    return poly, errors


def parse_point(geom: Any, *, well_id: Optional[str] = None,
                label: str = "geometry") -> tuple[Optional[Point], list[dict]]:
    """解析并校验 GeoJSON Point（井）。"""
    errors: list[dict] = []
    tag = {"well_id": well_id} if well_id is not None else {}

    def fail(code: str, message: str, **extra: Any) -> None:
        errors.append(_err(code, message, **tag, **extra))

    if not isinstance(geom, dict) or geom.get("type") != "Point":
        fail("invalid_geometry", f"{label}：必须是 GeoJSON Point 几何对象")
        return None, errors
    problem = _check_position(geom.get("coordinates"), require_integer=False)
    if problem:
        fail("invalid_geometry", f"{label}：{problem}")
        return None, errors
    x, y = geom["coordinates"]
    return Point(float(x), float(y)), errors


# ---------------------------------------------------------------- 宗地集合核查

@dataclass
class TilingReport:
    ok: bool
    gap_area: float = 0.0
    overflow_area: float = 0.0
    overlaps: list[dict] = field(default_factory=list)        # [{"parcel_ids": [a, b], "area": x}]
    overlap_parcel_ids: list[str] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)


def validate_tiling(parcels: dict[str, Polygon], region: Polygon) -> TilingReport:
    """核查整批新边界：在区域内、互不重叠、恰好覆盖区域。"""
    reasons: list[dict] = []
    overlaps: list[dict] = []

    # 1) 每块宗地必须位于区域内
    for pid, geom in parcels.items():
        outside = geom.difference(region)
        if outside.area > AREA_TOL:
            reasons.append(_err(
                "outside_region",
                f"宗地 {pid} 超出区域范围 {outside.area:.6g} 平方单位",
                parcel_id=pid, area=round(outside.area, 6),
            ))

    # 2) 两两内部不重叠（共边/共点面积为零，允许）
    ids = sorted(parcels)
    geoms = {pid: parcels[pid] for pid in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            inter = geoms[a].intersection(geoms[b])
            if inter.area > AREA_TOL:
                overlaps.append({"parcel_ids": [a, b], "area": round(inter.area, 6)})
    for ov in overlaps:
        a, b = ov["parcel_ids"]
        reasons.append(_err(
            "overlap",
            f"宗地 {a} 与 {b} 内部重叠 {ov['area']:.6g} 平方单位",
            parcel_ids=[a, b], area=ov["area"],
        ))

    # 3) 合起来恰好覆盖区域
    union = unary_union(list(parcels.values()))
    gap_area = region.difference(union).area
    overflow_area = union.difference(region).area
    if gap_area > AREA_TOL:
        reasons.append(_err(
            "gap",
            f"宗地合起来未覆盖区域，空缺 {gap_area:.6g} 平方单位",
            area=round(gap_area, 6),
        ))
    if overflow_area > AREA_TOL:
        reasons.append(_err(
            "overflow",
            f"宗地合起来超出区域 {overflow_area:.6g} 平方单位",
            area=round(overflow_area, 6),
        ))

    overlap_ids = sorted({pid for ov in overlaps for pid in ov["parcel_ids"]})
    return TilingReport(
        ok=not reasons,
        gap_area=round(gap_area, 6),
        overflow_area=round(overflow_area, 6),
        overlaps=overlaps,
        overlap_parcel_ids=overlap_ids,
        reasons=reasons,
    )


# ---------------------------------------------------------------- 井归属

def attribute_wells(wells: dict[str, Point],
                    parcels: dict[str, Polygon]) -> dict[str, dict]:
    """按新边界重算全部井归属（登记点口径）。

    - 严格位于唯一宗地内部 → assigned
    - 落在宗地边界上（共边/共点）→ pending，candidates 列出所有候选宗地
    - 不沾任何宗地（含区域外）→ uncovered
    """
    return attribute_locations(wells, parcels)


# ---------------------------------------------------------------- 测量矩形

@dataclass
class Rectangle:
    """同一坐标系内的轴对齐误差矩形。"""
    minx: float
    miny: float
    maxx: float
    maxy: float

    def to_shapely(self) -> Polygon:
        return box(self.minx, self.miny, self.maxx, self.maxy)

    def as_geojson(self) -> dict:
        return mapping_geometry(self.to_shapely())


def parse_rectangle(rect: Any, *, well_id: Optional[str] = None,
                    measurement_id: Optional[str] = None) -> tuple[Optional[Rectangle], list[dict]]:
    """解析轴对齐误差矩形：四个坐标有限，且 max > min（面积为正）。"""
    errors: list[dict] = []
    tag: dict[str, Any] = {}
    if well_id is not None:
        tag["well_id"] = well_id
    if measurement_id is not None:
        tag["measurement_id"] = measurement_id
    label = f"井 {well_id} 测量 {measurement_id}" if well_id is not None else "测量"

    def fail(code: str, message: str) -> None:
        errors.append(_err(code, message, **tag))

    if not isinstance(rect, dict):
        fail("invalid_rectangle", f"{label}：rectangle 必须是包含 minx/miny/maxx/maxy 的对象")
        return None, errors
    keys = ("minx", "miny", "maxx", "maxy")
    vals = []
    for k in keys:
        v = rect.get(k)
        if not _is_finite_number(v):
            fail("invalid_rectangle", f"{label}：{k} 必须是有限数值")
        else:
            vals.append(float(v))
    if errors:
        return None, errors
    minx, miny, maxx, maxy = vals
    if maxx <= minx or maxy <= miny:
        fail("invalid_rectangle",
             f"{label}：矩形面积须为正，需要 maxx > minx 且 maxy > miny，"
             f"得到 ({minx}, {miny}) - ({maxx}, {maxy})")
        return None, errors
    return Rectangle(minx, miny, maxx, maxy), errors


@dataclass
class IntersectionReport:
    """同井一组矩形求交的结果。"""
    ok: bool
    bbox: Optional[tuple[float, float, float, float]] = None  # (minx, miny, maxx, maxy)
    disjoint_pair: Optional[tuple[str, str]] = None          # 交集为空时的一对互不相交编号


def intersect_rectangles(rects: dict[str, Rectangle]) -> IntersectionReport:
    """计算同一口井所有有效矩形的共同范围（轴对齐 bbox 交集）。

    交集为点或线段同样有效（bbox 有零宽边但不报错）；交集为空时返回一对
    互不相交的测量编号。
    """
    if not rects:
        return IntersectionReport(ok=False)
    ids = sorted(rects)
    minx = max(rects[i].minx for i in ids)
    miny = max(rects[i].miny for i in ids)
    maxx = min(rects[i].maxx for i in ids)
    maxy = min(rects[i].maxy for i in ids)
    if minx <= maxx and miny <= maxy:
        return IntersectionReport(ok=True, bbox=(minx, miny, maxx, maxy))

    # 交集为空：至少一个轴向上区间不相交，找出一对互不相交的测量。
    # x 轴上取下界最大者与上界最小者，二者在 x 方向必然不重叠。
    lo = max(ids, key=lambda i: (rects[i].minx, i))
    hi = min(ids, key=lambda i: (rects[i].maxx, i))
    if rects[lo].minx > rects[hi].maxx:
        pair = (lo, hi)
    else:
        lo = max(ids, key=lambda i: (rects[i].miny, i))
        hi = min(ids, key=lambda i: (rects[i].maxy, i))
        # y 轴必不相交，否则与 bbox 交集为空矛盾
        pair = (lo, hi)
    a, b = sorted(pair)
    return IntersectionReport(ok=False, disjoint_pair=(a, b))


def bbox_to_geometry(bbox: tuple[float, float, float, float]) -> BaseGeometry:
    """把 bbox 转成 Shapely 几何：退化为线段/单点时给出对应有效几何。"""
    minx, miny, maxx, maxy = bbox
    if minx == maxx and miny == maxy:
        return Point(minx, miny)
    if minx == maxx or miny == maxy:
        return LineString([(minx, miny), (maxx, maxy)])
    return box(minx, miny, maxx, maxy)


def mapping_geometry(geom: BaseGeometry) -> dict:
    return mapping(geom)


# ---------------------------------------------------------------- 可能位置归属

def attribute_locations(locations: dict[str, BaseGeometry],
                        parcels: dict[str, Polygon]) -> dict[str, dict]:
    """按"可能位置"几何计算每口井的归属。

    位置可以是 Point（未复测井/单点交集）、LineString（线段交集）或
    Polygon（面交集）：
    - 整个位置严格位于唯一宗地内部（parcel.contains(location)）→ assigned；
    - 否则与至少一块宗地相交（含只碰到边界、跨边界）→ pending，
      candidates 列出所有相交宗地；
    - 与所有宗地都不相交 → uncovered。
    """
    items = sorted(parcels.items())
    result: dict[str, dict] = {}
    for wid in sorted(locations):
        loc = locations[wid]
        containing = [pid for pid, geom in items if geom.contains(loc)]
        if len(containing) == 1:
            result[wid] = {"status": "assigned", "parcel_id": containing[0],
                           "candidates": containing}
            continue
        touching = [pid for pid, geom in items if geom.intersects(loc)]
        if touching:
            result[wid] = {"status": "pending", "parcel_id": None,
                           "candidates": touching}
        else:
            result[wid] = {"status": "uncovered", "parcel_id": None,
                           "candidates": []}
    return result

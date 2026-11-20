"""请求体的 Pydantic 模型（结构校验；几何语义校验在 geo.py 中做）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_PARCELS = 40
MAX_WELLS = 100


def _coerce_id(v: Any) -> Any:
    """编号允许字符串或整数，统一归一成字符串。"""
    if isinstance(v, bool):
        raise ValueError("编号必须是非空字符串或整数")
    if isinstance(v, int):
        return str(v)
    return v


class ParcelIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]

    _coerce = field_validator("id", mode="before")(_coerce_id)


class WellIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]

    _coerce = field_validator("id", mode="before")(_coerce_id)


class InitRequest(BaseModel):
    """首次提交：区域 + 全部宗地 + 全部井。"""
    region: dict[str, Any]
    parcels: list[ParcelIn] = Field(min_length=1, max_length=MAX_PARCELS)
    wells: list[WellIn] = Field(default_factory=list, max_length=MAX_WELLS)


class ReplaceRequest(BaseModel):
    """整批替换指定宗地：携带所见修订号 + 这些宗地的完整新边界。"""
    base_revision: int = Field(ge=1)
    parcels: list[ParcelIn] = Field(min_length=1, max_length=MAX_PARCELS)


class RectangleIn(BaseModel):
    """同一坐标系内的轴对齐误差矩形（bbox）；有限性与正面积在 geo 层语义校验。"""
    minx: float
    miny: float
    maxx: float
    maxy: float


class MeasurementIn(BaseModel):
    """一次测量：已登记井号 + 测量编号 + 轴对齐误差矩形。"""
    well_id: str = Field(min_length=1, max_length=64)
    measurement_id: str = Field(min_length=1, max_length=64)
    rectangle: RectangleIn

    _coerce_well = field_validator("well_id", mode="before")(_coerce_id)
    _coerce_measurement = field_validator("measurement_id", mode="before")(_coerce_id)


class MeasurementRequest(BaseModel):
    """测量登记：携带所见修订号；同井同编号视为订正（UPSERT）。"""
    base_revision: int = Field(ge=1)
    measurements: list[MeasurementIn] = Field(min_length=1, max_length=500)

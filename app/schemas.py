"""Response models. These drive both validation and the generated openapi.json."""

from datetime import date, datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class HierarchyNode(BaseModel):
    code: str = Field(description="Network code, e.g. 'F-007'. Empty if the portal sent none and it could not be inferred.")
    name: str = Field(description="Human-readable name, e.g. 'Feeder 7'.")


class Hierarchy(BaseModel):
    zone: HierarchyNode
    circle: HierarchyNode
    division: HierarchyNode
    subdivision: HierarchyNode
    substation: HierarchyNode
    feeder: HierarchyNode
    dt: HierarchyNode = Field(description="Distribution transformer the meter is connected to.")


class Location(BaseModel):
    latitude: float
    longitude: float


class Meter(BaseModel):
    meter_id: str = Field(examples=["J100007"])
    serial_no: Optional[str] = Field(None, examples=["SE11409"])
    make: Optional[str] = Field(None, examples=["Genus"])
    phase_type: Optional[str] = Field(None, description="'single' or 'three'.")
    install_status: Optional[str] = Field(None, description="'Installed', 'Faulty' or 'Decommissioned'.")
    install_type: Optional[str] = Field(None, description="'Whole Current' or 'CT Operated'.")
    nameplate_format: Optional[str] = Field(
        None, description="Which nameplate format the portal stores for this meter ('legacy' or 'v2'). Informational."
    )
    hierarchy: Hierarchy
    location: Optional[Location] = None
    data_quality: List[str] = Field(
        default_factory=list,
        description="Problems found in the portal's data for this meter and how they were handled.",
    )
    distance_km: Optional[float] = Field(None, description="Distance from the search point on proximity searches; null otherwise.")


class Page(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class Freshness(BaseModel):
    snapshot_loaded_at: Optional[datetime] = Field(None, description="When the fleet snapshot was last pulled from the portal (UTC).")
    stale: bool = Field(description="True if the snapshot is past its TTL because the last refresh failed.")


class MeterList(BaseModel):
    data: List[Meter]
    page: Page
    freshness: Freshness


class MeterResponse(BaseModel):
    data: Meter
    source: str = Field(description="'snapshot' (fleet index) or 'live' (fetched directly because it was not in the snapshot).")
    freshness: Freshness


class Reading(BaseModel):
    timestamp: datetime = Field(description="ISO 8601 with offset; portal times are Asia/Kolkata (+05:30).")
    kwh: Optional[float] = Field(None, description="Cumulative active energy register (kWh).")
    kvah: Optional[float] = Field(None, description="Cumulative apparent energy register (kVAh).")
    voltage_r: Optional[float] = Field(None, description="R-phase voltage (V).")
    kwh_delta: Optional[float] = Field(None, description="kWh consumed since the previous reading. Null for the first reading, gaps or resets.")
    kvah_delta: Optional[float] = None


class DailyConsumption(BaseModel):
    date: date
    kwh: float
    kvah: float
    power_factor: Optional[float] = Field(None, description="kWh / kVAh for the day.")
    intervals: int = Field(description="Number of reading intervals that contributed.")
    voltage_r_min: Optional[float] = None
    voltage_r_max: Optional[float] = None
    voltage_r_avg: Optional[float] = None


class ReadingsResponse(BaseModel):
    meter_id: str
    resolution: str = Field(description="'raw' or 'daily'.")
    interval_minutes: Optional[int] = Field(
        None, description="Detected native cadence of this meter (30 or 1440 on the current portal)."
    )
    window_start: Optional[datetime] = Field(None, description="First reading returned.")
    window_end: Optional[datetime] = Field(None, description="Last reading returned.")
    total_kwh: Optional[float] = Field(None, description="Sum of consumption across the window.")
    readings: Optional[List[Reading]] = Field(None, description="Present when resolution=raw.")
    daily: Optional[List[DailyConsumption]] = Field(None, description="Present when resolution=daily.")
    data_quality: List[str] = Field(default_factory=list)


class Transformer(BaseModel):
    code: str = Field(examples=["DT-007"])
    name: str
    feeder_code: Optional[str] = None
    capacity_kva: Optional[float] = None
    meter_count: int
    meters_by_status: Dict[str, int]


class TransformerList(BaseModel):
    data: List[Transformer]
    freshness: Freshness


class TreeNode(BaseModel):
    level: str
    code: Optional[str] = None
    name: Optional[str] = None
    meter_count: int
    children: Optional[List["TreeNode"]] = None


class HierarchyResponse(BaseModel):
    data: List[TreeNode]
    freshness: Freshness


class Summary(BaseModel):
    meters: int
    transformers: int
    by_install_status: Dict[str, int]
    by_make: Dict[str, int]
    by_phase_type: Dict[str, int]
    by_install_type: Dict[str, int]
    meters_with_data_quality_issues: int


class SummaryResponse(BaseModel):
    data: Summary
    freshness: Freshness


class ErrorBody(BaseModel):
    code: str
    message: str


class Error(BaseModel):
    error: ErrorBody


class Health(BaseModel):
    status: str
    snapshot_loaded_at: Optional[datetime] = None
    meters: int
    last_refresh_error: Optional[str] = None

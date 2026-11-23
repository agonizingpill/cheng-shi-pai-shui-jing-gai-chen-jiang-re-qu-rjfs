from datetime import datetime
from pydantic import BaseModel, Field

class LoginIn(BaseModel):
    username: str
    password: str

class SurveyPointIn(BaseModel):
    point_code: str = Field(min_length=3, max_length=60)
    manhole_id: int
    x: float
    y: float
    elevation_mm: float
    settlement_mm: float = Field(ge=0, le=200)
    coordinate_reference: str
    measured_at: datetime

class BatchIn(BaseModel):
    batch_code: str
    measured_at: datetime
    instrument: str
    coordinate_reference: str
    notes: str = ""

class NoticeIn(BaseModel):
    road_name: str
    contractor: str
    permit_no: str
    starts_at: datetime
    ends_at: datetime
    lane_closure_required: bool = False

class ResurveyIn(BaseModel):
    measured_at: datetime
    settlement_after_mm: float = Field(ge=0, le=200)
    notes: str = ""

class NightWindowIn(BaseModel):
    window_code: str = Field(min_length=3, max_length=60)
    night_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    road_name: str = Field(min_length=2, max_length=120)
    starts_at: datetime
    ends_at: datetime
    boundary: dict
    quota: int = Field(ge=0, le=500)

class QuotaIn(BaseModel):
    quota: int = Field(ge=0, le=500)

class NightPlanIn(BaseModel):
    night_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

class NightConfirmIn(BaseModel):
    night_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    idempotency_key: str = Field(min_length=8, max_length=80)
    plan_ids: list[int] | None = None

"""夜间封道窗口排班：名额、成片/散点排班、两批同时确认与幂等回执。"""

import logging
from datetime import date, datetime, timezone

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from sklearn.cluster import DBSCAN
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .constants.enums import SINGLE_SETTLEMENT_THRESHOLD_MM
from .database import get_db
from .models import (
    ConfirmReceipt,
    ManholeAsset,
    NightWindow,
    ScheduleAssignment,
    SchedulePlan,
    SurveyBatch,
    SurveyPoint,
    User,
)
from .schemas import NightConfirmIn, NightPlanIn, NightWindowIn, QuotaIn
from .security import current_user, require_roles
from .services.audit import record
from .services.cluster import EPS_METERS, MIN_SAMPLES

logger = logging.getLogger("manhole-settlement")
router = APIRouter(prefix="/api", tags=["夜间封道排班"])

ACTIVE_ASSIGNMENT_STATUSES = ("planned", "confirmed")


def iso(value):
    return value.isoformat() if value else None


def valid_boundary(boundary):
    try:
        return all(float(boundary[key]) is not None for key in ("min_x", "max_x", "min_y", "max_y"))
    except (TypeError, ValueError, KeyError):
        return False


def latest_eligible_points(db):
    """每口井盖取最近一次且坐标基准与批次一致的测量点。"""
    rows = db.execute(
        select(SurveyPoint, SurveyBatch)
        .join(SurveyBatch, SurveyBatch.id == SurveyPoint.batch_id)
        .order_by(SurveyPoint.manhole_id, SurveyPoint.measured_at.desc(), SurveyPoint.id.desc())
    ).all()
    eligible = {}
    for point, batch in rows:
        if point.manhole_id in eligible:
            continue
        if point.coordinate_reference != batch.coordinate_reference:
            continue
        eligible[point.manhole_id] = point
    return eligible


def taken_manhole_ids(db, night):
    rows = db.execute(
        select(ScheduleAssignment.manhole_id).where(
            ScheduleAssignment.night_date == night,
            ScheduleAssignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
        )
    ).all()
    return {row[0] for row in rows}


def window_view(window, usage):
    confirmed = usage.get(window.id, {}).get("confirmed", 0)
    planned = usage.get(window.id, {}).get("planned", 0)
    return {
        "id": window.id,
        "window_code": window.window_code,
        "night_date": window.night_date,
        "road_name": window.road_name,
        "starts_at": iso(window.starts_at),
        "ends_at": iso(window.ends_at),
        "boundary": window.boundary,
        "quota": window.quota,
        "status": window.status,
        "planned_count": planned,
        "confirmed_count": confirmed,
        "remaining": max(window.quota - confirmed, 0),
    }


def assignment_view(row, windows, manholes):
    return {
        "id": row.id,
        "window_id": row.window_id,
        "window_code": windows[row.window_id].window_code if row.window_id in windows else None,
        "manhole_id": row.manhole_id,
        "asset_code": manholes[row.manhole_id].asset_code if row.manhole_id in manholes else None,
        "kind": row.kind,
        "cluster_label": row.cluster_label,
        "settlement_mm": row.settlement_mm,
        "status": row.status,
    }


def plan_view(plan, windows, manholes):
    return {
        "id": plan.id,
        "plan_code": plan.plan_code,
        "night_date": plan.night_date,
        "kind": plan.kind,
        "status": plan.status,
        "return_reason": plan.return_reason,
        "created_at": iso(plan.created_at),
        "confirmed_at": iso(plan.confirmed_at),
        "assignments": [assignment_view(a, windows, manholes) for a in plan.assignments],
    }


def schedule_payload(db, night):
    windows = db.execute(
        select(NightWindow).where(NightWindow.night_date == night).order_by(NightWindow.id)
    ).scalars().all()
    plans = db.execute(
        select(SchedulePlan).where(SchedulePlan.night_date == night).order_by(SchedulePlan.id)
    ).scalars().all()
    manhole_ids = {a.manhole_id for p in plans for a in p.assignments}
    manholes = {
        m.id: m
        for m in db.execute(select(ManholeAsset).where(ManholeAsset.id.in_(manhole_ids or {0}))).scalars().all()
    }
    usage = {}
    for plan in plans:
        if plan.status == "superseded":
            continue
        for a in plan.assignments:
            if a.status in ACTIVE_ASSIGNMENT_STATUSES:
                bucket = usage.setdefault(a.window_id, {"planned": 0, "confirmed": 0})
                bucket["confirmed" if a.status == "confirmed" else "planned"] += 1
    window_map = {w.id: w for w in windows}
    whereabouts = build_whereabouts(db, night, plans, window_map)
    return {
        "night": night,
        "windows": [window_view(w, usage) for w in windows],
        "plans": [plan_view(p, window_map, manholes) for p in plans],
        "manholes": whereabouts,
    }


def build_whereabouts(db, night, plans, window_map):
    assets = db.execute(select(ManholeAsset).order_by(ManholeAsset.asset_code)).scalars().all()
    eligible = latest_eligible_points(db)
    active = {}
    for plan in plans:
        if plan.status == "superseded":
            continue
        for a in plan.assignments:
            if a.status in ACTIVE_ASSIGNMENT_STATUSES:
                active[a.manhole_id] = (a, plan)
    rows = []
    for asset in assets:
        point = eligible.get(asset.id)
        entry = {
            "manhole_id": asset.id,
            "asset_code": asset.asset_code,
            "street_name": asset.street_name,
            "settlement_mm": point.settlement_mm if point else None,
            "window_code": None,
            "plan_code": None,
            "kind": None,
            "cluster_label": None,
            "status": "unscheduled",
            "whereabouts": "未排班" if point else "基准待统一，未排班",
        }
        if asset.id in active:
            assignment, plan = active[asset.id]
            window = window_map.get(assignment.window_id)
            label = {"planned": "待确认", "confirmed": "已确认"}[assignment.status]
            if plan.status == "returned":
                label = "退回待排"
            entry.update({
                "window_code": window.window_code if window else None,
                "plan_code": plan.plan_code,
                "kind": assignment.kind,
                "cluster_label": assignment.cluster_label,
                "status": "returned" if plan.status == "returned" else assignment.status,
                "whereabouts": f"{label} · {window.window_code if window else ''}",
            })
        rows.append(entry)
    return rows


@router.get("/night-schedule")
def get_night_schedule(
    night: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return schedule_payload(db, night or date.today().isoformat())


@router.post("/night-windows", status_code=201)
def create_window(payload: NightWindowIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    if not valid_boundary(payload.boundary):
        raise HTTPException(422, "覆盖范围需要 min_x/max_x/min_y/max_y 数值")
    if payload.ends_at <= payload.starts_at:
        raise HTTPException(422, "窗口结束时间必须晚于开始时间")
    if db.execute(select(NightWindow).where(NightWindow.window_code == payload.window_code)).scalar_one_or_none():
        raise HTTPException(409, "窗口编号已存在")
    window = NightWindow(
        window_code=payload.window_code,
        night_date=payload.night_date,
        road_name=payload.road_name,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        boundary={key: float(payload.boundary[key]) for key in ("min_x", "max_x", "min_y", "max_y")},
        quota=payload.quota,
        status="open",
        created_by=user.id,
    )
    db.add(window)
    record(db, user.id, "create_night_window", "NightWindow", payload.window_code, {"night_date": payload.night_date, "quota": payload.quota})
    db.commit()
    db.refresh(window)
    return window_view(window, {})


@router.put("/night-windows/{window_id}/quota")
def set_quota(window_id: int, payload: QuotaIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    window = db.get(NightWindow, window_id)
    if not window:
        raise HTTPException(404, "夜间封道窗口不存在")
    old = window.quota
    window.quota = payload.quota
    record(db, user.id, "set_night_window_quota", "NightWindow", window.window_code, {"from": old, "to": payload.quota})
    db.commit()
    usage = {}
    rows = db.execute(
        select(ScheduleAssignment).where(
            ScheduleAssignment.window_id == window.id,
            ScheduleAssignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
        )
    ).scalars().all()
    for row in rows:
        bucket = usage.setdefault(window.id, {"planned": 0, "confirmed": 0})
        bucket["confirmed" if row.status == "confirmed" else "planned"] += 1
    return window_view(window, usage)


@router.post("/night-schedule/plan", status_code=201)
def generate_plan(payload: NightPlanIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    night = payload.night_date
    windows = db.execute(
        select(NightWindow).where(NightWindow.night_date == night, NightWindow.status == "open").order_by(NightWindow.id)
    ).scalars().all()
    if not windows:
        raise HTTPException(409, "该晚没有开放的夜间封道窗口，请先创建窗口")
    stale_plans = db.execute(
        select(SchedulePlan).where(SchedulePlan.night_date == night, SchedulePlan.status.in_(["pending", "returned"]))
    ).scalars().all()
    for plan in stale_plans:
        plan.status = "superseded"
        for assignment in plan.assignments:
            assignment.status = "superseded"
    db.flush()
    taken = taken_manhole_ids(db, night)
    eligible = latest_eligible_points(db)
    assets = {
        m.id: m
        for m in db.execute(select(ManholeAsset).where(ManholeAsset.status == "active")).scalars().all()
    }
    points = [eligible[mid] for mid in eligible if mid in assets and mid not in taken]
    cluster_assignments = []
    single_assignments = []
    assigned = set()
    for window in windows:
        boundary = window.boundary
        inside = [
            p for p in points
            if p.manhole_id not in assigned
            and boundary["min_x"] <= p.x <= boundary["max_x"]
            and boundary["min_y"] <= p.y <= boundary["max_y"]
        ]
        if not inside:
            continue
        matrix = np.array([[p.x, p.y] for p in inside], dtype=float)
        labels = DBSCAN(eps=EPS_METERS, min_samples=MIN_SAMPLES).fit_predict(matrix)
        for label in sorted(set(int(x) for x in labels)):
            members = [p for p, current in zip(inside, labels) if int(current) == label]
            if label == -1:
                for point in members:
                    if point.settlement_mm > SINGLE_SETTLEMENT_THRESHOLD_MM and point.manhole_id not in assigned:
                        assigned.add(point.manhole_id)
                        single_assignments.append((window, point, None))
                continue
            cluster_label = f"{window.window_code}-C{label + 1}"
            for point in members:
                if point.manhole_id in assigned:
                    continue
                assigned.add(point.manhole_id)
                cluster_assignments.append((window, point, cluster_label))
    plans = []
    for kind, rows in (("cluster", cluster_assignments), ("single", single_assignments)):
        if not rows:
            continue
        seq = len(db.execute(
            select(SchedulePlan).where(SchedulePlan.night_date == night, SchedulePlan.kind == kind)
        ).scalars().all()) + 1
        plan = SchedulePlan(
            plan_code=f"PLN-{night.replace('-', '')}-{'CL' if kind == 'cluster' else 'SG'}-{seq}",
            night_date=night,
            kind=kind,
            status="pending",
            created_by=user.id,
        )
        db.add(plan)
        db.flush()
        for window, point, cluster_label in rows:
            db.add(ScheduleAssignment(
                plan_id=plan.id,
                window_id=window.id,
                night_date=night,
                manhole_id=point.manhole_id,
                point_id=point.id,
                kind=kind,
                cluster_label=cluster_label,
                settlement_mm=point.settlement_mm,
                status="planned",
            ))
        plans.append(plan)
    if not plans:
        raise HTTPException(409, "窗口覆盖范围内没有可排班的成片井盖或沉降超 8 毫米的散点")
    record(db, user.id, "generate_night_schedule", "SchedulePlan", night, {
        "plans": [p.plan_code for p in plans],
        "cluster_assignments": len(cluster_assignments),
        "single_assignments": len(single_assignments),
    })
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("night schedule generation conflict: %s", exc)
        raise HTTPException(409, "排班数据冲突，请刷新后重试")
    return schedule_payload(db, night)


def confirm_one_plan(db, plan, now):
    """单批一个事务：任一窗口名额不够则整批退回待排。"""
    locked_plan = db.execute(
        select(SchedulePlan).where(SchedulePlan.id == plan.id).with_for_update()
    ).scalar_one()
    if locked_plan.status not in ("pending", "returned"):
        return {"plan_id": locked_plan.id, "plan_code": locked_plan.plan_code, "kind": locked_plan.kind,
                "status": locked_plan.status, "note": "该批已处理，未重复占名额"}
    assignments = [a for a in locked_plan.assignments if a.status == "planned"]
    by_window = {}
    for assignment in assignments:
        by_window.setdefault(assignment.window_id, []).append(assignment)
    shortages = []
    windows = {}
    for window_id, rows in by_window.items():
        window = db.execute(select(NightWindow).where(NightWindow.id == window_id).with_for_update()).scalar_one()
        windows[window_id] = window
        confirmed = len(db.execute(
            select(ScheduleAssignment).where(
                ScheduleAssignment.window_id == window_id,
                ScheduleAssignment.night_date == locked_plan.night_date,
                ScheduleAssignment.status == "confirmed",
            )
        ).scalars().all())
        need = len(rows)
        if confirmed + need > window.quota:
            shortages.append({"window_code": window.window_code, "need": need, "available": max(window.quota - confirmed, 0)})
    if shortages:
        locked_plan.status = "returned"
        locked_plan.return_reason = "；".join(
            f"窗口 {s['window_code']} 名额不足：需 {s['need']}，剩 {s['available']}" for s in shortages
        )
        return {"plan_id": locked_plan.id, "plan_code": locked_plan.plan_code, "kind": locked_plan.kind,
                "status": "returned", "reason": locked_plan.return_reason, "shortages": shortages}
    for assignment in assignments:
        assignment.status = "confirmed"
    locked_plan.status = "confirmed"
    locked_plan.return_reason = None
    locked_plan.confirmed_at = now
    return {"plan_id": locked_plan.id, "plan_code": locked_plan.plan_code, "kind": locked_plan.kind,
            "status": "confirmed", "confirmed": len(assignments)}


@router.post("/night-schedule/confirm")
def confirm_plans(payload: NightConfirmIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    night = payload.night_date
    existing = db.execute(
        select(ConfirmReceipt).where(ConfirmReceipt.idempotency_key == payload.idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return {**existing.response, "replayed": True}
    query = select(SchedulePlan).where(
        SchedulePlan.night_date == night,
        SchedulePlan.status.in_(["pending", "returned"]),
    )
    if payload.plan_ids:
        query = query.where(SchedulePlan.id.in_(payload.plan_ids))
    plans = db.execute(query).scalars().all()
    kind_order = {"cluster": 0, "single": 1}
    plans.sort(key=lambda p: (kind_order.get(p.kind, 9), p.id))
    if payload.plan_ids and len(plans) != len(set(payload.plan_ids)):
        raise HTTPException(409, "部分批次不存在或不在待确认状态")
    now = datetime.now(timezone.utc)
    results = []
    for plan in plans:
        try:
            result = confirm_one_plan(db, plan, now)
            record(db, user.id, "confirm_night_schedule", "SchedulePlan", result["plan_code"],
                   {"status": result["status"], "idempotency_key": payload.idempotency_key})
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.exception("confirm plan %s failed", plan.plan_code)
            raise HTTPException(500, f"批次 {plan.plan_code} 确认失败：{exc}")
        results.append(result)
    response = {"night": night, "replayed": False, "results": results,
                "message": "没有待确认的批次" if not results else None}
    receipt = ConfirmReceipt(idempotency_key=payload.idempotency_key, night_date=night, actor_id=user.id, response=response)
    db.add(receipt)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        stored = db.execute(
            select(ConfirmReceipt).where(ConfirmReceipt.idempotency_key == payload.idempotency_key)
        ).scalar_one()
        return {**stored.response, "replayed": True}
    return response

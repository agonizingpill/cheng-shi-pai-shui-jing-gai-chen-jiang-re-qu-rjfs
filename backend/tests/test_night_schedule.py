"""夜间封道排班：名额、成片/散点、整批退回、幂等回执的端到端规则测试。"""

import os
import uuid
from datetime import date, timedelta

DB_PATH = "/tmp/manhole_night_schedule_test.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SEED_DEMO"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402

TODAY = date.today().isoformat()
PLUS1 = (date.today() + timedelta(days=1)).isoformat()
PLUS2 = (date.today() + timedelta(days=2)).isoformat()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth(client):
    def login(username):
        response = client.post("/api/auth/login", json={"username": username, "password": f"{username}123"})
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['access_token']}"}
    return {"supervisor": login("supervisor"), "operator": login("operator"), "viewer": login("viewer")}


def key():
    return f"test-{uuid.uuid4().hex}"


def schedule(client, headers, night):
    response = client.get(f"/api/night-schedule?night={night}", headers=headers)
    assert response.status_code == 200
    return response.json()


def window_of(data, suffix):
    return next(w for w in data["windows"] if w["window_code"].endswith(suffix))


def plans_by_kind(data):
    return {p["kind"]: p for p in data["plans"] if p["status"] != "superseded"}


def test_01_seeded_windows_and_whereabouts(client, auth):
    data = schedule(client, auth["viewer"], TODAY)
    assert len(data["windows"]) == 2
    assert window_of(data, "-NH")["quota"] == 12
    assert window_of(data, "-XH")["quota"] == 10
    assert data["plans"] == []
    assert len(data["manholes"]) == 23
    mh20 = next(m for m in data["manholes"] if m["asset_code"] == "MH-020")
    assert mh20["status"] == "unscheduled"
    assert "基准" in mh20["whereabouts"]
    assert all(m["status"] == "unscheduled" for m in data["manholes"])


def test_02_mutation_requires_supervisor(client, auth):
    window_id = schedule(client, auth["viewer"], TODAY)["windows"][0]["id"]
    assert client.put(f"/api/night-windows/{window_id}/quota", json={"quota": 5}, headers=auth["operator"]).status_code == 403
    assert client.post("/api/night-schedule/plan", json={"night_date": TODAY}, headers=auth["operator"]).status_code == 403
    assert client.post("/api/night-schedule/confirm", json={"night_date": TODAY, "idempotency_key": key()}, headers=auth["viewer"]).status_code == 403
    assert client.post("/api/night-windows", json={"window_code": "NW-X", "night_date": TODAY, "road_name": "测试路", "starts_at": f"{TODAY}T22:00:00Z", "ends_at": f"{TODAY}T23:00:00Z", "boundary": {"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1}, "quota": 1}, headers=auth["operator"]).status_code == 403


def test_03_plan_generates_cluster_and_single_batches(client, auth):
    response = client.post("/api/night-schedule/plan", json={"night_date": TODAY}, headers=auth["supervisor"])
    assert response.status_code == 201
    data = response.json()
    plans = plans_by_kind(data)
    assert set(plans) == {"cluster", "single"}
    cluster, single = plans["cluster"], plans["single"]
    nh_code = window_of(data, "-NH")["window_code"]
    xh_code = window_of(data, "-XH")["window_code"]
    nh_cluster = [a for a in cluster["assignments"] if a["window_code"] == nh_code]
    xh_cluster = [a for a in cluster["assignments"] if a["window_code"] == xh_code]
    assert len(nh_cluster) == 10
    assert len(xh_cluster) == 9  # MH-020 基准冲突，未入排班
    assert {a["cluster_label"] for a in nh_cluster} == {f"{nh_code}-C1"}
    assert all(a["kind"] == "cluster" for a in cluster["assignments"])
    # 沉降超 8mm 的散点单排；MH-023 仅 6.4mm 不排
    assert {a["asset_code"] for a in single["assignments"]} == {"MH-021", "MH-022"}
    assert all(a["window_code"] == nh_code for a in single["assignments"])
    scheduled = [a["manhole_id"] for p in plans.values() for a in p["assignments"]]
    assert len(scheduled) == len(set(scheduled)) == 21  # 同一井盖同晚只排一次


def test_04_confirm_occupies_quota_and_replay_does_not(client, auth):
    idem = key()
    first = client.post("/api/night-schedule/confirm", json={"night_date": TODAY, "idempotency_key": idem}, headers=auth["supervisor"])
    assert first.status_code == 200
    body = first.json()
    assert body["replayed"] is False
    statuses = {r["kind"]: r["status"] for r in body["results"]}
    assert statuses == {"cluster": "confirmed", "single": "confirmed"}
    data = schedule(client, auth["viewer"], TODAY)
    assert window_of(data, "-NH")["confirmed_count"] == 12
    assert window_of(data, "-XH")["confirmed_count"] == 9
    assert window_of(data, "-NH")["remaining"] == 0
    # 回执丢失后用同一幂等键重试：返回已存回执，名额不重复占用
    replay = client.post("/api/night-schedule/confirm", json={"night_date": TODAY, "idempotency_key": idem}, headers=auth["supervisor"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["results"] == body["results"]
    again = schedule(client, auth["viewer"], TODAY)
    assert window_of(again, "-NH")["confirmed_count"] == 12
    assert window_of(again, "-XH")["confirmed_count"] == 9
    # 新幂等键也没有待确认批次，不会重复占名额
    third = client.post("/api/night-schedule/confirm", json={"night_date": TODAY, "idempotency_key": key()}, headers=auth["supervisor"])
    assert third.status_code == 200
    assert third.json()["results"] == []
    assert window_of(schedule(client, auth["viewer"], TODAY), "-NH")["confirmed_count"] == 12


def make_window(client, headers, code, night, quota, boundary):
    payload = {"window_code": code, "night_date": night, "road_name": f"{code} 路段",
               "starts_at": f"{night}T22:00:00Z", "ends_at": f"{night}T23:30:00Z",
               "boundary": boundary, "quota": quota}
    response = client.post("/api/night-windows", json=payload, headers=headers)
    assert response.status_code == 201
    return response.json()


def test_05_shortage_returns_whole_batch_atomically(client, auth):
    supervisor = auth["supervisor"]
    boundary_nh = {"min_x": 1000, "max_x": 1095, "min_y": 1935, "max_y": 2005}
    boundary_xh = {"min_x": 1100, "max_x": 1155, "min_y": 1935, "max_y": 2005}
    nh = make_window(client, supervisor, f"NW-{PLUS1.replace('-', '')}-NH", PLUS1, 3, boundary_nh)
    make_window(client, supervisor, f"NW-{PLUS1.replace('-', '')}-XH", PLUS1, 10, boundary_xh)
    assert client.post("/api/night-schedule/plan", json={"night_date": PLUS1}, headers=supervisor).status_code == 201
    result = client.post("/api/night-schedule/confirm", json={"night_date": PLUS1, "idempotency_key": key()}, headers=supervisor).json()
    statuses = {r["kind"]: r["status"] for r in result["results"]}
    assert statuses == {"cluster": "returned", "single": "confirmed"}
    returned = next(r for r in result["results"] if r["kind"] == "cluster")
    assert nh["window_code"] in returned["reason"]
    data = schedule(client, supervisor, PLUS1)
    # 整批退回：成片批在任何窗口都没有占名额，哪怕西环路窗口名额足够
    assert window_of(data, "-XH")["confirmed_count"] == 0
    assert window_of(data, "-NH")["confirmed_count"] == 2
    cluster_plan = plans_by_kind(data)["cluster"]
    assert cluster_plan["status"] == "returned"
    assert all(a["status"] == "planned" for a in cluster_plan["assignments"])
    # 提高名额后，退回批次可随下一次两批提交确认
    nh_id = window_of(data, "-NH")["id"]
    assert client.put(f"/api/night-windows/{nh_id}/quota", json={"quota": 12}, headers=supervisor).status_code == 200
    retry = client.post("/api/night-schedule/confirm", json={"night_date": PLUS1, "idempotency_key": key()}, headers=supervisor).json()
    assert {r["kind"]: r["status"] for r in retry["results"]} == {"cluster": "confirmed"}
    data = schedule(client, supervisor, PLUS1)
    assert window_of(data, "-NH")["confirmed_count"] == 12
    assert window_of(data, "-XH")["confirmed_count"] == 9


def test_06_datum_conflict_point_waits_for_correction(client, auth):
    supervisor = auth["supervisor"]
    boundary_nh = {"min_x": 1000, "max_x": 1095, "min_y": 1935, "max_y": 2005}
    boundary_xh = {"min_x": 1100, "max_x": 1155, "min_y": 1935, "max_y": 2005}
    make_window(client, supervisor, f"NW-{PLUS2.replace('-', '')}-NH", PLUS2, 20, boundary_nh)
    make_window(client, supervisor, f"NW-{PLUS2.replace('-', '')}-XH", PLUS2, 20, boundary_xh)
    client.post("/api/night-schedule/plan", json={"night_date": PLUS2}, headers=supervisor)
    data = schedule(client, supervisor, PLUS2)
    cluster = plans_by_kind(data)["cluster"]
    assert len([a for a in cluster["assignments"] if a["window_code"].endswith("-XH")]) == 9
    # 修正 SP-1121-20 的坐标基准后重排，西环路成片恢复 10 口
    dashboard = client.get("/api/dashboard", headers=auth["operator"]).json()
    point_id = next(p["id"] for p in dashboard["points"] if p["point_code"] == "SP-1121-20")
    assert client.post(f"/api/survey-points/{point_id}/correct-datum", headers=auth["operator"]).status_code == 200
    client.post("/api/night-schedule/plan", json={"night_date": PLUS2}, headers=supervisor)
    data = schedule(client, supervisor, PLUS2)
    cluster = plans_by_kind(data)["cluster"]
    assert len([a for a in cluster["assignments"] if a["window_code"].endswith("-XH")]) == 10
    mh20 = next(m for m in data["manholes"] if m["asset_code"] == "MH-020")
    assert mh20["status"] == "planned"
    assert mh20["window_code"].endswith("-XH")


def test_07_window_validation(client, auth):
    supervisor = auth["supervisor"]
    bad = {"window_code": "NW-BAD-1", "night_date": PLUS2, "road_name": "测试路",
           "starts_at": f"{PLUS2}T22:00:00Z", "ends_at": f"{PLUS2}T23:00:00Z",
           "boundary": {"min_x": 0}, "quota": 1}
    assert client.post("/api/night-windows", json=bad, headers=supervisor).status_code == 422
    dup = make_window(client, supervisor, "NW-DUP-1", PLUS2, 1, {"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1})
    assert dup["window_code"] == "NW-DUP-1"
    bad["window_code"] = "NW-DUP-1"
    bad["boundary"] = {"min_x": 0, "max_x": 1, "min_y": 0, "max_y": 1}
    assert client.post("/api/night-windows", json=bad, headers=supervisor).status_code == 409

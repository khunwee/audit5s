"""ชุดทดสอบของรุ่น 1.5: กล้องหลายตัวต่อแผนก ตารางเวลา 3 ชั้น (ค่ากลาง แผนก กล้อง) โปรแกรมกล้องโหมดตามตาราง
ตัวตั้งเวลาของกล้องแบบ direct รอบที่เปิดปิดเองและสร้างรอบถัดไปเอง และการแจ้งเมื่อกล้องไม่ส่งภาพ
"""
import importlib.util
import io
import os
import random
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v15_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import cameras, db as dbm, rounds_auto, schedule, scheduler, scoring, settings_store, worker  # noqa: E402
from app.main import app  # noqa: E402

spec = importlib.util.spec_from_file_location("camera_agent", Path(__file__).parent.parent / "tools" / "camera_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

ADMIN_PW = "Factory5S2026"
TUE = date(2026, 10, 6)            # วันอังคาร
S = {"cam_ok": True}
CLOCK = {"now": datetime(2026, 10, 6, 10, 30), "day": TUE}


def jpeg(seed=None) -> bytes:
    rnd = random.Random(seed if seed is not None else random.randrange(10 ** 9))
    im = Image.new("RGB", (1100, 800), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    for _ in range(30):
        x, y = rnd.randrange(1000), rnd.randrange(700)
        im.paste((rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)), (x, y, x + 80, y + 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def cam_handler(request: httpx.Request) -> httpx.Response:
    if not S["cam_ok"]:
        return httpx.Response(500)
    return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})


@pytest.fixture(scope="module", autouse=True)
def wiring():
    old = (cameras._transport, schedule.thai_now, rounds_auto.today, agent.grab, agent.STATE_FILE)
    cameras._transport = httpx.MockTransport(cam_handler)
    schedule.thai_now = lambda: CLOCK["now"]
    rounds_auto.today = lambda: CLOCK["day"]
    agent.grab = lambda cam: jpeg() if S["cam_ok"] else (_ for _ in ()).throw(RuntimeError("กล้องไม่ตอบ"))
    agent.STATE_FILE = os.path.join(tempfile.mkdtemp(prefix="agent_"), "agent_state.json")
    yield
    cameras._transport, schedule.thai_now, rounds_auto.today, agent.grab, agent.STATE_FILE = old


def client(username, password, new_password=None):
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"username": username, "password": password}).headers["location"] == "/"
    if c.get("/").headers.get("location") == "/account/password":
        c.post("/account/password", data={"current": password, "new": new_password, "confirm": new_password})
    assert c.get("/").status_code == 200
    return c


def admin():
    with dbm.SessionLocal() as s:
        fresh = s.get(dbm.User, 1).must_change
    return client("admin", "admin1234", ADMIN_PW) if fresh else client("admin", ADMIN_PW)


def save(**kw):
    with dbm.SessionLocal() as s:
        settings_store.save(s, kw)


def drain():
    n = 0
    while worker.process_one() and n < 200:
        n += 1


def at(hh, mm, day=TUE):
    CLOCK["now"], CLOCK["day"] = datetime(day.year, day.month, day.day, hh, mm), day


class FakeApi:
    """โปรแกรมกล้องคุยกับระบบผ่าน TestClient แทนเครือข่ายจริง"""

    def __init__(self):
        self.c = TestClient(app)
        self.h = {"X-Agent-Token": settings_store.load()["agent_token"]}

    def poll(self):
        r = self.c.get("/api/agent/poll", headers=self.h)
        if r.status_code != 200:
            raise RuntimeError(f"ระบบตอบกลับ {r.status_code}")
        return r.json()

    def upload(self, camera_id, jpeg=None, error="", scheduled=""):
        data = {"camera_id": camera_id, "scheduled": scheduled}
        if error:
            data["error"] = error
        r = self.c.post("/api/agent/upload", headers=self.h, data=data,
                        files={"file": ("camera.jpg", jpeg, "image/jpeg")} if jpeg else None)
        if r.status_code != 200:
            raise RuntimeError(f"ระบบตอบกลับ {r.status_code}: {r.json().get('detail')}")
        return r.json()


def cam_form(name, dept, area, mode="agent", **extra):
    return dict(name=name, department_id=dept, area_name=area, area_type="สายการผลิต", mode=mode, source="snapshot",
                url=f"http://10.9.9.{random.randrange(2, 250)}/snap.jpg", **extra)


def cam(name):
    with dbm.SessionLocal() as s:
        return s.query(dbm.Camera).filter_by(name=name).one()


def plan():
    return S["api"].poll()["plan"]


def photos_of(camera_id):
    with dbm.SessionLocal() as s:
        return s.query(dbm.Photo).filter_by(camera_id=camera_id).order_by(dbm.Photo.id).all()


def test_01_several_cameras_per_department():
    a = admin()
    S["admin"] = a
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status.in_(["open", "planned"])):
            r.status = "closed"
        for c in s.query(dbm.Camera).all():
            c.active = False
        for d in s.query(dbm.Department).all():
            d.active = False
        s.query(dbm.Channel).delete()
        s.commit()
    cameras.invalidate()
    scheduler.done.clear()
    for code, name in (("K1", "ขึ้นรูปโลหะ"), ("K2", "พ่นสี")):
        a.post("/admin/departments/save", data={"code": code, "name": name})
    with dbm.SessionLocal() as s:
        S["d"] = {d.code: d.id for d in s.query(dbm.Department).filter(dbm.Department.code.in_(["K1", "K2"]))}
    d = S["d"]
    save(ai1_type="demo", ai2_type="none", ai_passes=1, ai_daily=100000, ranking_visibility="always", member_see_all=True,
         verify_required=False, require_coverage=False, allow_free_area=True, max_photos_per_dept=0, storage_budget_mb=350,
         retention_days=0, area_types=["สายการผลิต", "คลัง"], rounds_repeat="off", cam_holidays=[], cam_grace_min=20,
         cam_schedule={"times": [], "random": 0, "between": "08:30-16:30", "days": [0, 1, 2, 3, 4]}, auto_actions=False,
         **{k: "" for k in settings_store.load() if k.startswith("_alert_")})
    a.post("/admin/areas/save", data=dict(department_id=d["K1"], name="ไลน์ A", area_type="สายการผลิต", required="1", active="1"))
    assert a.post("/admin/cameras/save", data=cam_form("กล้อง A1", d["K1"], "ไลน์ A")).status_code == 303
    a.post("/admin/cameras/save", data=cam_form("กล้อง A2", d["K1"], "ไลน์ B"))
    a.post("/admin/cameras/save", data=cam_form("กล้อง A3", d["K1"], "คลังย่อย", mode="direct"))
    a.post("/admin/cameras/save", data=cam_form("กล้อง B1", d["K2"], "ห้องพ่นสี"))
    a.post("/admin/cameras/token")
    assert a.post("/admin/rounds/save", data={"name": "รอบกล้อง 1.5", "min_photos": 1, "mode": "level"}).status_code == 303
    with dbm.SessionLocal() as s:
        S["rid"] = s.query(dbm.Round).filter_by(name="รอบกล้อง 1.5").one().id
        assert s.query(dbm.Camera).filter_by(department_id=d["K1"], active=True).count() == 3
    S["api"] = FakeApi()
    data = S["api"].poll()
    assert [(c["name"], c["department"], c["area"]) for c in data["cameras"]] == \
        [("กล้อง A1", "ขึ้นรูปโลหะ", "ไลน์ A"), ("กล้อง A2", "ขึ้นรูปโลหะ", "ไลน์ B"), ("กล้อง B1", "พ่นสี", "ห้องพ่นสี")]
    assert data["open_round"] is True and data["date"] == "2026-10-06" and data["now"] == "10:30"
    assert len(data["plan"]) == 3 and all(v == [] for v in data["plan"].values())      # ยังไม่ได้ตั้งตาราง
    page = a.get("/admin/cameras").text
    assert "3 กล้อง" in page and "1 กล้อง" in page and "กล้องที่เปิดใช้ 4 ตัว, ถ่ายอัตโนมัติตามตาราง 0 ตัว" in page
    assert "ไม่ถ่ายอัตโนมัติ" in page and 'name="sched_mode"' in page and "ตารางเวลาค่ากลาง" in page
    # ภาพจากกล้องแต่ละตัวเข้าแผนกและจุดตรวจของกล้องนั้น
    for name in ("กล้อง A1", "กล้อง A2", "กล้อง B1"):
        assert S["api"].upload(cam(name).id, jpeg=jpeg())["ok"] is True
    p = photos_of(cam("กล้อง A1").id)[0]
    assert (p.department_id, p.area_name, p.source) == (d["K1"], "ไลน์ A", "agent") and p.area_id is not None
    assert photos_of(cam("กล้อง B1").id)[0].department_id == d["K2"]
    drain()
    with dbm.SessionLocal() as s:
        rk = scoring.round_ranking(s, s.get(dbm.Round, S["rid"]))
        row = scoring.find_row(rk, d["K1"])
        assert row["scored"] == 2 and row["areas_covered"] == 1          # สองกล้องของแผนกเดียวกันถูกเฉลี่ยรวมกัน


def test_02_schedules_default_department_camera():
    a, d = S["admin"], S["d"]
    a1, a2, a3, b1 = (cam(n).id for n in ("กล้อง A1", "กล้อง A2", "กล้อง A3", "กล้อง B1"))
    days = [str(i) for i in range(7)]
    assert a.post("/admin/cameras/schedule", data={"sched_times": "9:00", "sched_random": 0, "sched_between": "08:30-16:30",
                                                   "sched_days": days, "holidays": "", "grace": 20}).status_code == 303
    assert plan() == {str(a1): ["09:00"], str(a2): ["09:00"], str(b1): ["09:00"]}          # ทุกกล้องใช้ค่ากลาง
    # ตารางของแผนก K1 ทับค่ากลาง
    base = {"code": "K1", "name": "ขึ้นรูปโลหะ", "id": d["K1"]}
    a.post("/admin/departments/save", data=dict(base, cam_sched_mode="own", sched_times="08:00, 13:00", sched_random=0,
                                                sched_between="08:30-16:30", sched_days=days))
    assert plan() == {str(a1): ["08:00", "13:00"], str(a2): ["08:00", "13:00"], str(b1): ["09:00"]}
    # ตารางของกล้องเองทับตารางของแผนก: เวลาคงที่ + สุ่ม 3 ครั้ง
    c = cam("กล้อง A1")
    own = dict(id=a1, name=c.name, department_id=d["K1"], area_name=c.area_name, area_type="สายการผลิต", mode="agent",
               source="snapshot", url=c.url, sched_mode="own", sched_times="07:30", sched_random=3,
               sched_between="10:00-12:00", sched_days=days)
    assert a.post("/admin/cameras/save", data=own).status_code == 303
    times = plan()[str(a1)]
    rand = [t for t in times if t != "07:30"]
    assert len(times) == 4 and len(rand) == 3 and all("10:00" <= t < "12:00" for t in rand) and times == sorted(times)
    assert plan()[str(a1)] == times                                         # ถามซ้ำได้เวลาเดิม โปรแกรมกล้องเปิดใหม่ก็ไม่ถ่ายซ้ำ
    other = schedule.times_for(a1, cam("กล้อง A1").schedule, TUE + timedelta(days=1))
    assert [t for t in other if t != "07:30"] != rand                       # วันถัดไปสุ่มใหม่
    assert schedule.times_for(a1 + 100, cam("กล้อง A1").schedule, TUE) != times     # กล้องแต่ละตัวได้เวลาสุ่มต่างกัน
    # ปิดการถ่ายอัตโนมัติเฉพาะกล้อง
    c2 = cam("กล้อง A2")
    a.post("/admin/cameras/save", data=dict(id=a2, name=c2.name, department_id=d["K1"], area_name=c2.area_name, area_type="สายการผลิต",
                                            mode="agent", source="snapshot", url=c2.url, sched_mode="off"))
    assert plan()[str(a2)] == []
    page = a.get("/admin/cameras").text
    assert "เวลา 07:30 และสุ่ม 3 ครั้งระหว่าง 10:00-12:00 (ทุกวัน)" in page and "(ตารางของกล้องนี้)" in page
    assert "(ปิดเฉพาะกล้องนี้)" in page and "(ค่ากลาง)" in page and "ตารางของแผนก: เวลา 08:00, 13:00" in page
    shown = page.replace("10:00-12:00", "")                                  # ช่วงเวลาที่ตั้งไว้แสดงได้ แต่เวลาสุ่มจริงต้องไม่แสดง
    assert not any(t in shown for t in rand if t not in ("09:30", "14:30", "08:30", "16:30"))
    assert "ถ่ายอัตโนมัติตามตาราง 3 ตัว" in page                              # A1, A3 (ตามแผนก), B1
    # วันในสัปดาห์และวันหยุด
    a.post("/admin/cameras/schedule", data={"sched_times": "09:00", "sched_days": ["0", "1", "2", "3", "4"], "grace": 20,
                                            "holidays": "2026-10-13\nไม่ใช่วันที่\n2026-12-31"})
    assert settings_store.load()["cam_holidays"] == ["2026-10-13", "2026-12-31"]
    at(10, 30, date(2026, 10, 10))                                           # วันเสาร์
    assert plan()[str(b1)] == [] and len(plan()[str(a1)]) == 4               # B1 ตามค่ากลาง จ.-ศ., A1 ตั้งเองทุกวัน
    at(10, 30, date(2026, 10, 13))                                           # วันหยุดที่ตั้งไว้: ไม่ถ่ายทุกกล้อง
    assert all(v == [] for v in plan().values())
    at(10, 30)
    # รายการกล้องของโปรแกรมกล้องเก็บในหน่วยความจำ และถูกล้างเมื่อแก้การตั้งค่า
    cache = cameras.agent["cache"]
    plan()
    assert cameras.agent["cache"] is cache
    a.post("/admin/cameras/schedule", data={"sched_times": "09:00", "sched_days": ["0", "1", "2", "3", "4"], "grace": 20, "holidays": ""})
    assert cameras.agent["cache"] is None
    S.update(a1=a1, a2=a2, a3=a3, b1=b1, rand=rand)


def test_03_agent_follows_the_server_schedule():
    api, a1, b1 = S["api"], S["a1"], S["b1"]
    n_a, n_b = len(photos_of(a1)), len(photos_of(b1))
    at(9, 10)
    state = {}
    res = agent.auto_step(api, state, datetime(2026, 10, 6, 9, 10))
    assert ("กล้อง B1", "09:00", "sent") in res and ("กล้อง A1", "07:30", "late") in res and len(res) == 2
    shot = photos_of(b1)[-1]
    assert len(photos_of(b1)) == n_b + 1 and "ถ่ายตามตาราง 09:00" in shot.note and shot.source == "agent"
    assert len(photos_of(a1)) == n_a                                          # 07:30 เลยมา 100 นาที ไม่ถ่ายย้อนหลัง
    assert agent.auto_step(api, state, datetime(2026, 10, 6, 9, 11)) == []
    # ปิดโปรแกรมแล้วเปิดใหม่ในช่วงที่ยังชดเชยได้: ไม่ถ่ายซ้ำ
    at(9, 15)
    assert agent.auto_step(api, {}, datetime(2026, 10, 6, 9, 15)) == [] and len(photos_of(b1)) == n_b + 1
    # ถึงเวลาสุ่มของกล้อง A1
    hh, mm = map(int, S["rand"][0].split(":"))
    at(hh, mm)
    res = agent.auto_step(api, state, datetime(2026, 10, 6, hh, mm))
    assert res == [("กล้อง A1", S["rand"][0], "sent")] and len(photos_of(a1)) == n_a + 1
    assert photos_of(a1)[-1].area_name == "ไลน์ A" and f"ถ่ายตามตาราง {S['rand'][0]}" in photos_of(a1)[-1].note
    # นาฬิกาของเครื่องตั้งเขตเวลาผิด (ช้ากว่าเวลาไทย 7 ชั่วโมง): ใช้เวลาของระบบ
    hh, mm = map(int, S["rand"][1].split(":"))
    at(hh, mm)
    wrong = datetime(2026, 10, 6, hh, mm) - timedelta(hours=7)
    st = {}
    res = agent.auto_step(api, st, wrong)
    assert st["offset"] == timedelta(hours=7) and (("กล้อง A1", S["rand"][1], "sent") in res)
    assert agent.server_clock({"date": "2026-10-06", "now": "10:00"}, datetime(2026, 10, 6, 10, 1, 30)) == timedelta(0)
    # กล้องไม่ตอบ: บันทึกข้อผิดพลาดและแจ้งผู้ดูแล
    hh, mm = map(int, S["rand"][2].split(":"))
    at(hh, mm)
    S["cam_ok"] = False
    res = agent.auto_step(api, {}, datetime(2026, 10, 6, hh, mm))            # โปรแกรมเปิดใหม่: อ่านสิ่งที่ถ่ายไปแล้วจากไฟล์
    S["cam_ok"] = True
    assert res == [("กล้อง A1", S["rand"][2], "failed")] and "กล้องไม่ตอบ" in cam("กล้อง A1").last_error
    with dbm.SessionLocal() as s:
        alert = s.query(dbm.AuditLog).filter_by(action="alert").order_by(dbm.AuditLog.id.desc()).first()
        assert "กล้อง A1 (ขึ้นรูปโลหะ) ถ่ายตามตารางไม่สำเร็จ" in alert.detail
    # ไม่มีรอบที่เปิดรับภาพ: ไม่ถ่าย
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "closed"
        s.commit()
    cameras.invalidate()
    at(9, 5, date(2026, 10, 7))
    res = agent.auto_step(api, {}, datetime(2026, 10, 7, 9, 5))
    assert ("กล้อง B1", "09:00", "closed") in res and len(photos_of(b1)) == n_b + 1
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "open"
        s.commit()
    cameras.invalidate()
    at(10, 30)


def test_04_server_scheduler_for_direct_cameras():
    a, d, a3 = S["admin"], S["d"], S["a3"]
    c = cam("กล้อง A3")
    form = dict(id=a3, name=c.name, department_id=d["K1"], area_name=c.area_name, area_type="คลัง", mode="direct",
                source="snapshot", url=c.url, sched_mode="own", sched_times="14:00, 14:30", sched_random=0,
                sched_days=[str(i) for i in range(7)])
    assert a.post("/admin/cameras/save", data=form).status_code == 303
    scheduler.done.clear()
    assert scheduler.tick(datetime(2026, 10, 6, 13, 59)) == dict(captured=[], failed=[], skipped=[])
    out = scheduler.tick(datetime(2026, 10, 6, 14, 5))
    assert [(x[0], x[1]) for x in out["captured"]] == [(a3, "14:00")] and out["failed"] == []
    p = photos_of(a3)[-1]
    assert (p.department_id, p.source, p.area_name) == (d["K1"], "ipcam", "คลังย่อย") and "ถ่ายตามตาราง 14:00" in p.note
    assert scheduler.tick(datetime(2026, 10, 6, 14, 6))["captured"] == []     # ไม่ถ่ายซ้ำ
    assert scheduler.tick(datetime(2026, 10, 6, 15, 10))["skipped"] == [(a3, "14:30")]     # เลยมา 40 นาที ข้าม
    # กล้องเสีย: บันทึกและแจ้งผู้ดูแล แล้วทำงานต่อได้ในวันถัดไป
    save(**{f"_alert_camera_{a3}": ""})
    S["cam_ok"] = False
    out = scheduler.tick(datetime(2026, 10, 7, 14, 1))
    S["cam_ok"] = True
    assert out["failed"] == [(a3, "14:00")] and "กล้องตอบกลับ 500" in cam("กล้อง A3").last_error
    with dbm.SessionLocal() as s:
        assert "กล้อง A3" in s.query(dbm.AuditLog).filter_by(action="alert").order_by(dbm.AuditLog.id.desc()).first().detail
    assert [(x[0], x[1]) for x in scheduler.tick(datetime(2026, 10, 7, 14, 31))["captured"]] == [(a3, "14:30")]
    assert cam("กล้อง A3").last_error == ""
    # กล้องแบบ agent ไม่ถูกถ่ายโดยเซิร์ฟเวอร์ และไม่มีรอบเปิดก็ไม่ถ่าย
    assert all(cid == a3 for cid, _ in cameras.direct["cache"])
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "closed"
        s.commit()
    assert scheduler.tick(datetime(2026, 10, 8, 14, 2))["skipped"] == [(a3, "14:00")]
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "open"
        s.commit()
    # ปุ่มสั่งถ่ายจากหน้าเว็บยังใช้ได้
    r = a.post(f"/api/cameras/{a3}/capture", data={"round_id": S["rid"]})
    assert r.status_code == 200 and r.json()["area"] == "คลังย่อย"
    drain()
    with dbm.SessionLocal() as s:
        row = scoring.find_row(scoring.round_ranking(s, s.get(dbm.Round, S["rid"])), d["K1"])
        assert row["total"] == 7 and row["scored"] >= 1 and row["avg"] is not None      # ภาพจากกล้อง 3 ตัวของแผนกเดียวกันรวมเป็นคะแนนเดียว


def test_05_rounds_open_and_close_by_date():
    a = S["admin"]
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status == "open"):
            r.status, r.end_date = "closed", None
        s.commit()
    at(8, 0, date(2026, 10, 6))
    form = {"name": "รอบวางแผน", "start_date": "2026-10-10", "end_date": "2026-10-16", "min_photos": 2, "mode": "level",
            "auto_open": "1", "auto_close": "1"}
    assert a.post("/admin/rounds/save", data=form).status_code == 303
    with dbm.SessionLocal() as s:
        r = s.query(dbm.Round).filter_by(name="รอบวางแผน").one()
        assert (r.status, r.auto_open, r.auto_close) == ("planned", True, True)
        rid = r.id
    page = a.get("/admin/rounds").text
    assert "รอเปิดวันที่ 10/10/2026" in page and "เปิดรอบตอนนี้" in page and "ปิดเองหลังวันที่ 16/10/2026" in page
    assert "รอบวางแผน" not in a.get("/photos").text and "รอบวางแผน" not in a.get("/ranking").text      # ยังไม่ขึ้นในหน้าของผู้ใช้
    assert "ยังไม่มีรอบการตรวจที่เปิดรับภาพ" in a.get("/capture").text
    r = a.post("/api/photos", data=dict(round_id=rid, department_id=S["d"]["K1"], area_name="x", area_type="สายการผลิต"),
               files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 400                                               # รอบที่ยังไม่เปิดไม่รับภาพ
    assert S["api"].poll()["open_round"] is False

    def status():
        with dbm.SessionLocal() as s:
            return s.get(dbm.Round, rid).status

    at(23, 0, date(2026, 10, 9))
    a.get("/")
    assert status() == "planned"
    at(0, 5, date(2026, 10, 10))                                              # ถึงวันเริ่ม: มีคนเปิดระบบ รอบเปิดเอง
    a.get("/")
    assert status() == "open" and S["api"].poll()["open_round"] is True
    at(23, 50, date(2026, 10, 16))                                            # วันสิ้นสุดยังรับภาพ
    assert a.get("/capture").status_code == 200 and status() == "open"
    at(0, 10, date(2026, 10, 17))                                             # พ้นวันสิ้นสุด: ปิดเอง (ทางตัวตั้งเวลา)
    scheduler.tick(CLOCK["now"])
    assert status() == "closed"
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Round, rid).closed_at is not None
        acts = [x.action for x in s.query(dbm.AuditLog).filter(dbm.AuditLog.action.in_(["auto_open_round", "auto_close_round"]))
                .order_by(dbm.AuditLog.id.desc()).limit(2)]
        assert acts == ["auto_close_round", "auto_open_round"]
    # ไม่ติ๊กปิดเอง: รอบไม่ถูกปิดแม้พ้นวันสิ้นสุด และปิดเองต้องมีวันสิ้นสุด
    a.post("/admin/rounds/save", data={"name": "รอบปิดมือ", "start_date": "2026-10-17", "end_date": "2026-10-18", "min_photos": 1, "mode": "level"})
    a.post("/admin/rounds/save", data={"name": "รอบไม่มีวันจบ", "start_date": "2026-10-17", "min_photos": 1, "mode": "level", "auto_close": "1"})
    at(9, 0, date(2026, 10, 25))
    a.get("/")
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Round).filter_by(name="รอบปิดมือ").one().status == "open"
        endless = s.query(dbm.Round).filter_by(name="รอบไม่มีวันจบ").one()
        assert endless.status == "open" and endless.auto_close is False
        for r in s.query(dbm.Round).filter(dbm.Round.status == "open"):
            r.status = "closed"
        s.commit()
    # เปิดรอบที่รอเปิดด้วยมือ
    a.post("/admin/rounds/save", data=dict(form, name="รอบรอเปิด", start_date="2026-12-01", end_date="2026-12-05"))
    with dbm.SessionLocal() as s:
        wid = s.query(dbm.Round).filter_by(name="รอบรอเปิด").one().id
    assert a.post(f"/admin/rounds/{wid}/reopen").status_code == 303
    with dbm.SessionLocal() as s:
        r = s.get(dbm.Round, wid)
        assert r.status == "open"
        r.status = "closed"
        s.commit()
    S["planned"] = rid


def test_06_recurring_rounds():
    a = S["admin"]
    with dbm.SessionLocal() as s:                     # รอบล่าสุดที่มีวันสิ้นสุดคือ 16/10/2026
        for r in s.query(dbm.Round).filter(dbm.Round.end_date > date(2026, 10, 16)):
            r.end_date = None
        s.commit()
    at(8, 0, date(2026, 10, 17))
    assert a.post("/admin/rounds/auto", data={"rounds_repeat": "weekly", "prefix": "ตรวจ 5ส"}).status_code == 303
    with dbm.SessionLocal() as s:
        r = s.query(dbm.Round).order_by(dbm.Round.id.desc()).first()
        assert (r.name, r.start_date, r.end_date, r.status) == ("ตรวจ 5ส 17/10-23/10/2026", date(2026, 10, 17), date(2026, 10, 23), "open")
        assert (r.auto_close, r.min_photos, r.mode) == (True, 2, "level") and len(r.rubric) >= 1
        n = s.query(dbm.Round).count()
    a.get("/")                                         # เรียกซ้ำในวันเดิม ไม่สร้างรอบซ้ำ
    rounds_auto._seen["day"] = None
    with dbm.SessionLocal() as s:
        assert rounds_auto.apply(s) == dict(opened=[], closed=[], created=[]) and s.query(dbm.Round).count() == n
    # ระบบไม่ได้ทำงานหลายสัปดาห์: ปิดรอบเดิม แล้วสร้างรอบของสัปดาห์ปัจจุบัน ไม่สร้างรอบว่างย้อนหลัง
    at(8, 0, date(2026, 11, 20))
    a.get("/")
    with dbm.SessionLocal() as s:
        last = s.query(dbm.Round).order_by(dbm.Round.id.desc()).first()
        assert (last.name, last.start_date, last.end_date, last.status) == ("ตรวจ 5ส 14/11-20/11/2026", date(2026, 11, 14), date(2026, 11, 20), "open")
        assert s.query(dbm.Round).count() == n + 1
        assert s.query(dbm.Round).filter_by(name="ตรวจ 5ส 17/10-23/10/2026").one().status == "closed"
        last.end_date = date(2026, 11, 30)             # ปรับให้รอบนี้จบสิ้นเดือน เพื่อเริ่มรอบรายเดือนวันที่ 1
        s.commit()
    assert a.post("/admin/rounds/auto", data={"rounds_repeat": "monthly", "prefix": "ตรวจ 5ส ประจำเดือน"}).status_code == 303
    at(7, 0, date(2026, 12, 1))
    a.get("/")
    with dbm.SessionLocal() as s:
        dec = s.query(dbm.Round).order_by(dbm.Round.id.desc()).first()
        assert (dec.name, dec.start_date, dec.end_date, dec.status) == ("ตรวจ 5ส ประจำเดือน ธันวาคม 2569", date(2026, 12, 1), date(2026, 12, 31), "open")
    page = a.get("/admin/rounds").text
    assert 'value="monthly" selected' in page and "ตรวจ 5ส ประจำเดือน ธันวาคม 2569" in page
    a.post("/admin/rounds/auto", data={"rounds_repeat": "off"})
    at(7, 0, date(2027, 1, 5))
    a.get("/")
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Round).order_by(dbm.Round.id.desc()).first().name == "ตรวจ 5ส ประจำเดือน ธันวาคม 2569"      # ปิดการสร้างเองแล้ว
        assert s.query(dbm.Round).filter_by(name="ตรวจ 5ส ประจำเดือน ธันวาคม 2569").one().status == "closed"        # แต่ยังปิดเองตามวันที่


def test_07_stale_camera_alert_and_pages():
    a, d = S["admin"], S["d"]
    at(11, 0, date(2027, 1, 5))                        # วันอังคาร
    a.post("/admin/rounds/save", data={"name": "รอบเฝ้ากล้อง", "min_photos": 1, "mode": "level"})
    with dbm.SessionLocal() as s:
        r = s.query(dbm.Round).filter_by(name="รอบเฝ้ากล้อง").one()
        r.created_at = datetime(2027, 1, 3, 0, 0)       # รอบเปิดมาตั้งแต่ 2 วันก่อน
        b1 = s.query(dbm.Camera).filter_by(name="กล้อง B1").one()
        b1.last_capture_at = datetime(2027, 1, 3, 2, 0)                   # ถ่ายครั้งสุดท้าย 2 วันก่อน
        a3 = s.query(dbm.Camera).filter_by(name="กล้อง A3").one()
        a3.last_capture_at = datetime(2027, 1, 4, 7, 35)                  # 14:35 เวลาไทยของเมื่อวาน: ทันตารางล่าสุด
        s.commit()
    save(**{k: "" for k in settings_store.load() if k.startswith("_alert_camera")})
    out = worker.housekeeping()
    assert out["cameras"] >= 1
    with dbm.SessionLocal() as s:
        texts = [x.detail for x in s.query(dbm.AuditLog).filter_by(action="alert").order_by(dbm.AuditLog.id.desc()).limit(4)]
    assert any("กล้อง B1 (พ่นสี) ไม่ได้ส่งภาพตามตารางเวลา 05/01 09:00" in t and "โปรแกรมกล้อง" in t for t in texts)
    assert not any("กล้อง A3" in t and "ไม่ได้ส่งภาพ" in t for t in texts)
    assert worker.housekeeping()["cameras"] == 0                              # แจ้งวันละครั้งต่อกล้อง
    for url in ("/admin/cameras", "/admin/departments", "/admin/rounds", "/admin", "/capture", "/", "/ranking", "/tv", "/trend"):
        got = a.get(url)
        assert got.status_code == 200 and "Traceback" not in got.text, url
    page = a.get("/admin/departments").text
    assert 'name="cam_sched_mode"' in page and 'name="sched_times" value="08:00, 13:00"' in page
    # กลับไปใช้ค่ากลางของแผนก
    a.post("/admin/departments/save", data={"code": "K1", "name": "ขึ้นรูปโลหะ", "id": d["K1"], "cam_sched_mode": "inherit"})
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Department, d["K1"]).cam_schedule is None


def test_08_upgrade_from_1_4_database():
    from sqlalchemy import inspect, text
    with dbm.engine.begin() as conn:
        n = conn.execute(text("select count(*) from photos")).scalar()
        for table, col in (("cameras", "sched_mode"), ("cameras", "schedule"), ("departments", "cam_schedule"),
                           ("rounds", "auto_open"), ("rounds", "auto_close")):
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
    dbm.init_db()
    cameras.invalidate()
    insp = inspect(dbm.engine)
    assert {"sched_mode", "schedule"} <= {c["name"] for c in insp.get_columns("cameras")}
    assert {"auto_open", "auto_close"} <= {c["name"] for c in insp.get_columns("rounds")}
    with dbm.engine.begin() as conn:
        assert conn.execute(text("select count(*) from photos")).scalar() == n
    a = S["admin"]
    for url in ("/", "/admin/cameras", "/admin/rounds", "/admin/departments", "/capture", "/ranking"):
        assert a.get(url).status_code == 200, url
    data = S["api"].poll()
    assert len(data["cameras"]) == 3 and all(v == ["09:00"] for v in data["plan"].values())      # กล้องเดิมใช้ค่ากลาง
    rounds_auto._seen["day"] = None

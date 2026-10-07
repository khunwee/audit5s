"""ชุดทดสอบของรุ่น 1.5.1: ลบข้อมูลที่สร้างไว้ทดสอบได้ครบ ทั้งรายรอบ รายบัญชี รายแผนก และล้างข้อมูลการตรวจทั้งหมดก่อนใช้งานจริง"""
import io
import os
import random
import tempfile

from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v151_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import cameras, db as dbm, settings_store, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
S = {}


def jpeg() -> bytes:
    im = Image.new("RGB", (1000, 750), (random.randrange(256), random.randrange(256), random.randrange(256)))
    for _ in range(25):
        x, y = random.randrange(900), random.randrange(650)
        im.paste((random.randrange(256), random.randrange(256), random.randrange(256)), (x, y, x + 80, y + 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


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


def count(model, **kw):
    with dbm.SessionLocal() as s:
        return s.query(model).filter_by(**kw).count()


def trial_round(a, name, dept):
    """รอบทดสอบแบบรายการตรวจ: ส่งภาพ ให้ AI ทดลองตรวจ แล้วยืนยันผลเพื่อให้เกิดงานแก้ไข"""
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status.in_(["open", "planned"])):
            r.status = "closed"
        s.commit()
    assert a.post("/admin/rounds/save", data={"name": name, "min_photos": 1, "mode": "checklist"}).status_code == 303
    with dbm.SessionLocal() as s:
        rid = s.query(dbm.Round).filter_by(name=name).one().id
    ids = []
    for i in range(3):
        r = S["mem"].post("/api/photos", data=dict(round_id=rid, department_id=dept, area_name=f"จุดทดสอบ {i}", area_type="สายการผลิต"),
                          files={"file": ("p.jpg", jpeg(), "image/jpeg")})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    n = 0
    while worker.process_one() and n < 50:
        n += 1
    for pid in ids:
        a.post(f"/admin/photos/{pid}/verify")
    return rid, ids


def test_01_delete_a_trial_round_with_everything_in_it():
    a = admin()
    S["admin"] = a
    with dbm.SessionLocal() as s:
        settings_store.save(s, dict(ai1_type="demo", ai2_type="none", ai_passes=1, ai_daily=100000, auto_actions=True,
                                    verify_required=False, allow_free_area=True, max_photos_per_dept=0, storage_budget_mb=350,
                                    retention_days=0, area_types=["สายการผลิต", "คลัง"], rounds_repeat="off", member_see_all=True))
    a.post("/admin/checkpoints/reset")
    a.post("/admin/departments/save", data={"code": "Z1", "name": "แผนกทดสอบ"})
    with dbm.SessionLocal() as s:
        S["dept"] = s.query(dbm.Department).filter_by(code="Z1").one().id
    a.post("/admin/users/save", data=dict(username="z_mem", full_name="ผู้ทดสอบ", role="member", department_id=S["dept"], password="Start1234"))
    S["mem"] = client("z_mem", "Start1234", "Member5S99x")
    rid, ids = trial_round(a, "รอบทดสอบ ก", S["dept"])
    assert count(dbm.Photo, round_id=rid) == 3 and count(dbm.Action, round_id=rid) >= 1       # โหมดทดลองสุ่มให้มีข้อที่ไม่ผ่าน
    other, _ = trial_round(a, "รอบทดสอบ ข", S["dept"])
    kept = count(dbm.Action, round_id=other)
    assert a.post(f"/admin/rounds/{rid}/delete").status_code == 303
    assert count(dbm.Round, id=rid) == 0 and count(dbm.Photo, round_id=rid) == 0 and count(dbm.Action, round_id=rid) == 0
    with dbm.SessionLocal() as s:
        assert s.query(dbm.PhotoImage).filter(dbm.PhotoImage.photo_id.in_(ids)).count() == 0
        assert s.query(dbm.PhotoThumb).filter(dbm.PhotoThumb.photo_id.in_(ids)).count() == 0
    assert count(dbm.Action, round_id=other) == kept and count(dbm.Photo, round_id=other) == 3      # รอบอื่นไม่ถูกแตะ
    for url in ("/actions", "/ranking", "/dashboard", "/photos", "/admin/rounds", "/tv"):
        assert a.get(url).status_code == 200, url
    S["other"] = other


def test_02_delete_trial_users_and_departments():
    a, dept = S["admin"], S["dept"]
    with dbm.SessionLocal() as s:
        uid = s.query(dbm.User).filter_by(username="z_mem").one().id
        me = s.query(dbm.User).filter_by(username="admin").one().id
        sent = s.query(dbm.Photo).filter_by(uploader_id=uid).count()
    assert sent == 3 and "ลบบัญชีนี้" in a.get("/admin/users").text
    assert S["mem"].post(f"/admin/users/{me}/delete").status_code == 403           # เฉพาะผู้ดูแลระบบ
    a.post(f"/admin/users/{me}/delete")
    assert count(dbm.User, id=me) == 1                                             # ลบบัญชีตัวเองไม่ได้
    assert a.post(f"/admin/users/{uid}/delete").status_code == 303
    assert count(dbm.User, id=uid) == 0
    with dbm.SessionLocal() as s:                                                  # ภาพยังอยู่ และยังแสดงชื่อผู้ส่งเดิม
        p = s.query(dbm.Photo).filter_by(round_id=S["other"]).first()
        assert p.uploader_id is None and p.uploader_name == "ผู้ทดสอบ"
    assert S["mem"].get("/").headers["location"].startswith("/login")              # บัญชีที่ถูกลบใช้ต่อไม่ได้
    # แผนกที่ยังมีข้อมูล: ปิดใช้แทนการลบ, ลบได้เมื่อไม่มีภาพ กล้อง และงานแก้ไขแล้ว
    a.post("/admin/areas/save", data=dict(department_id=dept, name="จุดของแผนกทดสอบ", area_type="สายการผลิต", required="1", active="1"))
    a.post("/admin/cameras/save", data=dict(name="กล้องทดสอบ", department_id=dept, area_name="จุดของแผนกทดสอบ", area_type="สายการผลิต",
                                            mode="agent", source="snapshot", url="http://10.0.0.9/s.jpg"))
    a.post(f"/admin/departments/{dept}/delete")
    with dbm.SessionLocal() as s:
        d = s.get(dbm.Department, dept)
        assert d is not None and d.active is False
        cid = s.query(dbm.Camera).filter_by(name="กล้องทดสอบ").one().id
        aid = s.query(dbm.AuditArea).filter_by(name="จุดของแผนกทดสอบ").one().id
        s.add(dbm.Checkpoint(code="ZT1", crit_code="S2", text="โซนทดสอบ", area_id=aid, zone=[10, 10, 500, 500], points=5, minor_points=3))
        s.commit()
    a.post(f"/admin/rounds/{S['other']}/delete")
    a.post(f"/admin/cameras/{cid}/delete")
    assert a.post(f"/admin/departments/{dept}/delete").status_code == 303
    assert count(dbm.Department, id=dept) == 0 and count(dbm.AuditArea, department_id=dept) == 0
    assert count(dbm.Checkpoint, code="ZT1") == 0 and count(dbm.Camera, id=cid) == 0
    assert a.get("/admin/departments").status_code == 200 and a.get("/admin/areas").status_code == 200


def test_03_wipe_all_audit_data_before_go_live():
    a = S["admin"]
    a.post("/admin/departments/save", data={"code": "Z2", "name": "แผนกจริง"})
    with dbm.SessionLocal() as s:
        dept = s.query(dbm.Department).filter_by(code="Z2").one().id
    a.post("/admin/users/save", data=dict(username="z_real", full_name="ผู้ใช้จริง", role="member", department_id=dept, password="Start1234"))
    S["mem"] = client("z_real", "Start1234", "Member5S99x")
    a.post("/admin/cameras/save", data=dict(name="กล้องจริง", department_id=dept, area_name="ไลน์จริง", area_type="สายการผลิต", mode="agent",
                                            source="snapshot", url="http://10.0.0.8/s.jpg", sched_mode="own", sched_times="09:00",
                                            sched_days=["0", "1", "2", "3", "4"]))
    trial_round(a, "รอบทดสอบ ค", dept)
    before = dict(depts=count(dbm.Department), users=count(dbm.User), cams=count(dbm.Camera), checks=count(dbm.Checkpoint),
                  crits=count(dbm.Criterion), key=settings_store.load().get("ai1_type"))
    assert count(dbm.Round) >= 1 and count(dbm.Photo) >= 3
    page = a.get("/admin/storage").text
    assert "ล้างข้อมูลทดสอบก่อนใช้งานจริง" in page and 'action="/admin/storage/wipe"' in page
    assert "ล้างข้อมูลทดสอบก่อนใช้งานจริง" not in S["mem"].get("/").text
    assert S["mem"].post("/admin/storage/wipe", data={"confirm": "ลบข้อมูลทดสอบ"}).status_code == 403
    n = count(dbm.Photo)
    a.post("/admin/storage/wipe", data={"confirm": "ลบ"})                           # พิมพ์ไม่ตรง: ไม่ลบอะไร
    assert count(dbm.Photo) == n and "ยังไม่ได้ลบอะไร" in a.get("/admin/storage").text
    assert a.post("/admin/storage/wipe", data={"confirm": " ลบข้อมูลทดสอบ "}).status_code == 303
    for model in (dbm.Round, dbm.Photo, dbm.PhotoImage, dbm.PhotoThumb, dbm.Action, dbm.DeptSummary, dbm.NotifyEvent, dbm.AiUsage):
        assert count(model) == 0, model.__name__
    after = dict(depts=count(dbm.Department), users=count(dbm.User), cams=count(dbm.Camera), checks=count(dbm.Checkpoint),
                 crits=count(dbm.Criterion), key=settings_store.load().get("ai1_type"))
    assert after == before                                                          # สิ่งที่ตั้งค่าไว้อยู่ครบ
    with dbm.SessionLocal() as s:
        cam = s.query(dbm.Camera).filter_by(name="กล้องจริง").one()
        assert cam.schedule["times"] == ["09:00"] and cam.last_capture_at is None
        last = s.query(dbm.AuditLog).order_by(dbm.AuditLog.id.desc()).first()
        assert last.action == "compact_storage" or s.query(dbm.AuditLog).filter_by(action="wipe_test_data").count() == 1
        assert s.query(dbm.AuditLog).count() > 1                                    # บันทึกการใช้งานเดิมยังอยู่ (ไม่ได้ติ๊กลบ)
    assert "ลบข้อมูลการตรวจแล้ว" in a.get("/admin/storage").text
    for url in ("/", "/capture", "/photos", "/ranking", "/dashboard", "/actions", "/verify", "/trend", "/tv", "/admin", "/admin/rounds",
                "/admin/storage", "/admin/cameras", "/admin/quality"):
        got = a.get(url)
        assert got.status_code == 200 and "Traceback" not in got.text, url
    assert "ยังไม่มีรอบการตรวจ" in a.get("/").text
    # เริ่มใช้งานจริงได้ทันทีหลังล้าง
    rid, ids = trial_round(a, "รอบจริงรอบแรก", dept)
    assert count(dbm.Photo, round_id=rid) == 3 and a.get(f"/photos/{ids[0]}").status_code == 200
    # ล้างพร้อมบันทึกการใช้งาน: เหลือเฉพาะรายการที่บอกว่ามีการล้าง
    a.post("/admin/storage/wipe", data={"confirm": "ลบข้อมูลทดสอบ", "logs": "1"})
    with dbm.SessionLocal() as s:
        acts = [x.action for x in s.query(dbm.AuditLog).all()]
        assert "wipe_test_data" in acts and len(acts) <= 2 and s.query(dbm.Round).count() == 0
    cameras.invalidate()

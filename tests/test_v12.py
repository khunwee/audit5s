"""ชุดทดสอบของรุ่น 1.2: จุดตรวจที่โรงงานกำหนด ความครบของจุดตรวจ การยืนยันผลโดยหัวหน้าหรือกรรมการ
เพดานจำนวนภาพ การแจ้งผู้ดูแลเรื่องพื้นที่ การเตือนให้สำรอง และการคืนพื้นที่
"""
import csv
import io
import json
import os
import random
import tempfile
from datetime import timedelta

import httpx
import pytest
from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v12_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, cameras, db as dbm, notify, scoring, settings_store, storage, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
AI = {"queue": [], "default": [3, 3, 3, 3, 3], "bodies": []}
OUT = []
S = {}


def jpeg(seed: int, size=(1000, 750)) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    for _ in range(25):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        im.paste((rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)), (x, y, x + 70, y + 50))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ai_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    AI["bodies"].append(json.dumps(body, ensure_ascii=False))
    levels = AI["queue"].pop(0) if AI["queue"] else AI["default"]
    crit = [dict(code=c, na=False, level=levels[i], reason=f"เหตุผล {c}", findings=[], recommendations=[f"แก้ {c}"])
            for i, c in enumerate(S["codes"])]
    out = dict(image_ok=True, scene="-", criteria=crit, summary="สรุป", top_actions=["เก็บของเข้าที่"])
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(out, ensure_ascii=False)}]}}]})


def out_handler(request: httpx.Request) -> httpx.Response:
    OUT.append(json.loads(request.content)["content"])
    return httpx.Response(204)


def cam_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=jpeg(random.randrange(10 ** 6) + 50000), headers={"content-type": "image/jpeg"})


@pytest.fixture(scope="module", autouse=True)
def wiring():
    old = (ai._transport, notify._transport, cameras._transport)
    ai._transport = httpx.MockTransport(ai_handler)
    notify._transport = httpx.MockTransport(out_handler)
    cameras._transport = httpx.MockTransport(cam_handler)
    yield
    ai._transport, notify._transport, cameras._transport = old


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


def upload(c, did, seed, expect=200, **data):
    data.setdefault("area_type", "คลัง")
    r = c.post("/api/photos", data=dict(round_id=S["rid"], department_id=did, **data),
               files={"file": ("p.jpg", jpeg(seed), "image/jpeg")})
    assert r.status_code == expect, r.text
    return r.json().get("id") if expect == 200 else r.json()["detail"]


def drain():
    n = 0
    while worker.process_one() and n < 100:
        n += 1
    return n


def photo(pid):
    with dbm.SessionLocal() as s:
        return s.get(dbm.Photo, pid)


def save(**kw):
    with dbm.SessionLocal() as s:
        settings_store.save(s, kw)


def clear_alert_stamps():
    save(**{k: "" for k in settings_store.load() if k.startswith("_alert_")} or {"_alert_x": ""})


def row(did):
    with dbm.SessionLocal() as s:
        return scoring.find_row(scoring.round_ranking(s, s.get(dbm.Round, S["rid"])), did)


def alerts():
    with dbm.SessionLocal() as s:
        return [x.detail for x in s.query(dbm.AuditLog).filter_by(action="alert").order_by(dbm.AuditLog.id)]


def test_01_setup_defined_areas():
    a = admin()
    S["admin"] = a
    for code, name in (("W1", "เชื่อม"), ("W2", "บรรจุ")):
        a.post("/admin/departments/save", data={"code": code, "name": name})
    with dbm.SessionLocal() as s:
        S["d"] = {d.code: d.id for d in s.query(dbm.Department).all()}
        for r in s.query(dbm.Round).filter_by(status="open"):
            r.status = "closed"
        s.query(dbm.Channel).delete()
        s.commit()
    notify.refresh_channels()
    clear_alert_stamps()
    d = S["d"]
    save(ai1_type="gemini", ai1_key="g-key", ai1_model="gemini-flash", ai2_type="none", ai_daily=100000, ai_passes=1,
         storage_budget_mb=350, storage_warn_pct=80, retention_days=0, ranking_visibility="always", member_see_all=True,
         after_replaces=True, allow_free_area=True, require_coverage=False, verify_required=False, max_photos_per_dept=0,
         area_types=["สายการผลิต", "คลัง"], public_url="https://5s.example.test", ai_extra="")
    a.post("/admin/rounds/save", data={"name": "รอบทดสอบ 1.2", "min_photos": 1})
    with dbm.SessionLocal() as s:
        rnd = s.query(dbm.Round).order_by(dbm.Round.id.desc()).first()
        S["rid"], S["codes"] = rnd.id, [c["code"] for c in rnd.rubric]

    def area(dept, name, required=True, standard="", area_type="สายการผลิต"):
        data = dict(department_id=dept, name=name, area_type=area_type, standard=standard, sort_order=10, active="1")
        if required:
            data["required"] = "1"
        assert a.post("/admin/areas/save", data=data).status_code == 303
        with dbm.SessionLocal() as s:
            return s.query(dbm.AuditArea).filter_by(department_id=dept, name=name).one().id
    S["a1"] = area(d["W1"], "ตู้เชื่อม 1", standard="สายเชื่อมม้วนเก็บบนขอแขวน ถังแก๊สมีโซ่คล้อง")
    S["a2"] = area(d["W1"], "ชั้นวางลวดเชื่อม", area_type="คลัง")
    S["a3"] = area(d["W1"], "โต๊ะหัวหน้ากะ", required=False)
    a.post("/admin/areas/save", data=dict(department_id=d["W1"], name="ตู้เชื่อม 1", required="1"))     # ชื่อซ้ำในแผนกเดียวกัน
    a.post("/admin/areas/save", data=dict(department_id=d["W1"], name="", required="1"))
    with dbm.SessionLocal() as s:
        assert s.query(dbm.AuditArea).filter_by(department_id=d["W1"]).count() == 3
    page = a.get("/admin/areas")
    assert page.status_code == 200 and "ตู้เชื่อม 1" in page.text and "ยังไม่มีมาตรฐาน" in page.text
    for u, role, dept, perms in (("w_mem", "member", d["W1"], {}), ("w_sup", "member", d["W1"], {"perm_score": "1"}),
                                 ("w_aud", "auditor", "", {"perm_score": "1"}), ("w_m2", "member", d["W2"], {})):
        a.post("/admin/users/save", data=dict(username=u, full_name=u, role=role, department_id=dept, password="Start1234", **perms))
    for u in ("w_mem", "w_sup", "w_aud", "w_m2"):
        S[u] = client(u, "Start1234", "Member5S99x")
    # หน้าถ่ายภาพได้รายการจุดตรวจของแผนกตัวเอง
    page = S["w_mem"].get("/capture").text
    defined = json.loads(page.split("data-defined='")[1].split("'")[0].replace("&#34;", '"'))
    assert [x["name"] for x in defined[str(d["W1"])]] == ["ตู้เชื่อม 1", "ชั้นวางลวดเชื่อม", "โต๊ะหัวหน้ากะ"]
    assert defined[str(d["W1"])][2]["required"] is False and str(d["W2"]) not in defined
    assert 'data-allow-free="1"' in page and "data-area-pick" in page
    assert S["w_mem"].get("/admin/areas").status_code == 403


def test_02_upload_against_defined_areas_and_standard_reaches_ai():
    d, m = S["d"], S["w_mem"]
    AI["bodies"].clear()
    AI["queue"] = [[2, 2, 2, 2, 2]]
    p1 = upload(m, d["W1"], 1, area_id=S["a2"], area_name="ชื่อที่พิมพ์มาไม่ถูกใช้")
    p = photo(p1)
    assert (p.area_id, p.area_name, p.area_type) == (S["a2"], "ชั้นวางลวดเชื่อม", "คลัง")      # ชื่อและประเภทมาจากจุดที่โรงงานกำหนด
    p2 = upload(m, d["W1"], 2, area_name="ตู้เชื่อม 1")                                         # พิมพ์ชื่อตรงกับจุดที่กำหนด
    assert photo(p2).area_id == S["a1"]
    p3 = upload(m, d["W1"], 3, area_name="มุมพักสูบบุหรี่")                                     # จุดอื่น ยังอนุญาต
    assert photo(p3).area_id is None
    assert "ไม่ใช่ของแผนกนี้" in upload(S["w_m2"], d["W2"], 4, expect=400, area_id=S["a1"])
    drain()
    prompts = "\n".join(AI["bodies"])
    assert "มาตรฐานของจุดตรวจนี้ที่โรงงานกำหนด" in AI["bodies"][1] and "ถังแก๊สมีโซ่คล้อง" in AI["bodies"][1]
    assert "มาตรฐานของจุดตรวจนี้" not in AI["bodies"][0] and "มาตรฐานของจุดตรวจนี้" not in AI["bodies"][2]
    assert "เชื่อม\"" not in prompts.replace("ตู้เชื่อม", "").replace("ลวดเชื่อม", "")           # ไม่มีชื่อแผนกในคำสั่งถึง AI
    # ปิดการพิมพ์ชื่อเอง: แผนกที่มีจุดกำหนดต้องเลือกจากรายการ แผนกที่ยังไม่กำหนดยังพิมพ์ได้
    save(allow_free_area=False)
    assert "เลือกจุดตรวจจากรายการ" in upload(m, d["W1"], 5, expect=400, area_name="จุดที่ตั้งเอง")
    assert upload(S["w_m2"], d["W2"], 6, area_name="โต๊ะบรรจุ 1")
    assert 'data-allow-free="0"' in m.get("/capture").text
    # กล้อง IP ที่ผู้ดูแลลงทะเบียน ไม่ถูกบังคับให้ตรงรายการ แต่ถ้าชื่อจุดตรงกันจะผูกให้เอง
    a = S["admin"]
    a.post("/admin/cameras/save", data=dict(name="กล้องเชื่อม", department_id=d["W1"], area_name="ตู้เชื่อม 1", mode="direct",
                                            source="snapshot", url="http://10.1.1.1/s.jpg"))
    with dbm.SessionLocal() as s:
        cid = s.query(dbm.Camera).filter_by(name="กล้องเชื่อม").one().id
    r = a.post(f"/api/cameras/{cid}/capture", data={"round_id": S["rid"]})
    assert r.status_code == 200 and photo(r.json()["id"]).area_id == S["a1"]
    save(allow_free_area=True)
    drain()
    S.update(p1=p1, p2=p2, p3=p3)


def test_03_coverage_of_required_areas():
    d, a, rid = S["d"], S["admin"], S["rid"]
    r = row(d["W1"])
    assert (r["areas_required"], r["areas_covered"], r["missing"]) == (2, 2, []) and r["qualified"]
    assert row(d["W2"])["areas_required"] == 0
    # เพิ่มจุดบังคับใหม่ที่แผนกยังไม่ได้ถ่าย
    a.post("/admin/areas/save", data=dict(department_id=d["W1"], name="พื้นที่เก็บถังแก๊ส", required="1", active="1"))
    r = row(d["W1"])
    assert (r["areas_covered"], r["areas_required"], r["missing"], r["qualified"]) == (2, 3, ["พื้นที่เก็บถังแก๊ส"], True)
    assert "จุดตรวจบังคับที่ยังไม่มีภาพที่นับคะแนน: พื้นที่เก็บถังแก๊ส" in a.get(f"/rounds/{rid}/dept/{d['W1']}").text
    assert "จุดตรวจบังคับที่ยังขาด: พื้นที่เก็บถังแก๊ส" in S["w_mem"].get("/").text
    assert "จุดตรวจบังคับ 2/3" in a.get(f"/ranking?round={rid}").text
    save(require_coverage=True)
    r = row(d["W1"])
    assert r["qualified"] is False and r["rank"] is None
    page = a.get(f"/ranking?round={rid}").text
    assert "ยังไม่ถูกจัดอันดับ" in page and "พื้นที่เก็บถังแก๊ส" in page and "ครบทุกจุดตรวจบังคับ" in page
    rows = list(csv.reader(io.StringIO(a.get(f"/rounds/{rid}/export/ranking.csv").content.decode("utf-8-sig"))))
    w1 = [x for x in rows if x[1] == "W1"][0]
    assert (w1[rows[0].index("จุดตรวจบังคับ")], w1[rows[0].index("จุดตรวจที่ส่งแล้ว")], w1[-1]) == ("3", "2", "ภาพยังไม่ครบ")
    with dbm.SessionLocal() as s:
        aid = s.query(dbm.AuditArea).filter_by(name="พื้นที่เก็บถังแก๊ส").one().id
    upload(S["w_mem"], d["W1"], 30, area_id=aid)
    assert row(d["W1"])["qualified"] is False                       # ส่งแล้วแต่ AI ยังไม่ให้คะแนน ยังไม่นับว่าครบ
    drain()
    assert row(d["W1"])["qualified"] is True and row(d["W1"])["rank"] is not None
    # จุดที่มีภาพแล้ว ลบไม่ได้ ถูกปิดใช้ และไม่นับเป็นจุดบังคับอีก
    a.post(f"/admin/areas/{aid}/delete")
    with dbm.SessionLocal() as s:
        assert s.get(dbm.AuditArea, aid).active is False
    assert row(d["W1"])["areas_required"] == 2
    save(require_coverage=False)


def test_04_supervisor_verification():
    d, a, rid = S["d"], S["admin"], S["rid"]
    sup, aud, mem = S["w_sup"], S["w_aud"], S["w_mem"]
    with dbm.SessionLocal() as s:
        logged = s.query(dbm.AuditLog).filter_by(action="verify_photo").count()
    assert mem.get("/verify").status_code == 403 and "ยืนยันผล" not in mem.get("/").text.split("</header>")[0]
    page = aud.get(f"/verify?round={rid}")
    assert page.status_code == 200 and "ชั้นวางลวดเชื่อม" in page.text and "ตรงกับภาพ ยืนยันคะแนนนี้" in page.text
    assert page.text.index("ชั้นวางลวดเชื่อม") < page.text.index("มุมพักสูบบุหรี่")          # คะแนนต่ำขึ้นก่อน (40% ก่อน 75%)
    # หัวหน้าแผนกที่มีสิทธิ์ยืนยัน: ยืนยันภาพของคนอื่นได้ แต่ภาพที่ตัวเองส่งต้องให้คนอื่นยืนยัน
    own = upload(sup, d["W1"], 40, area_name="ภาพที่หัวหน้าส่งเอง")
    drain()
    assert sup.post(f"/admin/photos/{own}/verify").status_code == 403
    assert "ต้องให้คนอื่นยืนยัน" in sup.get(f"/verify?round={rid}").text and "ภาพนี้คุณเป็นผู้ส่ง" in sup.get(f"/photos/{own}").text
    assert sup.post(f"/admin/photos/{S['p1']}/verify").status_code == 303
    p = photo(S["p1"])
    assert p.verified_by == "w_sup" and p.verified_at is not None
    assert mem.post(f"/admin/photos/{S['p2']}/verify").status_code == 403
    assert "ยืนยันผล</dt><dd>w_sup เมื่อ" in a.get(f"/photos/{S['p1']}").text.replace("\n", "")
    # ตั้งให้นับเฉพาะภาพที่ยืนยันแล้ว
    r = row(d["W1"])
    assert r["verified"] == 1 and r["unverified"] >= 4 and r["scored"] >= 5
    save(verify_required=True)
    r = row(d["W1"])
    assert (r["scored"], r["avg"]) == (1, 50.0) and r["areas_covered"] == 1 and r["missing"] == ["ตู้เชื่อม 1"]
    assert row(d["W2"])["scored"] == 0 and row(d["W2"])["qualified"] is False
    assert "นับคะแนนเฉพาะภาพที่หัวหน้าหรือกรรมการดูแล้วยืนยัน" in a.get(f"/ranking?round={rid}").text
    assert aud.post(f"/admin/photos/{S['p2']}/verify", headers={"referer": f"http://testserver/verify?round={rid}"}).headers["location"].endswith(f"/verify?round={rid}")
    r = row(d["W1"])
    assert r["scored"] == 2 and r["avg"] == 62.5 and r["missing"] == []
    # ปรับคะแนน = ยืนยันไปในตัว, วิเคราะห์ใหม่ = ต้องยืนยันใหม่
    data = {f"level_{c}": 4 for c in S["codes"]}
    assert aud.post(f"/admin/photos/{S['p3']}/override", data=dict(data, note="ตรวจหน้างานแล้วเรียบร้อย")).status_code == 303
    assert photo(S["p3"]).verified_by == "w_aud" and row(d["W1"])["scored"] == 3
    a.post(f"/admin/photos/{S['p2']}/reanalyze")
    drain()
    assert photo(S["p2"]).verified_at is None and row(d["W1"])["scored"] == 2
    page = a.get(f"/photos?round={rid}&status=unverified").text
    assert f'href="/photos/{S["p2"]}"' in page and f'href="/photos/{S["p1"]}"' not in page
    rows = list(csv.reader(io.StringIO(a.get(f"/rounds/{rid}/export/photos.csv").content.decode("utf-8-sig"))))
    by = {r_[0]: r_ for r_ in rows[1:]}
    assert by[str(S["p1"])][rows[0].index("ยืนยันโดย")] == "w_sup" and by[str(S["p1"])][rows[0].index("เป็นจุดตรวจที่กำหนด")] == "ใช่"
    assert by[str(S["p3"])][rows[0].index("เป็นจุดตรวจที่กำหนด")] == "" and by[str(S["p2"])][rows[0].index("ยืนยันโดย")] == ""
    with dbm.SessionLocal() as s:
        assert s.query(dbm.AuditLog).filter_by(action="verify_photo").count() == logged + 2
    assert "ภาพรอหัวหน้าหรือกรรมการยืนยันผล" in a.get("/admin").text
    save(verify_required=False)


def test_05_photo_cap_per_department():
    d = S["d"]
    with dbm.SessionLocal() as s:
        have = s.query(dbm.Photo).filter_by(round_id=S["rid"], department_id=d["W2"]).count()
    save(max_photos_per_dept=have + 1)
    upload(S["w_m2"], d["W2"], 50, area_name="โต๊ะบรรจุ 2")
    assert "ส่งครบ" in upload(S["w_m2"], d["W2"], 51, expect=400, area_name="โต๊ะบรรจุ 3")
    save(max_photos_per_dept=0)
    upload(S["w_m2"], d["W2"], 51, area_name="โต๊ะบรรจุ 3")
    drain()


def test_06_storage_alerts_reach_admin():
    a, d = S["admin"], S["d"]
    a.post("/admin/notifications/save", data={"name": "Discord ผู้ดูแล", "kind": "discord", "ev_system": "1",
                                              "cfg_discord_webhook_url": "https://discord.com/api/webhooks/9/z"})
    clear_alert_stamps()
    OUT.clear()
    before = len(alerts())
    with dbm.SessionLocal() as s:
        used = storage.usage(s, settings_store.load())["logical"] / storage.MB
    # งบพื้นที่ที่ทำให้การใช้อยู่ราว 90% -> ถึงเกณฑ์แจ้ง (80%) แต่ยังรับภาพได้
    save(storage_budget_mb=20, storage_warn_pct=80)
    with dbm.SessionLocal() as s:
        s.query(dbm.Photo).filter(dbm.Photo.has_image.is_(True)).update({"image_bytes": 100})
        first = s.query(dbm.Photo).filter(dbm.Photo.has_image.is_(True)).first()
        first.image_bytes = int(17.5 * storage.MB)
        s.commit()
        u = storage.usage(s, settings_store.load())
        assert u["level"] == "warn" and 80 <= u["pct"] < 100, u["pct"]
    upload(S["w_m2"], d["W2"], 60, area_name="ระหว่างพื้นที่ใกล้เต็ม")
    notify.flush(force=True)
    new = alerts()[before:]
    assert len(new) == 1 and "พื้นที่จัดเก็บใช้ไป" in new[0] and "100%" in new[0]
    assert len(OUT) == 1 and "แจ้งผู้ดูแลระบบ" in OUT[0] and "พื้นที่จัดเก็บใช้ไป" in OUT[0]
    upload(S["w_m2"], d["W2"], 61, area_name="ภาพถัดไป")                 # ภายใน 20 ชั่วโมง ไม่แจ้งเรื่องเดิมซ้ำ
    notify.flush(force=True)
    assert len(alerts()) == before + 1 and len(OUT) == 1
    assert settings_store.load(force=True)["_alert_storage_warn"]          # เวลาที่แจ้งเก็บในฐานข้อมูล อยู่รอดข้ามการรีสตาร์ต
    page = a.get("/admin").text
    assert "แจ้งเตือนถึงผู้ดูแลใน 14 วันล่าสุด" in page and "พื้นที่จัดเก็บใช้ไป" in page
    assert "ยังไม่ถูกส่งออกไปที่ใด" not in page
    # เต็ม: หยุดรับภาพ และแจ้งทันทีว่าหยุดรับแล้ว
    with dbm.SessionLocal() as s:
        s.get(dbm.Photo, first.id).image_bytes = int(21 * storage.MB)
        s.commit()
    assert "พื้นที่จัดเก็บเต็ม" in upload(S["w_m2"], d["W2"], 62, expect=507, area_name="ตอนเต็ม")
    notify.flush(force=True)
    assert "ระบบหยุดรับภาพใหม่แล้ว" in alerts()[-1] and "ระบบหยุดรับภาพใหม่แล้ว" in OUT[-1] and len(OUT) == 2
    assert "พื้นที่เต็ม" in a.get("/admin").text and "พื้นที่ใกล้เต็ม" in a.get("/").text
    # ความจุของ host (Postgres): แจ้งเมื่อขนาดฐานข้อมูลจริงใกล้เพดาน แม้ข้อมูลที่ใช้อยู่จะยังน้อย
    real = (storage.physical_bytes, storage.IS_SQLITE)
    storage.physical_bytes, storage.IS_SQLITE = (lambda _db: int(480 * storage.MB)), False
    try:
        save(storage_budget_mb=350, host_limit_mb=500)
        with dbm.SessionLocal() as s:
            s.get(dbm.Photo, first.id).image_bytes = 100
            s.commit()
            u = storage.usage(s, settings_store.load())
            assert (u["level"], u["host_level"], u["host_pct"]) == ("ok", "critical", 96.0)
            assert storage.check_alerts(s, settings_store.load()) == ["host_critical"]
        assert "480 MB จากความจุ 500 MB" in alerts()[-1] and "คืนพื้นที่ที่ลบแล้ว" in alerts()[-1]
        assert "ใกล้ความจุของ host" in a.get("/admin").text and "96% ของ host" in a.get("/admin/storage").text
    finally:
        storage.physical_bytes, storage.IS_SQLITE = real
    notify.flush(force=True)
    S["used_mb"] = used


def test_07_housekeeping_reminders():
    a, d, rid = S["admin"], S["d"], S["rid"]
    # ภาพที่ยังรอ AI ต้องไม่ถูกลบภาพเต็ม แม้เข้าเงื่อนไขอื่นครบ
    save(retention_days=1, purge_requires_backup=False)
    with dbm.SessionLocal() as s:
        waiting = [p.id for p in s.query(dbm.Photo).filter_by(round_id=rid, status="pending")]
        s.query(dbm.Photo).filter(dbm.Photo.id.in_(waiting)).update({"created_at": dbm.now() - timedelta(days=30)}, synchronize_session=False)
        s.commit()
        assert len(waiting) == 2 and storage.auto_cleanup(s, settings_store.load()) == {"by_age": 0, "by_space": 0}
        s.query(dbm.Photo).filter(dbm.Photo.id.in_(waiting)).update({"created_at": dbm.now()}, synchronize_session=False)
        s.commit()
    save(retention_days=0, purge_requires_backup=True)
    drain()
    clear_alert_stamps()
    OUT.clear()
    before = len(alerts())
    assert worker.housekeeping() == {"cleanup": {"by_age": 0, "by_space": 0}, "alerts": [], "backup": 0, "actions": 0, "closing": 0}
    # ภาพอายุ 85 วัน นโยบายเก็บ 90 วัน เตือนล่วงหน้า 7 วัน และยังไม่เคยสำรอง -> เตือนให้สำรอง
    save(retention_days=90, backup_remind_days=7, purge_requires_backup=True)
    with dbm.SessionLocal() as s:
        n = s.query(dbm.Photo).filter_by(round_id=rid).update({"created_at": dbm.now() - timedelta(days=85)})
        s.commit()
    out = worker.housekeeping()
    assert out["backup"] == 1 and out["cleanup"] == {"by_age": 0, "by_space": 0}
    assert f"มีภาพ {n} ภาพที่ยังไม่ได้สำรอง" in alerts()[-1] and "รอบทดสอบ 1.2" in alerts()[-1]
    assert worker.housekeeping()["backup"] == 0                                   # วันละครั้งต่อรอบ
    # สำรองแล้ว และภาพเกินอายุ -> ระบบลบภาพเต็มเอง พร้อมแจ้งว่าลบไปกี่ภาพ
    assert a.get(f"/admin/rounds/{rid}/backup.zip").status_code == 200
    with dbm.SessionLocal() as s:
        s.query(dbm.Photo).filter_by(round_id=rid).update({"created_at": dbm.now() - timedelta(days=95)})
        s.get(dbm.Round, rid).last_backup_at = dbm.now()
        s.commit()
    out = worker.housekeeping()
    assert out["cleanup"]["by_age"] == n and out["backup"] == 0
    assert f"ระบบลบภาพเต็มอัตโนมัติ {n} ภาพ" in alerts()[-1] and "ภาพย่อยังอยู่" in alerts()[-1]
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Photo).filter_by(round_id=rid, has_image=True).count() == 0
        assert s.query(dbm.PhotoThumb).join(dbm.Photo, dbm.Photo.id == dbm.PhotoThumb.photo_id).filter(dbm.Photo.round_id == rid).count() == n
    assert a.get(f"/photos/{S['p1']}").status_code == 200 and a.get(f"/photos/{S['p1']}/image").status_code == 200
    # รอบใกล้สิ้นสุด และมีแผนกที่ภาพไม่ครบ
    today = (dbm.now() + timedelta(hours=7)).date()
    a.post("/admin/departments/save", data={"code": "W3", "name": "ตรวจสอบขั้นสุดท้าย"})
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, rid).end_date = today + timedelta(days=1)
        s.commit()
    out = worker.housekeeping()
    assert out["closing"] == 1 and "จะสิ้นสุดอีก 1 วัน" in alerts()[-1] and "ตรวจสอบขั้นสุดท้าย" in alerts()[-1]
    assert worker.housekeeping()["closing"] == 0
    # ใช้ครบเพดาน AI ของวัน และยังมีภาพค้างคิว
    save(retention_days=0, ai_daily=1)
    upload(S["w_m2"], d["W2"], 70, area_name="ค้างคิวเพราะครบเพดาน")
    assert worker.process_one() is False
    assert "AI ใช้ครบเพดาน 1 ครั้งของวันนี้แล้ว มี 1 ภาพรอในคิว" in alerts()[-1]
    worker.process_one()
    assert sum("AI ใช้ครบเพดาน" in x for x in alerts()[before:]) == 1
    save(ai_daily=100000)
    drain()
    notify.flush(force=True)
    assert sum("แจ้งผู้ดูแลระบบ" in x for x in OUT) == len(alerts()) - before          # ทุกเรื่องถูกส่งออกช่องทางของผู้ดูแลด้วย
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, rid).end_date = None
        s.commit()


def test_08_compact_settings_and_pages():
    a, rid = S["admin"], S["rid"]
    assert a.post("/admin/storage/compact").status_code == 303
    assert "คืนพื้นที่แล้ว" in a.get("/admin/storage").text
    assert S["w_aud"].post("/admin/storage/compact").status_code == 403
    form = {"org_name": "AHP", "area_types": "สายการผลิต\nคลัง", "band_good": 80, "band_mid": 60, "ai_passes": "1", "after_replaces": "1",
            "ai1_type": "gemini", "ai1_model": "gemini-flash", "ai2_type": "none", "ai_rpm": 30, "ai_daily": 100000,
            "img_max_side": 1280, "img_quality": 78, "allow_gallery": "1", "storage_budget_mb": 300, "retention_days": 60,
            "purge_requires_backup": "1", "ranking_visibility": "always", "member_see_all": "1",
            "require_coverage": "1", "verify_required": "1", "host_limit_mb": 512, "storage_warn_pct": 70,
            "max_photos_per_dept": 40, "backup_remind_days": 5}
    assert a.post("/admin/settings", data=form).status_code == 303
    s = settings_store.load()
    assert (s["allow_free_area"], s["require_coverage"], s["verify_required"], s["host_limit_mb"], s["storage_warn_pct"],
            s["max_photos_per_dept"], s["backup_remind_days"]) == (False, True, True, 512, 70, 40, 5)
    page = a.get("/admin/settings").text
    assert 'name="host_limit_mb" value="512"' in page and 'name="verify_required" value="1" checked' in page
    assert "จำกัด 40 ภาพต่อแผนกต่อรอบ" in a.get("/admin/storage").text and "แจ้งผู้ดูแลเมื่อใช้ถึง 70%" in a.get("/admin/storage").text
    save(allow_free_area=True, require_coverage=False, verify_required=False, max_photos_per_dept=0, storage_warn_pct=80)
    for name in ("admin", "w_aud", "w_sup", "w_mem", "w_m2"):
        for url in ("/", "/capture", "/photos", f"/photos/{S['p1']}", "/ranking", "/trend", f"/rounds/{rid}/dept/{S['d']['W1']}"):
            r = S[name].get(url)
            assert r.status_code == 200 and "Traceback" not in r.text, (name, url)
    for url in ("/verify", "/admin", "/admin/areas", "/admin/rounds", "/admin/settings", "/admin/storage", "/admin/users",
                "/admin/notifications", "/admin/cameras", "/admin/logs", f"/rounds/{rid}/report", f"/rounds/{rid}/export/scores.xlsx"):
        assert a.get(url).status_code == 200, url


def test_09_upgrade_from_1_1_database():
    from sqlalchemy import inspect, text
    with dbm.engine.begin() as conn:
        n = conn.execute(text("select count(*) from photos")).scalar()
        for col in ("area_id", "verified_by", "verified_at"):
            conn.execute(text(f"ALTER TABLE photos DROP COLUMN {col}"))
        conn.execute(text("DROP TABLE audit_areas"))
    dbm.init_db()
    insp = inspect(dbm.engine)
    assert {"area_id", "verified_by", "verified_at"} <= {c["name"] for c in insp.get_columns("photos")}
    assert "audit_areas" in insp.get_table_names()
    with dbm.engine.begin() as conn:
        assert conn.execute(text("select count(*) from photos")).scalar() == n
    a = S["admin"]
    for url in ("/", "/capture", "/photos", "/ranking", "/verify", "/admin", "/admin/areas", f"/rounds/{S['rid']}/report"):
        assert a.get(url).status_code == 200, url
    assert row(S["d"]["W1"])["areas_required"] == 0 and row(S["d"]["W1"])["scored"] >= 1

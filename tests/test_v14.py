"""ชุดทดสอบของรุ่น 1.4: จอแสดงผล (TV) ประวัติคะแนนและอันดับ การตั้งค่าจอ ลิงก์ของจอ และการเปลี่ยนภาษาไทย/อังกฤษ"""
import io
import json
import os
import random
import re
import tempfile

import httpx
import pytest
from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v14_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, db as dbm, i18n, settings_store, tvdata, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
AI = {"levels": [], "status": {}}
S = {}


def jpeg(seed: int) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", (1000, 750), (rnd.randrange(200), 60 + rnd.randrange(150), rnd.randrange(200)))
    for _ in range(25):
        x, y = rnd.randrange(1000), rnd.randrange(750)
        im.paste((rnd.randrange(256), rnd.randrange(256), 90), (x, y, x + 70, y + 50))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ai_handler(request: httpx.Request) -> httpx.Response:
    prompt = json.loads(request.content)["contents"][0]["parts"][-1]["text"]
    if "รายการตรวจ (แต่ละข้อคือสภาพที่ถูกต้อง)" in prompt:
        codes = re.search(r"ต้องมีครบทุกข้อตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        out = dict(image_ok=True, scene="-", summary="-", top_actions=[],
                   checks=[dict(code=c, status=AI["status"].get(c, "ok"), evidence="เห็น", action="แก้", box=[100, 100, 300, 300]) for c in codes])
    else:
        codes = re.search(r"ต้องมีครบทุกเกณฑ์ตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        level = AI["levels"].pop(0) if AI["levels"] else 3
        out = dict(image_ok=True, scene="-", summary="-", top_actions=[],
                   criteria=[dict(code=c, na=False, level=level, reason="-", findings=[], recommendations=[]) for c in codes])
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(out, ensure_ascii=False)}]}}]})


@pytest.fixture(scope="module", autouse=True)
def wiring():
    old = ai._transport
    ai._transport = httpx.MockTransport(ai_handler)
    yield
    ai._transport = old


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
    while worker.process_one() and n < 100:
        n += 1


def new_round(a, name, mode="level"):
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter_by(status="open"):
            r.status = "closed"
        s.commit()
    assert a.post("/admin/rounds/save", data={"name": name, "min_photos": 1, "mode": mode}).status_code == 303
    with dbm.SessionLocal() as s:
        return s.query(dbm.Round).filter_by(name=name).one().id


def shoot(c, rid, did, seed, level=None):
    if level is not None:
        AI["levels"].append(level)
    r = c.post("/api/photos", data=dict(round_id=rid, department_id=did, area_name=f"จุด {seed}", area_type="สายการผลิต"),
               files={"file": ("p.jpg", jpeg(seed), "image/jpeg")})
    assert r.status_code == 200, r.text
    drain()
    return r.json()["id"]


def tv(c=None, rounds=6):
    r = (c or S["screen"]).get(f"/api/tv/data?rounds={rounds}")
    assert r.status_code == 200, r.text
    return r.json()


def test_01_history_over_two_rounds():
    a = admin()
    S["admin"] = a
    with dbm.SessionLocal() as s:                       # เริ่มจากรายชื่อแผนกที่รู้แน่ชัด
        for d in s.query(dbm.Department).all():
            d.active = False
        s.commit()
    assert a.post("/admin/departments/save", data={"code": "T1", "name": "งานเชื่อม", "name_en": "Welding"}).status_code == 303
    a.post("/admin/departments/save", data={"code": "T2", "name": "งานประกอบ", "name_en": "Assembly"})
    a.post("/admin/departments/save", data={"code": "T3", "name": "คลังสินค้าสำเร็จรูป"})
    with dbm.SessionLocal() as s:
        S["d"] = {d.code: d.id for d in s.query(dbm.Department).filter(dbm.Department.code.in_(["T1", "T2", "T3"]))}
        assert s.get(dbm.Department, S["d"]["T1"]).name_en == "Welding"
    save(ai1_type="gemini", ai1_key="g", ai1_model="gemini-flash", ai2_type="none", ai_passes=1, ai_daily=100000,
         ranking_visibility="always", member_see_all=True, verify_required=False, require_coverage=False, allow_free_area=True,
         max_photos_per_dept=0, storage_budget_mb=350, retention_days=0, area_types=["สายการผลิต", "คลัง"], org_name="โรงงานทดสอบ",
         band_good=80, band_mid=60, auto_actions=True, tv_key="", tv_round="auto", tv_rounds=6, after_replaces=True)
    d = S["d"]
    S["ra"] = new_round(a, "รอบ TV ก")
    shoot(a, S["ra"], d["T1"], 1, 4)                     # 100
    shoot(a, S["ra"], d["T2"], 2, 3)                     # 75
    shoot(a, S["ra"], d["T3"], 3, 2)                     # 50
    S["rb"] = new_round(a, "รอบ TV ข")
    shoot(a, S["rb"], d["T1"], 4, 3)                     # 75
    shoot(a, S["rb"], d["T2"], 5, 4)                     # 100
    shoot(a, S["rb"], d["T3"], 6, 2)                     # 50
    a.post("/admin/users/save", data=dict(username="t_mem", full_name="t_mem", role="member", department_id=d["T3"], password="Start1234"))
    S["mem"] = client("t_mem", "Start1234", "Member5S99x")


def test_02_display_link_and_access():
    a = S["admin"]
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/tv").headers["location"] == "/login"                   # ไม่มีรหัสและไม่ได้เข้าระบบ
    assert anon.get("/api/tv/data").status_code == 403
    page = a.get("/admin/tv")
    assert page.status_code == 200 and "ลิงก์สำหรับจอ TV" in page.text
    key = settings_store.load()["tv_key"]
    assert len(key) >= 20 and f"/tv?key={key}" in page.text
    assert anon.get("/tv?key=wrong").status_code == 403
    screen = TestClient(app, follow_redirects=False)                         # จอ TV: ไม่มีบัญชีผู้ใช้
    r = screen.get(f"/tv?key={key}")
    assert r.status_code == 200 and 'id="tv-config"' in r.text and "/static/tv.js" in r.text and "tvkey" in r.headers.get("set-cookie", "")
    cfg = json.loads(r.text.split('id="tv-config" type="application/json">')[1].split("</script>")[0])
    assert cfg["lang"] == "th" and cfg["theme"] == "light" and cfg["slides"][0] == "ranking" and cfg["title"] == "โรงงานทดสอบ"
    assert screen.get("/api/tv/data").status_code == 200                     # ใช้คุกกี้ของจอ ไม่ต้องมีรหัสในทุกคำขอ
    assert screen.get("/").headers["location"].startswith("/login")          # รหัสของจอไม่ได้ให้สิทธิ์หน้าอื่น
    assert screen.get("/admin/tv").status_code in (303, 401, 403)
    S["screen"], S["key"] = screen, key
    assert a.get("/tv").status_code == 200                                   # ผู้ดูแลที่เข้าระบบอยู่เปิดได้โดยไม่ใช้รหัส
    assert S["mem"].get("/tv").status_code == 200                            # ตัวแทนแผนก: ได้เพราะตั้งให้ดูผลของทุกแผนก
    save(member_see_all=False)
    assert S["mem"].get("/tv").status_code == 403 and S["mem"].get("/api/tv/data").status_code == 403
    save(member_see_all=True)
    for path in ("/static/tv.js", "/static/tv.css"):
        assert anon.get(path).status_code == 200


def test_03_ranking_deltas_and_history():
    d = S["d"]
    data = tv()
    assert data["round"]["name"] == "รอบ TV ข" and data["round"]["status"] == "open" and data["org"] == "โรงงานทดสอบ"
    rows = data["rows"]
    assert [(r["rank"], r["code"], r["avg"], r["band"]) for r in rows] == [(1, "T2", 100.0, "good"), (2, "T1", 75.0, "mid"), (3, "T3", 50.0, "low")]
    by = {r["code"]: r for r in rows}
    assert (by["T2"]["delta"], by["T2"]["rank_delta"]) == (25.0, 1)           # ขึ้นจากอันดับ 2 เป็น 1
    assert (by["T1"]["delta"], by["T1"]["rank_delta"]) == (-25.0, -1)
    assert (by["T3"]["delta"], by["T3"]["rank_delta"]) == (0.0, 0)
    assert by["T1"]["name_en"] == "Welding" and by["T3"]["name_en"] == ""
    h = data["history"]
    assert [r["name"] for r in h["rounds"]][-2:] == ["รอบ TV ก", "รอบ TV ข"]
    series = {s["name"]: s for s in h["series"]}
    assert series["งานเชื่อม"]["avg"][-2:] == [100.0, 75.0] and series["งานเชื่อม"]["rank"][-2:] == [1, 2]
    assert series["งานประกอบ"]["avg"][-2:] == [75.0, 100.0] and series["งานประกอบ"]["rank"][-2:] == [2, 1]
    assert [s["name"] for s in h["series"]][:3] == ["งานประกอบ", "งานเชื่อม", "คลังสินค้าสำเร็จรูป"]      # เรียงตามอันดับปัจจุบัน
    assert data["summary"]["good"] == 1 and data["summary"]["mid"] == 1 and data["summary"]["low"] == 1 and data["summary"]["avg"] == 75.0
    assert data["bands"] == {"good": 80, "mid": 60} and [c["name_en"] for c in data["cats"]][:2] == ["Seiri", "Seiton"]
    assert len(tv(rounds=2)["history"]["rounds"]) == 2 and len(tv(rounds=99)["history"]["rounds"]) <= 12
    # เลือกรอบที่แสดง
    save(tv_round="closed")
    old = tv()
    assert old["round"]["name"] == "รอบ TV ก" and [r["code"] for r in old["rows"]] == ["T1", "T2", "T3"]
    save(tv_round="auto", ranking_visibility="closed")                        # อันดับเปิดเผยหลังปิดรอบ: จอไม่แสดงรอบที่ยังเปิด
    assert tv()["round"]["name"] == "รอบ TV ก"
    save(ranking_visibility="always")
    assert tv()["round"]["name"] == "รอบ TV ข"
    # แผนกที่ยังไม่ส่งภาพขึ้นในรายการที่ยังไม่ถูกจัดอันดับ
    S["admin"].post("/admin/departments/save", data={"code": "T4", "name": "ซ่อมบำรุง", "name_en": "Maintenance"})
    assert [u["name_en"] for u in tv()["unranked"]] == ["Maintenance"]
    assert d["T1"] != d["T2"]


def test_04_cache_keeps_the_database_idle():
    tv()
    before = dict(tvdata.stats)
    for _ in range(5):                                    # จอถามซ้ำ: ไม่มีอะไรเปลี่ยน ไม่คำนวณใหม่
        tv()
    assert tvdata.stats["computed"] == before["computed"] and tvdata.stats["served"] == before["served"] + 5
    shoot(S["admin"], S["rb"], S["d"]["T3"], 40, 4)       # มีภาพใหม่ได้คะแนน: T3 = (50 + 100) / 2 = 75
    fresh = tv()
    assert tvdata.stats["computed"] == before["computed"] + 1
    by = {r["code"]: r for r in fresh["rows"]}
    assert by["T3"]["avg"] == 75.0 and by["T3"]["delta"] == 25.0 and by["T3"]["scored"] == 2
    assert [r["rank"] for r in fresh["rows"]] == [1, 2, 3] and fresh["rows"][0]["code"] == "T2"


def test_05_admin_settings_and_link_rotation():
    a = S["admin"]
    form = {"tv_title": "ผลการตรวจ 5ส", "tv_title_en": "5S Audit Results", "tv_lang": "en", "tv_theme": "dark", "tv_round": "open",
            "tv_rounds": 99, "tv_seconds": 2, "tv_rows": 10, "tv_refresh_min": 15, "tv_hours": "7:00 - 19:30",
            "slides": ["trend", "ranking", "bogus"], "tv_clock": "1"}
    assert S["mem"].post("/admin/tv", data=form).status_code == 403
    assert a.post("/admin/tv", data=form).status_code == 303
    s = settings_store.load()
    assert (s["tv_title_en"], s["tv_lang"], s["tv_theme"], s["tv_round"]) == ("5S Audit Results", "en", "dark", "open")
    assert (s["tv_rounds"], s["tv_seconds"], s["tv_rows"], s["tv_refresh_min"], s["tv_hours"]) == (12, 5, 10, 15, "07:00-19:30")
    assert s["tv_slides"] == ["trend", "ranking"] and s["tv_clock"] is True and s["tv_unranked"] is False
    r = S["screen"].get("/tv")
    cfg = json.loads(r.text.split('id="tv-config" type="application/json">')[1].split("</script>")[0])
    assert (cfg["lang"], cfg["theme"], cfg["slides"], cfg["rows"], cfg["title_en"], cfg["hours"]) == \
        ("en", "dark", ["trend", "ranking"], 10, "5S Audit Results", "07:00-19:30")
    assert 'class="tv theme-dark"' in r.text and '<html lang="en">' in r.text
    a.post("/admin/tv", data=dict(form, tv_hours="25:99-1", slides=[]))
    assert settings_store.load()["tv_hours"] == "" and settings_store.load()["tv_slides"] == ["ranking"]
    page = a.get("/admin/tv").text
    assert 'name="tv_title_en" value="5S Audit Results"' in page and "สร้างลิงก์ใหม่" in page
    # สร้างลิงก์ใหม่: ลิงก์เดิมและคุกกี้ของจอเดิมใช้ไม่ได้ทันที
    assert a.post("/admin/tv/key").status_code == 303
    new = settings_store.load()["tv_key"]
    assert new != S["key"] and S["screen"].get("/api/tv/data").status_code == 403 and S["screen"].get("/tv").status_code == 403
    again = TestClient(app, follow_redirects=False)
    assert again.get(f"/tv?key={new}").status_code == 200 and again.get("/api/tv/data").status_code == 200
    S["screen"] = again
    a.post("/admin/tv", data={"tv_lang": "th", "tv_theme": "light", "tv_round": "auto", "tv_rounds": 6, "tv_seconds": 15, "tv_rows": 8,
                              "tv_refresh_min": 5, "slides": list(tvdata.SLIDES), "tv_clock": "1", "tv_unranked": "1"})


def test_06_language_switch():
    a, d = S["admin"], S["d"]
    thai = a.get("/ranking").text
    assert '<html lang="th"' in thai and ">อันดับ</a>" in thai and 'href="/lang/en?next=/ranking"' in thai and ">EN</a>" in thai
    r = a.get("/lang/en?next=/ranking")
    assert r.status_code == 303 and r.headers["location"] == "/ranking" and "lang=en" in r.headers["set-cookie"]
    en = a.get("/ranking").text
    assert '<html lang="en"' in en and ">Ranking</a>" in en and ">Home</a>" in en and ">Actions</a>" in en and ">ไทย</a>" in en
    assert "5S ranking" in en and "How scores are calculated" in en and "Trend across rounds" in en
    assert ">Welding<" in en and ">Assembly<" in en and ">งานเชื่อม<" not in en          # ชื่อแผนกภาษาอังกฤษ
    assert ">คลังสินค้าสำเร็จรูป<" in en                                                # แผนกที่ไม่ได้ใส่ชื่ออังกฤษ ยังเป็นชื่อเดิม
    # ค่าในฟอร์มไม่ถูกแปล: ตัวเลือกยังส่งค่าเดิม และการส่งภาพยังทำงาน
    cap = a.get("/capture").text
    assert "Capture for audit" in cap and 'value="สายการผลิต"' in cap and "Department that owns the area" in cap
    assert f'<option value="{d["T1"]}"' in cap
    pid = shoot(a, S["rb"], d["T1"], 60, 3)
    page = a.get(f"/photos/{pid}").text
    assert "Reasons and advice by criterion" in page and "Sent by" in page and ">Scored<" in page
    for url in ("/", "/photos", "/trend", "/actions", "/verify", f"/dashboard?round={S['rb']}", f"/rounds/{S['rb']}/dept/{d['T1']}",
                "/admin", "/admin/tv", "/admin/settings", "/admin/checkpoints"):
        got = a.get(url)
        assert got.status_code == 200 and "Traceback" not in got.text and '<html lang="en"' in got.text, url
    assert ">Settings and AI</a>" in a.get("/admin").text                              # เมนูย่อยของหน้าจัดการ
    login = TestClient(app, follow_redirects=False)
    login.get("/lang/en?next=/login")
    lp = login.get("/login").text
    assert ">Sign in</h1>" in lp and ">Username</span>" in lp and ">ไทย</a>" in lp
    # ลิงก์เปลี่ยนภาษาพากลับได้เฉพาะหน้าในระบบ
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "javascript:alert(1)"):
        assert a.get("/lang/en", params={"next": bad}).headers["location"] == "/", bad
    assert a.get("/lang/th?next=/").headers["location"] == "/"
    assert '<html lang="th"' in a.get("/ranking").text and ">Ranking</a>" not in a.get("/ranking").text
    # ตัวแปลไม่แตะสคริปต์ ค่าในช่องกรอก หรือข้อความบางส่วนของประโยค
    html = '<p>อันดับ ของแผนก</p><script>var s = "<b>อันดับ</b>";</script><textarea>อันดับ</textarea><option>อันดับ</option><b>อันดับ</b>'
    out = i18n.translate_html(html)
    assert out == html.replace("<b>อันดับ</b>", "<b>Ranking</b>").replace('"<b>Ranking</b>"', '"<b>อันดับ</b>"')


def test_07_english_text_for_checklist_findings():
    a, d = S["admin"], S["d"]
    a.post("/admin/checkpoints/reset")
    with dbm.SessionLocal() as s:
        k = s.query(dbm.Checkpoint).filter_by(code="C04").one()
        assert k.text_en == "Walkways are clear, with nothing placed in them"
        kid, base = k.id, dict(code="C04", crit_code=k.crit_code, text=k.text, minor_hint=k.minor_hint, major_hint=k.major_hint,
                               points=5, minor_points=3, sort_order=k.sort_order, allow_na="1")
    assert a.post("/admin/checkpoints/save", data=dict(base, id=kid, text_en="Aisles are clear")).status_code == 303
    assert 'name="text_en" value="Aisles are clear"' in a.get("/admin/checkpoints").text
    rc = new_round(a, "รอบ TV ค", mode="checklist")
    with dbm.SessionLocal() as s:
        snap = {k["code"]: k for k in s.get(dbm.Round, rc).checklist}
        assert snap["C04"]["text_en"] == "Aisles are clear" and snap["C08"]["text_en"] == "No scrap or litter on the floor"
    AI["status"] = {"C04": "major", "C08": "minor"}
    shoot(a, rc, d["T1"], 70)
    shoot(a, rc, d["T2"], 71)
    data = tv()
    assert data["round"]["name"] == "รอบ TV ค" and data["round"]["mode"] == "checklist" and data["checklist"] is True
    top = data["pareto"][0]
    assert (top["code"], top["text_en"], top["count"], top["major"], top["areas"]) == ("C04", "Aisles are clear", 2, 2, 2)
    assert data["pareto"][1]["text_en"] == "No scrap or litter on the floor" and len(data["history"]["rounds"]) >= 3
    # ยืนยันผล -> มีงานแก้ไข -> จอแสดงจำนวนงานของแผนก
    with dbm.SessionLocal() as s:
        pid = s.query(dbm.Photo).filter_by(round_id=rc, department_id=d["T2"]).one().id
    a.post("/admin/users/save", data=dict(username="t_aud", full_name="t_aud", role="auditor", password="Start1234", perm_score="1"))
    aud = client("t_aud", "Start1234", "Member5S99x")
    assert aud.post(f"/admin/photos/{pid}/verify").status_code == 303
    data = tv()
    by = {r["code"]: r for r in data["rows"]}
    assert data["actions"]["open"] == 2 and by["T2"]["open"] == 2 and by["T1"]["open"] == 0
    AI["status"] = {}


def test_08_upgrade_from_1_3_database():
    from sqlalchemy import inspect, text
    a = S["admin"]
    with dbm.engine.begin() as conn:
        n = conn.execute(text("select count(*) from photos")).scalar()
        conn.execute(text("ALTER TABLE departments DROP COLUMN name_en"))
        conn.execute(text("ALTER TABLE checkpoints DROP COLUMN text_en"))
    dbm.init_db()
    insp = inspect(dbm.engine)
    assert "name_en" in {c["name"] for c in insp.get_columns("departments")}
    assert "text_en" in {c["name"] for c in insp.get_columns("checkpoints")}
    with dbm.engine.begin() as conn:
        assert conn.execute(text("select count(*) from photos")).scalar() == n
        assert conn.execute(text("select text_en from checkpoints where code = 'C08'")).scalar() == "No scrap or litter on the floor"
    data = tv()
    assert data["ok"] is True and all(r["name_en"] == "" for r in data["rows"])
    for url in ("/", "/ranking", "/tv", "/admin/tv", "/admin/departments", "/admin/checkpoints"):
        assert a.get(url).status_code == 200, url

"""ชุดทดสอบของรุ่น 1.7: อันดับแยกกลุ่ม ความสดของภาพจากคลังภาพ การขอทบทวนผล ป้าย QR ของจุดตรวจ และการเตือนแผนกก่อนปิดรอบ"""
import io
import json
import os
import random
import re
import tempfile
from datetime import timedelta

import httpx
import pytest
from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v17_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, db as dbm, presets, scoring, settings_store, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
AI = {"levels": []}
S = {}


def jpeg() -> bytes:
    im = Image.new("RGB", (1000, 750), (random.randrange(256), random.randrange(256), random.randrange(256)))
    for _ in range(25):
        x, y = random.randrange(900), random.randrange(650)
        im.paste((random.randrange(256), random.randrange(256), random.randrange(256)), (x, y, x + 80, y + 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ai_handler(request: httpx.Request) -> httpx.Response:
    prompt = json.loads(request.content)["contents"][0]["parts"][-1]["text"]
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


def shoot(c, dept, level=None, expect=200, **kw):
    if level is not None:
        AI["levels"].append(level)
    data = dict(round_id=S["rid"], department_id=dept, area_name=kw.pop("area_name", f"จุด {random.randrange(10 ** 6)}"), area_type="สายการผลิต")
    data.update(kw)
    r = c.post("/api/photos", data=data, files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == expect, r.text
    if expect != 200:
        if level is not None:
            AI["levels"].pop()
        return r
    drain()
    return r.json()["id"]


def photo(pid):
    with dbm.SessionLocal() as s:
        return s.get(dbm.Photo, pid)


def thai_now():
    return dbm.now() + timedelta(hours=7)


def test_01_ranking_within_groups():
    a = admin()
    S["admin"] = a
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status.in_(["open", "planned"])):
            r.status = "closed"
        for d in s.query(dbm.Department).all():
            d.active = False
        for c in s.query(dbm.Camera).all():
            c.active = False
        s.query(dbm.Channel).delete()
        s.commit()
    save(ai1_type="gemini", ai1_key="g", ai1_model="gemini-flash", ai2_type="none", ai_passes=1, ai_daily=100000, scoring_mode="level",
         ranking_visibility="always", member_see_all=True, verify_required=False, require_coverage=False, allow_free_area=True,
         max_photos_per_dept=0, storage_budget_mb=350, retention_days=0, area_types=["สายการผลิต", "คลัง"], rounds_repeat="off",
         auto_actions=False, gallery_max_age_h=0, gallery_stale="flag", dept_remind_days=3, tv_group="", after_replaces=True, preset="")
    for code, name, group in (("G1", "ปั๊มขึ้นรูป", "ผลิต"), ("G2", "เชื่อมประกอบ", "ผลิต"), ("G3", "ซ่อมบำรุงกลาง", "สนับสนุน"), ("G4", "ธุรการ", "")):
        assert a.post("/admin/departments/save", data={"code": code, "name": name, "group_name": group}).status_code == 303
    with dbm.SessionLocal() as s:
        S["d"] = {d.code: d.id for d in s.query(dbm.Department).filter(dbm.Department.code.in_(["G1", "G2", "G3", "G4"]))}
        assert s.get(dbm.Department, S["d"]["G3"]).group_name == "สนับสนุน"
    d = S["d"]
    assert 'name="group_name" value="ผลิต"' in a.get("/admin/departments").text
    a.post("/admin/rounds/save", data={"name": "รอบ 1.7", "min_photos": 1, "mode": "level"})
    with dbm.SessionLocal() as s:
        S["rid"] = s.query(dbm.Round).filter_by(name="รอบ 1.7").one().id
    shoot(a, d["G3"], 4)          # 100
    shoot(a, d["G1"], 3)          # 75
    shoot(a, d["G4"], 3)          # 75
    shoot(a, d["G2"], 2)          # 50
    with dbm.SessionLocal() as s:
        rk = scoring.round_ranking(s, s.get(dbm.Round, S["rid"]))
    got = {r["dept"].code: (r["rank"], r["group"], r["group_rank"]) for r in rk["ranked"]}
    assert got["G3"] == (1, "สนับสนุน", 1) and got["G1"][1:] == ("ผลิต", 1) and got["G2"] == (4, "ผลิต", 2) and got["G4"][1:] == ("", 1)
    assert rk["groups"] == ["ผลิต", "สนับสนุน"]
    page = a.get(f"/ranking?round={S['rid']}").text
    assert "กลุ่มผลิต อันดับ 1" in page and "กลุ่มผลิต อันดับ 2" in page and "กลุ่มสนับสนุน" in page and ">ทุกแผนก</a>" in page
    only = a.get(f"/ranking?round={S['rid']}&group=ผลิต").text
    assert "ปั๊มขึ้นรูป" in only and "เชื่อมประกอบ" in only and "ซ่อมบำรุงกลาง" not in only and "อันดับรวม 4" in only
    assert re.search(r'rank-no">1</span>\s*<span>\s*<span class="rank-name">ปั๊มขึ้นรูป', only)
    assert "ซ่อมบำรุงกลาง" in a.get(f"/ranking?round={S['rid']}&group=ไม่มีกลุ่มนี้").text            # กลุ่มที่ไม่มี = แสดงทั้งหมด
    rows = [ln.split(",") for ln in a.get(f"/rounds/{S['rid']}/export/ranking.csv").content.decode("utf-8-sig").splitlines()]
    assert rows[0][-3:] == ["กลุ่ม", "อันดับในกลุ่ม", "สถานะ"]
    line = next(r for r in rows if r[1] == "G2")
    assert line[0] == "4" and line[-3:] == ["ผลิต", "2", "จัดอันดับแล้ว"]
    # จอแสดงผล
    a.get("/admin/tv")
    key = settings_store.load()["tv_key"]
    screen = TestClient(app, follow_redirects=False)
    screen.get(f"/tv?key={key}")
    data = screen.get("/api/tv/data").json()
    by = {r["code"]: r for r in data["rows"]}
    assert (by["G2"]["group"], by["G2"]["group_rank"]) == ("ผลิต", 2) and data["groups"] == ["ผลิต", "สนับสนุน"]
    assert a.post("/admin/tv", data={"tv_group": "ผลิต", "slides": ["ranking"], "tv_clock": "1"}).status_code == 303
    cfg = json.loads(screen.get("/tv").text.split('id="tv-config" type="application/json">')[1].split("</script>")[0])
    assert cfg["group"] == "ผลิต" and 'name="tv_group" value="ผลิต"' in a.get("/admin/tv").text
    a.post("/admin/tv", data={"tv_group": "", "slides": list(__import__("app.tvdata", fromlist=["SLIDES"]).SLIDES), "tv_clock": "1", "tv_unranked": "1"})
    a.post("/admin/users/save", data=dict(username="g_mem", full_name="สมชาย ปั๊ม", role="member", department_id=d["G1"], password="Start1234"))
    a.post("/admin/users/save", data=dict(username="g_mem2", full_name="สมหญิง เชื่อม", role="member", department_id=d["G2"], password="Start1234"))
    a.post("/admin/users/save", data=dict(username="g_aud", full_name="กรรมการ ก", role="auditor", password="Start1234", perm_score="1"))
    S["mem"], S["mem2"], S["aud"] = (client(u, "Start1234", "Member5S99x") for u in ("g_mem", "g_mem2", "g_aud"))


def test_02_gallery_photos_must_be_recent():
    a, mem, d = S["admin"], S["mem"], S["d"]
    old = (thai_now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    fresh = (thai_now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    pid = shoot(mem, d["G1"], 3, source="gallery", shot_at=old)                 # ยังไม่ได้ตั้งให้ตรวจ
    assert photo(pid).stale is False and photo(pid).shot_at is not None
    form = {"org_name": "x", "area_types": "สายการผลิต\nคลัง", "ai1_type": "gemini", "ai1_model": "gemini-flash", "ai_rpm": 600, "ai_daily": 100000,
            "ai_passes": "1", "allow_gallery": "1", "allow_free_area": "1", "member_see_all": "1", "storage_budget_mb": 350, "after_replaces": "1",
            "gallery_max_age_h": 24, "gallery_stale": "flag", "dept_remind_days": 3, "scoring_mode": "level"}
    assert a.post("/admin/settings", data=form).status_code == 303
    s_ = settings_store.load()
    assert (s_["gallery_max_age_h"], s_["gallery_stale"], s_["dept_remind_days"]) == (24, "flag", 3)
    assert 'name="gallery_max_age_h" value="24"' in a.get("/admin/settings").text
    r = mem.post("/api/photos", data=dict(round_id=S["rid"], department_id=d["G1"], area_name="ภาพเก่า", area_type="สายการผลิต",
                                          source="gallery", shot_at=old), files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200 and r.json()["stale"] is True
    drain()
    stale_id = r.json()["id"]
    p = photo(stale_id)
    assert p.stale is True and p.source == "gallery" and abs((thai_now() - p.shot_at).days - 3) <= 1
    page = a.get(f"/photos/{stale_id}").text
    assert "ถ่ายเมื่อ" in page and "ถ่ายไว้นานแล้ว" in page
    assert "ภาพจากคลังภาพ ถ่ายไว้เมื่อ" in S["aud"].get(f"/verify?round={S['rid']}").text
    listed = a.get(f"/photos?round={S['rid']}&status=stale").text
    assert f'href="/photos/{stale_id}"' in listed and f'href="/photos/{pid}"' not in listed
    assert photo(shoot(mem, d["G1"], 3, source="gallery", shot_at=fresh)).stale is False       # ภาพใหม่จากคลังภาพ
    assert photo(shoot(mem, d["G1"], 3, source="mobile", shot_at=old)).stale is False           # ถ่ายจากกล้องโดยตรง ไม่ตรวจ
    for bad in ("", "ไม่ใช่วันที่", "2099-01-01T10:00:00", "1999-01-01T10:00:00"):               # ไม่ทราบเวลา: รับ ไม่ติดป้าย
        q = photo(shoot(mem, d["G1"], 3, source="gallery", shot_at=bad))
        assert q.stale is False and q.shot_at is None, bad
    save(gallery_stale="reject")
    r = shoot(mem, d["G1"], expect=400, source="gallery", shot_at=old)
    assert "เกิน 24 ชั่วโมง" in r.json()["detail"] and "ถ่ายภาพใหม่" in r.json()["detail"]
    assert isinstance(shoot(mem, d["G1"], 3, source="gallery", shot_at=fresh), int)
    save(gallery_stale="flag", gallery_max_age_h=0)
    S["stale"] = stale_id


def test_03_departments_can_appeal_a_result():
    a, mem, mem2, aud, d = S["admin"], S["mem"], S["mem2"], S["aud"], S["d"]
    a.post("/admin/notifications/save", data=dict(kind="webhook", name="แผนกปั๊ม", cfg_webhook_url="https://hooks.example.test/g1", active="1",
                                                  department_id=d["G1"], ev_action="1"))
    pid = shoot(mem, d["G1"], 1, area_name="ชั้นวางแม่พิมพ์")
    page = mem.get(f"/photos/{pid}").text
    assert "ขอทบทวนผล" in page and f'action="/photos/{pid}/appeal"' in page
    assert f'action="/photos/{pid}/appeal"' not in mem2.get(f"/photos/{pid}").text            # แผนกอื่นไม่มีปุ่ม
    assert mem2.post(f"/photos/{pid}/appeal", data={"note": "ขอทบทวนแทนแผนกอื่น ยาวพอ"}).status_code == 403
    mem.post(f"/photos/{pid}/appeal", data={"note": "สั้น"})
    assert photo(pid).appeal_status == ""
    note = "ข้อ สะดวก: กล่องที่เห็นอยู่ในพื้นที่วางของ ไม่ได้อยู่ในทางเดิน"
    assert mem.post(f"/photos/{pid}/appeal", data={"note": note}).status_code == 303
    p = photo(pid)
    assert (p.appeal_status, p.appeal_note, p.appeal_by) == ("open", note, "สมชาย ปั๊ม") and p.appeal_at is not None
    mem.post(f"/photos/{pid}/appeal", data={"note": "ขอซ้ำอีกครั้งระหว่างที่ยังรออยู่"})
    assert photo(pid).appeal_note == note                                                     # มีคำขอที่รออยู่ ขอซ้ำไม่ได้
    with dbm.SessionLocal() as s:
        assert "ขอทบทวนผลของภาพ ชั้นวางแม่พิมพ์" in s.query(dbm.AuditLog).filter_by(action="alert").order_by(dbm.AuditLog.id.desc()).first().detail
    q = aud.get(f"/verify?round={S['rid']}").text
    assert "แผนกขอทบทวน" in q and note in q and q.index("ชั้นวางแม่พิมพ์") < q.index("ภาพเก่า")      # คำขอทบทวนขึ้นก่อน
    assert f'href="/photos/{pid}"' in a.get(f"/photos?round={S['rid']}&status=appeal").text
    view = aud.get(f"/photos/{pid}").text
    assert "รอกรรมการทบทวน" in view and f'action="/admin/photos/{pid}/appeal-reply"' in view
    aud.post(f"/admin/photos/{pid}/appeal-reply", data={"reply": "ok"})
    assert photo(pid).appeal_status == "open"                                                 # ต้องมีคำอธิบาย
    assert mem.post(f"/admin/photos/{pid}/appeal-reply", data={"reply": "ตอบเองไม่ได้"}).status_code == 403
    assert aud.post(f"/admin/photos/{pid}/appeal-reply", data={"reply": "ในภาพยังเห็นกล่องวางล้ำเส้นทางเดินด้านซ้าย"}).status_code == 303
    p = photo(pid)
    assert p.appeal_status == "resolved" and p.appeal_reply == "คงผลเดิม: ในภาพยังเห็นกล่องวางล้ำเส้นทางเดินด้านซ้าย"
    assert p.appeal_closed_by == "กรรมการ ก" and p.verified_at is not None and p.verified_by == "กรรมการ ก"
    seen = mem.get(f"/photos/{pid}").text
    assert "ทบทวนแล้ว" in seen and "คำตอบของกรรมการ" in seen and "ในภาพยังเห็นกล่อง" in seen and "ขอทบทวนอีกครั้ง" in seen
    with dbm.SessionLocal() as s:
        ev = s.query(dbm.NotifyEvent).filter_by(kind="action", department_id=d["G1"]).order_by(dbm.NotifyEvent.id.desc()).first()
        assert "ผลการทบทวนภาพ ชั้นวางแม่พิมพ์" in json.dumps(ev.payload, ensure_ascii=False)
        acts = [x.action for x in s.query(dbm.AuditLog).filter(dbm.AuditLog.action.in_(["appeal_photo", "resolve_appeal"]))]
        assert "appeal_photo" in acts and "resolve_appeal" in acts
    # กรรมการเห็นด้วยกับแผนก: ปรับผล เหตุผลที่ปรับคือคำตอบ
    p2 = shoot(mem, d["G1"], 1, area_name="โต๊ะตรวจชิ้นงาน")
    mem.post(f"/photos/{p2}/appeal", data={"note": "พื้นที่นี้เก็บเรียบร้อยแล้วก่อนถ่าย ผลต่ำเกินจริง"})
    with dbm.SessionLocal() as s:
        codes = [c["code"] for c in s.get(dbm.Round, S["rid"]).rubric]
    form = {f"level_{c}": "4" for c in codes}
    form["note"] = "ดูภาพแล้วเห็นด้วยกับแผนก"
    assert aud.post(f"/admin/photos/{p2}/override", data=form).status_code == 303
    p = photo(p2)
    assert p.appeal_status == "resolved" and p.appeal_reply == "ปรับผลแล้ว: ดูภาพแล้วเห็นด้วยกับแผนก" and p.overridden and p.percent == 100
    # ยืนยันผลตามปกติก็ปิดคำขอ
    p3 = shoot(mem, d["G1"], 2, area_name="จุดพักชิ้นงาน")
    mem.post(f"/photos/{p3}/appeal", data={"note": "ขอให้กรรมการดูภาพนี้อีกครั้งหนึ่ง"})
    aud.post(f"/admin/photos/{p3}/verify")
    assert photo(p3).appeal_status == "resolved" and photo(p3).appeal_reply.startswith("คงผลเดิม")
    # ขอทบทวนได้อีกครั้งหลังได้คำตอบ และกรรมการที่ส่งภาพเองตอบคำขอของภาพนั้นไม่ได้
    assert mem.post(f"/photos/{p3}/appeal", data={"note": "ขอทบทวนอีกครั้ง มีข้อมูลเพิ่มเติม"}).status_code == 303
    assert photo(p3).appeal_status == "open" and photo(p3).appeal_reply == ""
    aud.post(f"/admin/photos/{p3}/verify")
    # รอบปิดแล้วขอทบทวนผ่านระบบไม่ได้
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "closed"
        s.commit()
    mem.post(f"/photos/{S['stale']}/appeal", data={"note": "ขอทบทวนหลังปิดรอบไปแล้ว ไม่ควรได้"})
    assert photo(S["stale"]).appeal_status == ""
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "open"
        s.commit()
    for url in ("/verify", f"/photos/{pid}", f"/photos?round={S['rid']}&status=appeal", f"/rounds/{S['rid']}/report"):
        assert a.get(url).status_code == 200, url


def test_04_qr_labels_open_the_right_audit_point():
    a, mem, mem2, d = S["admin"], S["mem"], S["mem2"], S["d"]
    a.post("/admin/areas/save", data=dict(department_id=d["G1"], name="เครื่องปั๊ม 300 ตัน", area_type="สายการผลิต", required="1", active="1"))
    a.post("/admin/areas/save", data=dict(department_id=d["G2"], name="ตู้เชื่อมโรบอต 2", area_type="สายการผลิต", required="1", active="1"))
    with dbm.SessionLocal() as s:
        a1 = s.query(dbm.AuditArea).filter_by(name="เครื่องปั๊ม 300 ตัน").one().id
        a2 = s.query(dbm.AuditArea).filter_by(name="ตู้เชื่อมโรบอต 2").one().id
    save(public_url="https://5s.example.test")
    page = a.get("/admin/areas/qr")
    assert page.status_code == 200 and page.text.count("<svg") >= 2 and "เครื่องปั๊ม 300 ตัน" in page.text and "ปั๊มขึ้นรูป" in page.text
    assert "พิมพ์ป้าย QR ของจุดตรวจ" in a.get("/admin/areas").text
    only = a.get(f"/admin/areas/qr?dept={d['G2']}").text
    assert "ตู้เชื่อมโรบอต 2" in only and "เครื่องปั๊ม 300 ตัน" not in only
    assert mem.get("/admin/areas/qr").status_code == 403
    import segno                                                              # รหัสในป้ายคือลิงก์ไปหน้าถ่ายภาพของจุดนั้น
    assert segno.make(f"https://5s.example.test/capture?area={a1}", error="m").svg_inline(scale=5, border=2, dark="#1D2A2F", omitsize=True) in page.text
    cap = mem.get(f"/capture?area={a1}").text
    assert f'data-scanned="{a1}"' in cap and "เปิดจากป้าย QR ของจุดตรวจ <b>เครื่องปั๊ม 300 ตัน</b>" in cap
    assert re.search(rf'<option value="{d["G1"]}" selected', cap)
    other = mem.get(f"/capture?area={a2}").text                                # จุดตรวจของแผนกอื่น
    assert 'data-scanned=""' in other and "ป้าย QR นี้เป็นของจุดตรวจที่บัญชีของคุณส่งภาพให้ไม่ได้" in other
    assert f'data-scanned="{a2}"' in mem2.get(f"/capture?area={a2}").text
    assert 'data-scanned=""' in mem.get("/capture?area=999999").text and 'data-scanned=""' in mem.get("/capture").text
    # ยังไม่ได้เข้าระบบ: เข้าสู่ระบบแล้วไปหน้าของจุดตรวจนั้นต่อ
    anon = TestClient(app, follow_redirects=False)
    r = anon.get(f"/capture?area={a1}")
    assert r.status_code == 303 and r.headers["location"] == f"/login?next=/capture%3Farea%3D{a1}"
    assert f'name="next" value="/capture?area={a1}"' in anon.get(r.headers["location"]).text
    r = anon.post("/login", data={"username": "g_mem", "password": "Member5S99x", "next": f"/capture?area={a1}"})
    assert r.headers["location"] == f"/capture?area={a1}"
    pid = shoot(mem, d["G1"], 3, area_id=a1, area_name="")
    assert (photo(pid).area_id, photo(pid).area_name) == (a1, "เครื่องปั๊ม 300 ตัน")
    S.update(a1=a1, a2=a2)


def test_05_reminders_before_the_round_closes():
    a, d = S["admin"], S["d"]
    a.post("/admin/notifications/save", data=dict(kind="webhook", name="แผนกเชื่อม", cfg_webhook_url="https://hooks.example.test/x", active="1",
                                                  department_id=d["G2"], ev_round="1"))
    save(require_coverage=True, dept_remind_days=3, **{k: "" for k in settings_store.load() if k.startswith("_stamp_remind_")})
    with dbm.SessionLocal() as s:
        r = s.get(dbm.Round, S["rid"])
        r.end_date = thai_now().date() + timedelta(days=9)
        s.commit()
        n0 = s.query(dbm.NotifyEvent).count()
    assert worker.remind_departments(dbm.SessionLocal()) == 0                              # ยังไม่ถึงช่วงเตือน
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).end_date = thai_now().date() + timedelta(days=2)
        s.commit()
    with dbm.SessionLocal() as s:
        sent = worker.remind_departments(s)
        events = s.query(dbm.NotifyEvent).filter(dbm.NotifyEvent.id > 0).order_by(dbm.NotifyEvent.id.desc()).limit(sent).all()
        texts = {e.department_id: json.dumps(e.payload, ensure_ascii=False) for e in events}
        assert s.query(dbm.NotifyEvent).count() == n0 + sent
    # G2 มีภาพแต่ขาดจุดตรวจบังคับ, G3 และ G4 ไม่มีจุดบังคับและมีภาพแล้ว จึงไม่ถูกเตือน, G1 ส่งจุดบังคับแล้ว
    assert sent == 1 and set(texts) == {d["G2"]}
    assert "จะปิดใน 2 วัน" in texts[d["G2"]] and "ตู้เชื่อมโรบอต 2" in texts[d["G2"]] and "ยังไม่ถูกจัดอันดับ" in texts[d["G2"]]
    with dbm.SessionLocal() as s:
        assert worker.remind_departments(s) == 0                                           # วันละครั้งต่อแผนก
    out = worker.housekeeping()
    assert out["reminders"] == 0
    from app import notify
    with dbm.SessionLocal() as s:
        ev = s.query(dbm.NotifyEvent).filter_by(kind="round", department_id=d["G2"]).order_by(dbm.NotifyEvent.id.desc()).first()
        msg = notify.build_message(s, "round", [ev]) if hasattr(notify, "build_message") else None
    if msg:
        assert "ใกล้ปิดรอบ" in msg["title"] and "ตู้เชื่อมโรบอต 2" in msg["text"]
    save(dept_remind_days=0, **{k: "" for k in settings_store.load() if k.startswith("_stamp_remind_")})
    with dbm.SessionLocal() as s:
        assert worker.remind_departments(s) == 0                                           # ปิดการเตือน
    save(require_coverage=False, dept_remind_days=3)


def test_06_presets_cover_boards_and_documents():
    a, d = S["admin"], S["d"]
    for key, n in (("factory", 22), ("office", 17)):
        p = presets.preview(key)
        assert p["n_checks"] == n and p["area_types"][-1] == "บอร์ดและเอกสาร 5ส"
        assert [c[0] for c in presets.PRESETS[key]["checks"]][-3:] == ["D01", "D02", "D03"]
        assert presets.PRESETS[key]["settings"]["gallery_max_age_h"] == 48 and presets.PRESETS[key]["settings"]["dept_remind_days"] == 3
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status == "open"):
            r.status = "closed"
        s.commit()
    save(ai1_type="demo")
    assert a.post("/admin/setup/apply", data={"preset": "factory", "rules": "1", "settings": "1"}).status_code == 303
    s_ = settings_store.load()
    assert s_["gallery_max_age_h"] == 48 and "บอร์ดและเอกสาร 5ส" in s_["area_types"]
    a.post("/admin/rounds/save", data={"name": "รอบบอร์ด", "min_photos": 1})
    with dbm.SessionLocal() as s:
        rid = s.query(dbm.Round).filter_by(name="รอบบอร์ด").one().id

    def codes(area_type):
        r = a.post("/api/photos", data=dict(round_id=rid, department_id=d["G1"], area_name=f"จุด {area_type}", area_type=area_type),
                   files={"file": ("p.jpg", jpeg(), "image/jpeg")})
        assert r.status_code == 200, r.text
        drain()
        return {c["code"] for c in photo(r.json()["id"]).analysis["checks"]}
    board, line = codes("บอร์ดและเอกสาร 5ส"), codes("สายการผลิต")
    assert {"D01", "D02", "D03"} <= board and "C02" not in board and not ({"D01", "D02", "D03"} & line)
    assert "บอร์ด 5ส แสดงผลการตรวจ" in a.get("/admin/setup").text
    save(verify_required=False, rounds_repeat="off", preset="", gallery_max_age_h=0)


def test_07_upgrade_from_1_6_database():
    from sqlalchemy import inspect, text
    cols = (("departments", "group_name"), ("photos", "shot_at"), ("photos", "stale"), ("photos", "appeal_status"), ("photos", "appeal_note"),
            ("photos", "appeal_by"), ("photos", "appeal_at"), ("photos", "appeal_reply"), ("photos", "appeal_closed_by"), ("photos", "appeal_closed_at"))
    with dbm.engine.begin() as conn:
        n = conn.execute(text("select count(*) from photos")).scalar()
        for table, col in cols:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
    dbm.init_db()
    insp = inspect(dbm.engine)
    assert {c for t, c in cols if t == "photos"} <= {c["name"] for c in insp.get_columns("photos")}
    assert "group_name" in {c["name"] for c in insp.get_columns("departments")}
    with dbm.engine.begin() as conn:
        assert conn.execute(text("select count(*) from photos")).scalar() == n
    a = S["admin"]
    for url in ("/", "/ranking", "/verify", "/photos", "/capture", "/admin/departments", "/admin/areas/qr", "/tv", "/admin/settings"):
        assert a.get(url).status_code == 200, url
    with dbm.SessionLocal() as s:
        rk = scoring.round_ranking(s, s.query(dbm.Round).order_by(dbm.Round.id.desc()).first())
        assert rk["groups"] == [] and all(r["group_rank"] is not None for r in rk["ranked"])

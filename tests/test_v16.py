"""ชุดทดสอบของรุ่น 1.6: ชุดตั้งค่าเริ่มต้น โรงงาน และ สำนักงาน และหน้า เริ่มต้นใช้งาน"""
import io
import os
import random
import tempfile

from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v16_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, db as dbm, presets, settings_store, worker  # noqa: E402
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


def save(**kw):
    with dbm.SessionLocal() as s:
        settings_store.save(s, kw)


def codes():
    with dbm.SessionLocal() as s:
        return [k.code for k in s.query(dbm.Checkpoint).filter(dbm.Checkpoint.area_id.is_(None)).order_by(dbm.Checkpoint.sort_order)]


def shoot(c, rid, dept, area_type):
    r = c.post("/api/photos", data=dict(round_id=rid, department_id=dept, area_name=f"จุด {area_type}", area_type=area_type),
               files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    n = 0
    while worker.process_one() and n < 20:
        n += 1
    with dbm.SessionLocal() as s:
        return s.get(dbm.Photo, r.json()["id"])


def test_01_getting_started_page_explains_the_structure():
    a = admin()
    S["admin"] = a
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status.in_(["open", "planned"])):
            r.status = "closed"
        s.commit()
    save(preset="", ai1_type="demo", ai2_type="none", ai_passes=1, ai_daily=100000, scoring_mode="level", verify_required=False,
         area_types=["สายการผลิต", "คลัง"], rounds_repeat="off", allow_free_area=True, max_photos_per_dept=0, storage_budget_mb=350,
         retention_days=0, ai_extra="", member_see_all=True)
    assert "ยังไม่ได้เลือกชุดตั้งค่าเริ่มต้น" in a.get("/admin").text and 'href="/admin/setup"' in a.get("/admin").text
    page = a.get("/admin/setup")
    assert page.status_code == 200
    for text in ("โครงสร้างของการตั้งค่า", "ชุดโรงงาน", "ชุดสำนักงาน", "ดูรายการตรวจทั้ง 24 ข้อ", "ดูรายการตรวจทั้ง 19 ข้อ", "สิ่งที่ต้องทำเอง",
                 "ทางเดินโล่ง ไม่มีสิ่งของวางอยู่ในทางเดิน", "ห้องประชุมพร้อมใช้", "ไม่วางทับหรือคร่อมเส้นเหลือง", "ระบบคำนวณเอง"):
        assert text in page.text, text
    a.post("/admin/departments/save", data={"code": "P1", "name": "แผนกชุดตั้งค่า"})
    with dbm.SessionLocal() as s:
        S["dept"] = s.query(dbm.Department).filter_by(code="P1").one().id
    a.post("/admin/users/save", data=dict(username="p_mem", full_name="p_mem", role="member", department_id=S["dept"], password="Start1234"))
    S["mem"] = client("p_mem", "Start1234", "Member5S99x")
    assert S["mem"].get("/admin/setup").status_code == 403
    assert S["mem"].post("/admin/setup/apply", data={"preset": "factory", "rules": "1"}).status_code == 403
    assert a.post("/admin/setup/apply", data={"preset": "nope", "rules": "1"}).status_code == 400
    before = codes()
    a.post("/admin/setup/apply", data={"preset": "factory"})                      # ไม่ติ๊กอะไร: ไม่เปลี่ยน
    assert codes() == before and settings_store.load()["preset"] == ""


def test_02_factory_preset_sets_everything_at_once():
    a, dept = S["admin"], S["dept"]
    a.post("/admin/criteria/save", data={"code": "X9", "name": "หมวดที่เพิ่มเอง", "max_score": 10, "level_0": "a", "level_1": "b",
                                         "level_2": "c", "level_3": "d", "level_4": "e"})
    a.post("/admin/areas/save", data=dict(department_id=dept, name="จุดมีโซน", area_type="คลัง", required="1", active="1"))
    with dbm.SessionLocal() as s:
        aid = s.query(dbm.AuditArea).filter_by(name="จุดมีโซน").one().id
        s.add(dbm.Checkpoint(code="ZP1", crit_code="S2", text="โซนเดิม", area_id=aid, zone=[10, 10, 500, 500], points=5, minor_points=3))
        s.commit()
    a.post("/admin/rounds/save", data={"name": "รอบก่อนใช้ชุดตั้งค่า", "min_photos": 1, "mode": "checklist"})
    with dbm.SessionLocal() as s:
        old = s.query(dbm.Round).filter_by(name="รอบก่อนใช้ชุดตั้งค่า").one()
        old_id, old_n, old_rev = old.id, len(old.checklist), old.rule_rev
    rev = settings_store.load()["_rule_rev"]
    assert a.post("/admin/setup/apply", data={"preset": "factory", "rules": "1", "settings": "1"}).status_code == 303
    s_ = settings_store.load()
    assert s_["preset"] == "factory" and s_["_rule_rev"] == rev + 1
    got = codes()
    assert len(got) == 24 and got[:3] == ["C01", "C02", "C03"] and {"C14", "C15", "C16", "C17", "O01", "O02", "O04", "O06"} <= set(got)
    with dbm.SessionLocal() as s:
        crit = {c.code: c for c in s.query(dbm.Criterion).all()}
        assert [crit[c].kind for c in ("S1", "S2", "S3", "S4", "S5")] == ["ai", "ai", "ai", "ai", "sustain"]
        assert all(crit[c].active and crit[c].max_score == 20 for c in ("S1", "S2", "S3", "S4", "S5")) and crit["X9"].active is False
        c13 = s.query(dbm.Checkpoint).filter_by(code="C13").one()
        assert (c13.points, c13.minor_points) == (10, 6.0) and c13.text_en.startswith("Fire extinguishers")
        assert s.query(dbm.Checkpoint).filter_by(code="C02").one().area_types == ["สายการผลิต", "คลังสินค้า", "ซ่อมบำรุง"]
        assert s.query(dbm.Checkpoint).filter_by(code="ZP1").count() == 1                  # โซนของจุดตรวจไม่ถูกแตะ
        r = s.get(dbm.Round, old_id)
        assert (len(r.checklist), r.rule_rev) == (old_n, old_rev)                           # รอบที่เปิดอยู่ยังใช้ชุดเดิม
    assert (s_["scoring_mode"], s_["verify_required"], s_["auto_actions"], s_["rounds_repeat"]) == ("checklist", True, True, "monthly")
    assert s_["area_types"] == ["สายการผลิต", "คลังสินค้า", "ซ่อมบำรุง", "ทางเดินและพื้นที่ส่วนกลาง", "สำนักงานในโรงงาน", "บอร์ดและเอกสาร 5ส", "คลัง"]     # ประเภทเดิมยังอยู่
    assert s_["cam_schedule"] == {"times": [], "random": 2, "between": "08:30-16:30", "days": [0, 1, 2, 3, 4]}
    assert "C05" in s_["ai_extra"] and "เพียงข้อเดียว" in s_["ai_extra"]
    assert "ยังไม่ได้เลือกชุดตั้งค่าเริ่มต้น" not in a.get("/admin").text
    page = a.get("/admin/setup").text
    assert "ตอนนี้ใช้ชุด <b>โรงงาน</b>" in page and "ใช้อยู่" in page and "เสร็จแล้ว: <a href=\"/admin/departments\">" in page
    # รอบใหม่ใช้โหมดรายการตรวจโดยไม่ต้องเลือก และแต่ละประเภทพื้นที่ได้ข้อที่ตรงกับพื้นที่
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, old_id).status = "closed"
        s.commit()
    a.post("/admin/rounds/save", data={"name": "รอบชุดโรงงาน", "min_photos": 1})
    with dbm.SessionLocal() as s:
        r = s.query(dbm.Round).filter_by(name="รอบชุดโรงงาน").one()
        assert r.mode == "checklist" and len(r.checklist) == 25 and r.rule_rev == rev + 1
        rid = r.id
    line = {c["code"] for c in shoot(S["mem"], rid, dept, "สายการผลิต").analysis["checks"]}
    office = {c["code"] for c in shoot(S["mem"], rid, dept, "สำนักงานในโรงงาน").analysis["checks"]}
    walk = {c["code"] for c in shoot(S["mem"], rid, dept, "ทางเดินและพื้นที่ส่วนกลาง").analysis["checks"]}
    assert {"C02", "C05", "C09"} <= line and not (line & {"O01", "O02", "O04", "O06"}) and len(line) == 17 and "C17" in line
    assert {"O01", "O02", "O04", "O06", "C04", "C13"} <= office and not (office & {"C02", "C03", "C05", "C09", "C17"})
    assert walk == {"C01", "C04", "C14", "C08", "C10", "C15", "C13", "C16"}
    with dbm.SessionLocal() as s:
        checks = s.get(dbm.Round, rid).checklist[:3]
    prompt = ai.build_check_prompt(checks, dict(area_type="สายการผลิต", area_name="x", note=""), s_["ai_extra"])
    assert "ให้นับที่ข้อที่ตรงที่สุดเพียงข้อเดียว" in prompt and "เส้นสีเหลืองบนพื้น" in prompt
    # ชุดตั้งค่าเปิดให้นับเฉพาะภาพที่ยืนยันแล้ว: ยังไม่มีแผนกถูกจัดอันดับจนกว่าจะยืนยัน
    assert "ยังไม่มีแผนกที่ถูกจัดอันดับ" in a.get(f"/ranking?round={rid}").text or "ยังไม่ถูกจัดอันดับ" in a.get(f"/ranking?round={rid}").text
    # ลบข้อแล้วกดคืนค่า: ได้ชุดโรงงานกลับมา ไม่ใช่ชุดตั้งต้นเดิม
    with dbm.SessionLocal() as s:
        kid = s.query(dbm.Checkpoint).filter_by(code="C14").one().id
    a.post(f"/admin/checkpoints/{kid}/delete")
    assert len(codes()) == 23
    assert a.post("/admin/checkpoints/reset").status_code == 303 and len(codes()) == 24
    assert "ชุดโรงงาน 24 ข้อ" in a.get("/admin/checkpoints").text
    S["rid"] = rid


def test_03_office_preset_and_partial_apply():
    a = S["admin"]
    assert a.post("/admin/setup/apply", data={"preset": "office", "rules": "1"}).status_code == 303       # เฉพาะหมวดและรายการตรวจ
    s_ = settings_store.load()
    got = codes()
    assert s_["preset"] == "office" and len(got) == 19 and got[0] == "O01" and "C02" not in got
    assert s_["area_types"][0] == "สายการผลิต" and s_["cam_schedule"]["random"] == 2                       # การตั้งค่ายังเป็นของชุดโรงงาน
    assert a.post("/admin/setup/apply", data={"preset": "office", "settings": "1"}).status_code == 303
    s_ = settings_store.load()
    assert s_["area_types"][:5] == ["โต๊ะทำงาน", "ห้องประชุม", "ห้องเก็บเอกสาร", "พื้นที่ส่วนกลาง", "ห้องเตรียมอาหาร"]
    assert "สายการผลิต" in s_["area_types"] and s_["cam_schedule"] == {"times": [], "random": 1, "between": "09:00-16:00", "days": [0, 1, 2, 3, 4]}
    assert "ให้นับที่ข้อ O02: กระเป๋าเป้" in s_["ai_extra"] and len(codes()) == 19
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter(dbm.Round.status == "open"):
            r.status = "closed"
        s.commit()
    a.post("/admin/rounds/save", data={"name": "รอบชุดสำนักงาน", "min_photos": 1})
    with dbm.SessionLocal() as s:
        rid = s.query(dbm.Round).filter_by(name="รอบชุดสำนักงาน").one().id
    meeting = {c["code"] for c in shoot(S["mem"], rid, S["dept"], "ห้องประชุม").analysis["checks"]}
    desk = {c["code"] for c in shoot(S["mem"], rid, S["dept"], "โต๊ะทำงาน").analysis["checks"]}
    assert "O08" in meeting and "O01" not in meeting and "O11" not in meeting
    assert {"O01", "O02", "O04", "O15", "O16"} <= desk and "O08" not in desk
    page = a.get("/admin/setup").text
    assert "ตอนนี้ใช้ชุด <b>สำนักงาน</b>" in page
    for url in ("/", "/admin", "/admin/setup", "/admin/checkpoints", "/admin/criteria", "/admin/settings", "/admin/cameras", "/capture",
                f"/ranking?round={rid}", f"/dashboard?round={rid}", "/tv"):
        got = a.get(url)
        assert got.status_code == 200 and "Traceback" not in got.text, url
    # กลับไปใช้ชุดโรงงานได้ทุกเมื่อ
    a.post("/admin/setup/apply", data={"preset": "factory", "rules": "1", "settings": "1"})
    assert len(codes()) == 24 and settings_store.load()["area_types"][0] == "สายการผลิต"
    save(verify_required=False, rounds_repeat="off", preset="")


def test_04_phone_camera_files():
    """ไฟล์จากกล้องมือถือ: ภาพถือแนวตั้งที่มีข้อมูลการหมุน ภาพขนาดใหญ่ และภาพ HEIC"""
    import pytest
    from PIL import ImageDraw
    from app import imaging
    im = Image.new("RGB", (4000, 3000), (120, 140, 130))
    ImageDraw.Draw(im).rectangle([0, 0, 400, 3000], fill=(230, 20, 20))        # ขอบซ้ายของข้อมูลภาพ = ด้านบนเมื่อถือแนวตั้ง
    exif = Image.Exif()
    exif[0x0112] = 6                                                             # มือถือถือแนวตั้ง
    exif[0x8825] = {1: "N", 2: (13.0, 45.0, 0.0)}                                # พิกัด GPS
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, exif=exif.tobytes())
    out = imaging.process(buf.getvalue(), 1280, 78)
    stored = Image.open(io.BytesIO(out["image"]))
    assert (out["width"], out["height"]) == (960, 1280) and stored.size == (960, 1280)      # ตั้งตรง และย่อด้านยาวเหลือ 1280
    r, g, b = stored.getpixel((480, 30))
    assert r > 180 and g < 90                                                    # แถบแดงอยู่ด้านบน: หมุนถูกทิศ
    assert len(stored.getexif()) == 0 and len(out["image"]) < 400_000            # ไม่มี EXIF และ GPS ติดไปกับภาพที่เก็บ
    big = io.BytesIO()
    Image.new("RGB", (8160, 6120), (90, 110, 100)).save(big, "JPEG", quality=85)     # 50 ล้านพิกเซล
    assert imaging.process(big.getvalue(), 1280, 78)["width"] == 1280
    # ส่งผ่านหน้าเว็บจริง: ภาพจากมือถือเข้ารอบ ได้คะแนน และแสดงทิศทางถูก
    a = S["admin"]
    with dbm.SessionLocal() as s:
        s.get(dbm.Round, S["rid"]).status = "open"
        s.commit()
    r = S["mem"].post("/api/photos", data=dict(round_id=S["rid"], department_id=S["dept"], area_name="ถ่ายจากมือถือ", area_type="สายการผลิต",
                                               source="mobile"), files={"file": ("IMG_0001.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200, r.text
    with dbm.SessionLocal() as s:
        p = s.get(dbm.Photo, r.json()["id"])
        assert (p.source, p.width, p.height) == ("mobile", 960, 1280)
    assert a.get(f"/photos/{r.json()['id']}/image").headers["content-type"] == "image/jpeg"
    # ไฟล์ HEIC ที่เบราว์เซอร์แปลงไม่ได้
    fake = bytes([0, 0, 0, 24]) + b"ftypheic" + bytes(4000)
    assert imaging.looks_heif(fake) and not imaging.looks_heif(buf.getvalue())
    was = imaging.HEIF
    imaging.HEIF = False
    try:
        with pytest.raises(imaging.ImageError, match="HEIC"):
            imaging.process(fake)
        r = S["mem"].post("/api/photos", data=dict(round_id=S["rid"], department_id=S["dept"], area_name="heic", area_type="สายการผลิต"),
                          files={"file": ("IMG_0002.heic", fake, "image/heic")})
        assert r.status_code == 400 and "ใช้ปุ่ม ถ่ายภาพ" in r.json()["detail"]
    finally:
        imaging.HEIF = was
    if imaging.HEIF:                                                             # ติดตั้ง pillow-heif ไว้: เซิร์ฟเวอร์เปิดไฟล์ HEIC ได้เอง
        real = io.BytesIO()
        Image.new("RGB", (1600, 1200), (60, 120, 90)).save(real, "HEIF", quality=80)
        assert imaging.process(real.getvalue())["width"] == 1280
    # หน้าถ่ายภาพมีปุ่มกล้องสำหรับมือถือ
    page = S["mem"].get("/capture").text
    assert 'id="cam" accept="image/*" capture="environment"' in page and "ถ่ายภาพ" in page


def test_05_preset_revision_two_is_countable_and_explicit():
    """ฉบับที่ 2 ของชุดตั้งค่า: ทุกข้อบอกเกณฑ์เป็นจำนวนที่นับได้ มีข้อเรื่องของบนพื้นและของใช้ส่วนตัว และมีคู่มือการตัดสินให้ AI"""
    import re
    a = S["admin"]
    for key in ("factory", "office"):
        for code, crit, text, minor, major, points, types, en in presets.PRESETS[key]["checks"]:
            assert text and minor and major and en and points in (5, 10), code
            assert minor != major and len(minor) >= 8 and len(major) >= 8, code
        guide = presets.PRESETS[key]["settings"]["ai_extra"]
        assert "ให้นับที่ข้อที่ตรงที่สุดเพียงข้อเดียว" in guide and "ตรวจให้ทั่วทั้งภาพ" in guide and len(guide) < 3000
        counted = [c for c in presets.PRESETS[key]["checks"] if re.search(r"\d", c[3] + c[4])]
        assert len(counted) >= len(presets.PRESETS[key]["checks"]) - 6, key          # เกือบทุกข้อมีจำนวนกำกับ
    office = {c[0]: c for c in presets.OFFICE_CHECKS}
    assert "กระเป๋า" in office["O02"][2] and "บนพื้น" in office["O02"][2] and "1 ชิ้น" in office["O02"][3] and "ตั้งแต่ 2 ชิ้น" in office["O02"][4]
    assert "พาดพนักเก้าอี้" in office["O15"][2] and "พื้นที่ว่าง" in office["O16"][2]
    factory = {c[0]: c for c in presets.FACTORY_CHECKS}
    assert "ของใช้ส่วนตัว" in factory["C17"][2] and factory["O02"][6] == ["สำนักงานในโรงงาน"] and factory["C17"][6] == presets.PROD
    assert "แม้เจ้าของจะนั่งอยู่ที่โต๊ะนั้น" in presets.PRESETS["office"]["settings"]["ai_extra"]
    assert (presets.PRESETS["office"]["settings"]["band_good"], presets.PRESETS["factory"]["settings"]["band_mid"]) == (90, 80)
    assert "ให้นับที่ข้อ C17" in presets.PRESETS["factory"]["settings"]["ai_extra"]
    # กติกากลางของ AI: นับก่อนตัดสิน ใช้เกณฑ์ตามตัวอักษร และมองทั้งภาพ
    for rule in ("นับจำนวนสิ่งของหรือจุด", "ถ้าถึงเกณฑ์ของ major ต้องตอบ major", "ใต้โต๊ะ", "จำนวนที่นับได้", "ข้อที่ตรงที่สุดเพียงข้อเดียว"):
        assert rule in ai.CHECK_SYSTEM, rule
    # ระบบที่ใช้ชุดตั้งค่าฉบับเดิมอยู่ได้รับแจ้งว่ามีฉบับใหม่ และหายไปเมื่อกดใช้
    save(preset="office", preset_rev=1)
    assert "ชุดตั้งค่าเริ่มต้นมีฉบับปรับปรุง" in a.get("/admin").text and "ชุดตั้งค่ามีฉบับปรับปรุง" in a.get("/admin/setup").text
    a.post("/admin/setup/apply", data={"preset": "office", "rules": "1", "settings": "1"})
    assert settings_store.load()["preset_rev"] == presets.PRESET_REV
    assert "ฉบับปรับปรุง" not in a.get("/admin").text and "ชุดตั้งค่ามีฉบับปรับปรุง" not in a.get("/admin/setup").text
    with dbm.SessionLocal() as s:
        k = s.query(dbm.Checkpoint).filter_by(code="O02").one()
        assert "กระเป๋า" in k.text and k.minor_hint.startswith("มีของวางบนพื้น 1 ชิ้น") and k.text_en.startswith("Nothing is placed on the floor")
        checks = [dict(code=k.code, text=k.text, minor_hint=k.minor_hint, major_hint=k.major_hint, allow_na=True)]
    prompt = ai.build_check_prompt(checks, dict(area_type="โต๊ะทำงาน", area_name="โต๊ะ", note=""), settings_store.load()["ai_extra"])
    assert "minor เมื่อ: มีของวางบนพื้น 1 ชิ้น" in prompt and "major เมื่อ: มีของวางบนพื้นตั้งแต่ 2 ชิ้น" in prompt and "กระเป๋าเป้ กระเป๋าถือ" in prompt
    save(verify_required=False, rounds_repeat="off", preset="", gallery_max_age_h=0)

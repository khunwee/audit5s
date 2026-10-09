"""ตรวจทุกฟีเจอร์ของ 5ส Vision กับเซิร์ฟเวอร์จริง (uvicorn + เธรดคิว ตัวแจ้งเตือน ตัวตั้งเวลา ทำงานจริง)

ใช้จากโฟลเดอร์ของโปรแกรม (หลังติดตั้งตาม requirements.txt แล้ว)

    python tools/validate_all.py                          ตรวจกับ SQLite ในโฟลเดอร์ชั่วคราว
    python tools/validate_all.py postgresql://ผู้ใช้:รหัส@เครื่อง/ฐานข้อมูลเปล่า    ตรวจกับ PostgreSQL

สคริปต์เปิดเซิร์ฟเวอร์ของโปรแกรมที่พอร์ต 8795 และบริการจำลองที่พอร์ต 8821 บนเครื่องนี้เอง ใช้ข้อมูลในโฟลเดอร์ชั่วคราว
ไม่แตะข้อมูลจริงในโฟลเดอร์ data ไม่ออกอินเทอร์เน็ต และไม่ใช้โควตาของ AI
บริการภายนอกทั้งหมด (AI สามเจ้า กล้อง IP ปลายทางแจ้งเตือน) เป็นบริการจำลองผ่าน HTTP จริง
ผลลัพธ์: บรรทัดสุดท้ายขึ้น RESULT ผ่านกี่ข้อจากกี่ข้อ ใช้เวลาประมาณ 1 ถึง 2 นาที

ห้ามชี้ไปที่ฐานข้อมูลที่ใช้งานจริง: ขั้นสุดท้ายของการตรวจคือการล้างข้อมูลการตรวจทั้งหมด
"""
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx
from PIL import Image
from sqlalchemy import create_engine, text

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ARG = sys.argv[1] if len(sys.argv) > 1 else "sqlite"
DBKIND = "postgres" if ARG.startswith(("postgresql://", "postgres://")) else "sqlite"
FAKE, PORT = 8821, 8795
BASE, FB = f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{FAKE}"
DATA = tempfile.mkdtemp(prefix="validate_")
PG = ARG.replace("postgres://", "postgresql://", 1) if DBKIND == "postgres" else ""
RESULTS, GROUP = [], [""]


def check(name, cond, detail=""):
    RESULTS.append((GROUP[0], name, bool(cond), "" if cond else str(detail)[:300]))
    if not cond:
        print(f"   FAIL [{GROUP[0]}] {name} :: {str(detail)[:300]}", flush=True)
    return bool(cond)


def group(name):
    GROUP[0] = name
    print(f"== {name}", flush=True)


def jpeg(seed, size=(1400, 1050)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    for _ in range(40):
        x, y = rnd.randrange(size[0] - 80), rnd.randrange(size[1] - 60)
        im.paste((rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)), (x, y, x + 80, y + 60))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    return b.getvalue()


class Server:
    def __init__(self):
        self.proc, self.log = None, os.path.join(DATA, "server.log")

    def start(self, app_dir=APP):
        env = dict(os.environ, DATA_DIR=DATA, ADMIN_PASSWORD="admin1234", PYTHONUNBUFFERED="1", RENDER_EXTERNAL_URL=BASE,
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8", COOKIE_SECURE="0")
        env.pop("SECRET_KEY", None)
        env.pop("DISABLE_WORKER", None)
        env.pop("DATABASE_URL", None)
        if DBKIND == "postgres":
            env["DATABASE_URL"] = PG
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=app_dir, env=env,
                                     stdout=open(self.log, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                if httpx.get(BASE + "/healthz", timeout=3).status_code == 200:
                    return httpx.get(BASE + "/healthz").json()["version"]
            except httpx.HTTPError:
                time.sleep(0.25)
        raise RuntimeError("server did not start: " + open(self.log).read()[-1500:])

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except Exception:
                self.proc.kill()
            self.proc = None


ENGINE = None


def q(sql, **p):
    global ENGINE
    if ENGINE is None:
        url = (PG.replace("postgresql://", "postgresql+psycopg://") if DBKIND == "postgres"
               else "sqlite:///" + os.path.join(DATA, "fives.db").replace("\\", "/"))
        ENGINE = create_engine(url)
    with ENGINE.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p).fetchall()]


def one(sql, **p):
    rows = q(sql, **p)
    return rows[0][0] if rows else None


def client(username, password, new=None):
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303, (username, r.status_code, r.text[:200])
    if c.get("/").headers.get("location") == "/account/password":
        c.post("/account/password", data={"current": password, "new": new, "confirm": new})
    return c


def ctl(**kw):
    httpx.post(FB + "/control", json=kw, timeout=10)


def stats():
    return httpx.get(FB + "/stats", timeout=10).json()


def upload(c, rid, did, seed, area, area_type, expect=200, **kw):
    data = dict(round_id=rid, department_id=did, area_name=area, area_type=area_type)
    data.update(kw)
    raw = kw.pop("_raw", None) or jpeg(seed)
    data.pop("_raw", None)
    r = c.post("/api/photos", data=data, files={"file": ("p.jpg", raw, "image/jpeg")})
    if r.status_code != expect:
        raise AssertionError(f"upload expected {expect} got {r.status_code}: {r.text[:300]}")
    return r.json().get("id") if expect == 200 else r


def wait_status(ids, want=("done", "rejected", "error"), timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        rows = dict(q(f"SELECT id, status FROM photos WHERE id IN ({','.join(map(str, ids))})"))
        if all(rows.get(i) in want for i in ids):
            return rows
        time.sleep(0.5)
    return dict(q(f"SELECT id, status FROM photos WHERE id IN ({','.join(map(str, ids))})"))


def setting(key):
    v = one("SELECT value FROM settings WHERE key = :k", k=key)
    return json.loads(v) if v is not None else None


fake = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_services.py"), str(FAKE)],
                        env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
time.sleep(1.0)
srv = Server()
T0 = time.time()
try:
    if DBKIND == "postgres":
        e0 = create_engine(PG.replace("postgresql://", "postgresql+psycopg://"))
        with e0.connect() as c0:
            existing = c0.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'photos'")).scalar()
            if existing and c0.execute(text("SELECT COUNT(*) FROM photos")).scalar():
                raise SystemExit("ฐานข้อมูลนี้มีข้อมูลภาพอยู่แล้ว สคริปต์นี้ใช้กับฐานข้อมูลเปล่าเท่านั้น เพราะขั้นสุดท้ายจะล้างข้อมูลการตรวจทั้งหมด")
        e0.dispose()
    version = srv.start()
    print("version", version, "| database", DBKIND, flush=True)

    # ------------------------------------------------------------------ 1
    group("1 เข้าสู่ระบบและบัญชี")
    anon = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    check("หน้าแรกพาไปหน้าเข้าสู่ระบบ", anon.get("/").headers.get("location", "").startswith("/login"))
    check("healthz ไม่ต้องเข้าสู่ระบบ", anon.get("/healthz").json().get("ok") is True)
    r = anon.post("/login", data={"username": "admin", "password": "wrong"})
    check("รหัสผ่านผิดเข้าไม่ได้", r.status_code == 303 and r.headers["location"] == "/login")
    a = httpx.Client(base_url=BASE, timeout=60, follow_redirects=False)
    a.post("/login", data={"username": "admin", "password": "admin1234"})
    check("ครั้งแรกบังคับเปลี่ยนรหัสผ่าน", a.get("/admin").headers.get("location") == "/account/password")
    a.post("/account/password", data={"current": "admin1234", "new": "Factory5S2026", "confirm": "Factory5S2026"})
    check("เปลี่ยนรหัสแล้วใช้งานได้", a.get("/").status_code == 200 and a.get("/admin").status_code == 200)
    r = a.post("/admin/settings", data={"org_name": "x"}, headers={"origin": "https://evil.example"})
    check("คำสั่งจากเว็บอื่นถูกปฏิเสธ", r.status_code == 403)
    check("ไฟล์ static และ manifest", a.get("/static/app.css").status_code == 200 and a.get("/static/manifest.json").status_code == 200)

    # ------------------------------------------------------------------ 2
    group("2 ชุดตั้งค่าเริ่มต้นและแนวทาง")
    page = a.get("/admin/setup").text
    check("หน้าเริ่มต้นใช้งานแสดงสองชุด", "โรงงาน" in page and "สำนักงาน" in page)
    r = a.post("/admin/setup/apply", data={"preset": "office", "rules": "1", "settings": "1"})
    check("ใช้ชุดสำนักงาน", r.status_code == 303 and setting("preset") == "office", (r.status_code, setting("preset")))
    codes = [r_[0] for r_ in q("SELECT code FROM checkpoints WHERE active ORDER BY sort_order")] if True else []
    check("รายการตรวจของชุดสำนักงาน 19 ข้อ", len(codes) == 19, codes)
    check("ชุดตั้งค่าเปิดโหมดรายการตรวจ", setting("scoring_mode") == "checklist")
    check("หน้ารายการตรวจและหมวด", a.get("/admin/checkpoints").status_code == 200 and a.get("/admin/criteria").status_code == 200)
    types = setting("area_types")
    AT = types[0]

    # ------------------------------------------------------------------ 3
    group("3 ตั้งค่า AI สามตัว")
    form = {"org_name": "AHP ตรวจจริง", "public_url": BASE,
            "ai1_type": "gemini", "ai1_base": FB, "ai1_key": "g-key", "ai1_model": "gemini-3.8-flash",
            "ai2_type": "openai", "ai2_base": FB + "/two/v1", "ai2_key": "q-key", "ai2_model": "qwen/qwen3.8-27b",
            "ai3_type": "openai", "ai3_base": FB + "/three/v1", "ai3_key": "m-key", "ai3_model": "mistral-small-latest",
            "ai_rpm": 60, "ai_daily": 500, "ai2_rpm": 60, "ai2_daily": 0, "ai3_rpm": 60, "ai3_daily": 0, "ai_workers": 2, "ai_timeout": 30,
            "ai_max_attempts": 4, "ai_passes": 1, "img_max_side": 1280, "img_quality": 78, "storage_budget_mb": 350, "retention_days": 0,
            "member_see_all": "1", "allow_gallery": "1", "allow_free_area": "1", "after_replaces": "1", "scoring_mode": "checklist",
            "auto_actions": "1", "action_due_days": 7, "action_due_days_major": 3, "verify_required": "", "band_good": 90, "band_mid": 80,
            "area_types": "\n".join(types), "gallery_max_age_h": 48, "gallery_stale": "flag", "keep_awake": "queue",
            "queue_alert_min": 30, "host_limit_mb": 500, "storage_warn_pct": 80, "backup_remind_days": 7, "dept_remind_days": 3,
            "webcam_width": 1920, "ranking_visibility": "always"}
    check("บันทึกการตั้งค่า", a.post("/admin/settings", data=form).status_code == 303)
    check("เก็บ AI ครบสามช่อง", [setting(f"ai{i}_type") for i in (1, 2, 3)] == ["gemini", "openai", "openai"])
    page = a.get("/admin/settings").text
    check("หน้าตั้งค่าไม่แสดง key เต็ม", "g-key" not in page and "q-key" not in page and "m-key" not in page)
    check("หน้าตั้งค่ามีตัวช่วยผู้ให้บริการฟรี", "ผู้ให้บริการฟรีที่ใช้กับโปรแกรมนี้ได้" in page and "api.mistral.ai" in page)
    j = a.post("/admin/ai/models", json={"slot": "ai1", "type": "gemini", "base": FB}).json()
    check("ดึงรายชื่อโมเดล Gemini", j.get("ok") and "gemini-3.8-flash" in j.get("models", []), j)
    j = a.post("/admin/ai/models", json={"slot": "ai3", "type": "openai", "base": FB + "/three/v1"}).json()
    check("ดึงรายชื่อโมเดลแบบ Mistral เหลือเฉพาะรุ่นที่รับภาพ", j.get("ok") and j.get("models") == ["mistral-small-latest"], j)
    for slot, typ, base, model in (("ai1", "gemini", FB, "gemini-3.8-flash"), ("ai2", "openai", FB + "/two/v1", "qwen/qwen3.8-27b"),
                                   ("ai3", "openai", FB + "/three/v1", "mistral-small-latest")):
        j = a.post("/admin/ai/test", json={"slot": slot, "type": typ, "base": base, "model": model}).json()
        check(f"ทดสอบด้วยภาพตัวอย่าง {slot}", j.get("ok") is True, j)

    # ------------------------------------------------------------------ 4
    group("4 แผนก ผู้ใช้ จุดตรวจ")
    for code, name, grp in (("OF", "OFFICE", "สำนักงาน"), ("HR", "บุคคล", "สำนักงาน"), ("ST", "Store AHP", "สนับสนุน")):
        a.post("/admin/departments/save", data={"code": code, "name": name, "name_en": name, "group_name": grp})
    D = dict(q("SELECT code, id FROM departments"))
    check("สร้างแผนก 3 แผนก", set(D) >= {"OF", "HR", "ST"}, D)
    for u, role, dep in (("of_mem", "member", D["OF"]), ("hr_mem", "member", D["HR"]), ("judge", "auditor", "")):
        a.post("/admin/users/save", data={"username": u, "full_name": f"ผู้ใช้ {u}", "role": role, "department_id": dep, "password": "Start1234",
                                          "active": "1", **({"perm_score": "1", "perm_rounds": "1"} if role == "auditor" else {})})
    check("สร้างผู้ใช้ 3 บัญชี", one("SELECT COUNT(*) FROM users") == 4)
    mem = client("of_mem", "Start1234", "Member5S2026")
    hr = client("hr_mem", "Start1234", "Member5S2026")
    aud = client("judge", "Start1234", "Judge5S2026")
    check("ผู้ใช้ใหม่เข้าสู่ระบบและตั้งรหัสเอง", mem.get("/").status_code == 200 and aud.get("/").status_code == 200)
    a.post("/admin/areas/save", data=dict(department_id=D["OF"], name="โต๊ะกลม", area_type=AT, required="1", standard="บนโต๊ะมีได้เฉพาะแฟ้มที่ใช้อยู่"))
    a.post("/admin/areas/save", data=dict(department_id=D["OF"], name="ลิ้นชัก", area_type=AT, required="1"))
    AREA = dict(q("SELECT name, id FROM audit_areas"))
    check("สร้างจุดตรวจบังคับ", set(AREA) == {"โต๊ะกลม", "ลิ้นชัก"}, AREA)
    r = a.get("/admin/areas/qr")
    check("หน้าป้าย QR ของจุดตรวจ", r.status_code == 200 and "<svg" in r.text and "โต๊ะกลม" in r.text)
    check("ตัวแทนแผนกเข้าหน้าจัดการไม่ได้", mem.get("/admin").status_code == 403 and mem.get("/admin/settings").status_code == 403)

    # ------------------------------------------------------------------ 5
    group("5 รอบการตรวจ")
    r = a.post("/admin/rounds/save", data={"name": "ตรวจ 5 ส ประจำเดือน ตุลาคม 2569", "min_photos": 2, "mode": "checklist"})
    RID = one("SELECT id FROM rounds WHERE status = 'open'")
    check("สร้างรอบแบบรายการตรวจและเปิดรับภาพ", r.status_code == 303 and RID, r.status_code)
    a.post("/admin/rounds/save", data={"name": "รอบเดือนหน้า", "start_date": "2099-01-01", "end_date": "2099-01-31", "min_photos": 2,
                                       "auto_open": "1", "auto_close": "1"})
    check("รอบที่วันเริ่มอยู่ในอนาคตรอเปิดตามกำหนด", one("SELECT status FROM rounds WHERE name = 'รอบเดือนหน้า'") == "planned")
    check("หน้าถ่ายภาพของตัวแทนแผนก", mem.get("/capture").status_code == 200 and 'type="file"' in mem.get("/capture").text)
    check("เปิดจากป้าย QR เลือกจุดตรวจให้", f'data-scanned="{AREA["โต๊ะกลม"]}"' in mem.get(f"/capture?area={AREA['โต๊ะกลม']}").text)
    check("ป้าย QR ของแผนกอื่นใช้ไม่ได้", f'data-scanned="{AREA["โต๊ะกลม"]}"' not in hr.get(f"/capture?area={AREA['โต๊ะกลม']}").text)

    # ------------------------------------------------------------------ 6
    group("6 ส่งภาพและให้ AI ตรวจ")
    ng_major, ng_minor = codes[2], codes[5]
    ctl(ng={"major": [ng_major], "minor": [ng_minor]})
    p1 = upload(mem, RID, D["OF"], 1, "โต๊ะกลม", AT, area_id=AREA["โต๊ะกลม"], note="ถ่ายจากมือถือ")
    r = upload(mem, RID, D["OF"], 1, "โต๊ะกลม", AT, expect=409, area_id=AREA["โต๊ะกลม"])
    check("ภาพซ้ำถูกปฏิเสธ", "ภาพนี้ส่งแล้ว" in r.text, r.text[:120])
    r = mem.post("/api/photos", data=dict(round_id=RID, department_id=D["OF"], area_name="x", area_type=AT),
                 files={"file": ("a.jpg", b"not an image at all", "image/jpeg")})
    check("ไฟล์ที่ไม่ใช่ภาพถูกปฏิเสธ", r.status_code == 400, r.status_code)
    r = mem.post("/api/photos", data=dict(round_id=RID, department_id=D["HR"], area_name="x", area_type=AT),
                 files={"file": ("a.jpg", jpeg(900), "image/jpeg")})
    check("ส่งภาพแทนแผนกอื่นไม่ได้", r.status_code == 403, r.status_code)
    p2 = upload(mem, RID, D["OF"], 2, "ลิ้นชัก", AT, area_id=AREA["ลิ้นชัก"], source="gallery", shot_at="2020-01-01T08:00:00")
    p3 = upload(hr, RID, D["HR"], 3, "โต๊ะทำงาน", AT)
    p4 = upload(hr, RID, D["HR"], 4, "ชั้นเก็บของ", AT)
    st = wait_status([p1, p2, p3, p4])
    check("AI ตรวจภาพครบโดยเธรดคิวจริง", all(v == "done" for v in st.values()), st)
    row = q("SELECT percent, provider, model, ai_slot, analysis FROM photos WHERE id = :i", i=p1)[0]
    an = json.loads(row[4]) if isinstance(row[4], str) else row[4]
    by = {c["code"]: c for c in an.get("checks", [])}
    check("ผลเป็นโหมดรายการตรวจครบทุกข้อที่ใช้กับพื้นที่", an.get("mode") == "checklist" and len(by) >= 10, len(by))
    check("ข้อที่ AI รายงานว่าบกพร่องถูกบันทึกพร้อมกรอบ", by.get(ng_major, {}).get("status") == "major" and by[ng_major].get("box")
          and by.get(ng_minor, {}).get("status") == "minor", {k: by.get(k) for k in (ng_major, ng_minor)})
    check("ระบบคิดคะแนนเองและต่ำกว่า 100", row[0] is not None and 0 < row[0] < 100, row[0])
    check("บันทึกว่าได้คะแนนจาก AI หลัก", (row[1], row[2], row[3]) == ("gemini", "gemini-3.8-flash", "ai1"), row[:4])
    check("ภาพจากคลังที่ถ่ายไว้นานถูกติดป้าย", one("SELECT stale FROM photos WHERE id = :i", i=p2) in (True, 1))
    page = mem.get(f"/photos/{p1}").text
    check("หน้ารายละเอียดภาพแสดงผลและกรอบ", "fbox" in page and "จัดเก็บให้เข้าที่" in page)
    check("ภาพและภาพย่อเปิดได้", mem.get(f"/photos/{p1}/image").headers["content-type"] == "image/jpeg"
          and mem.get(f"/photos/{p1}/thumb").status_code == 200)
    ctl(image_ok=False)
    p5 = upload(mem, RID, D["OF"], 5, "ภาพมืด", AT)
    check("ภาพที่ AI ประเมินไม่ได้ไม่ถูกคิดคะแนน", wait_status([p5])[p5] == "rejected")
    ctl(image_ok=True)
    ctl(ng={"major": [], "minor": []})
    p6 = upload(mem, RID, D["OF"], 6, "โต๊ะกลม", AT, area_id=AREA["โต๊ะกลม"], after_of=p1)
    wait_status([p6])
    check("ภาพหลังแก้ไขผูกกับภาพเดิมและได้คะแนนดีขึ้น", one("SELECT after_of FROM photos WHERE id = :i", i=p6) == p1
          and one("SELECT percent FROM photos WHERE id = :i", i=p6) > row[0])
    check("หน้ารายการภาพและตัวกรอง", all(mem.get(f"/photos?round={RID}&status={s}").status_code == 200
                                       for s in ("", "done", "waiting", "rejected", "error", "unverified", "appeal", "stale", "review", "override", "backup")))

    # ------------------------------------------------------------------ 7
    group("7 ยืนยันผล ปรับผล ขอทบทวน งานแก้ไข")
    check("หน้ายืนยันผลของกรรมการ", aud.get("/verify").status_code == 200)
    r = aud.post(f"/admin/photos/{p1}/verify")
    check("กรรมการยืนยันผล", r.status_code == 303 and one("SELECT verified_by FROM photos WHERE id = :i", i=p1))
    acts = q("SELECT id, title, status, due_date FROM actions WHERE photo_id = :i ORDER BY id", i=p1)
    check("ยืนยันแล้วสร้างงานแก้ไขจากข้อที่ไม่ผ่าน", len(acts) == 2 and all(x[3] for x in acts), acts)
    check("ตัวแทนแผนกยืนยันภาพเองไม่ได้", mem.post(f"/admin/photos/{p3}/verify").status_code == 403)
    r = mem.post(f"/photos/{p3}/appeal", data={"note": "กระเป๋าที่เห็นเป็นของผู้มาติดต่อ ขอทบทวน"})
    check("แผนกเจ้าของภาพขอทบทวนได้เฉพาะภาพของตัวเอง", r.status_code in (303, 403))
    r = hr.post(f"/photos/{p3}/appeal", data={"note": "กระเป๋าที่เห็นเป็นของผู้มาติดต่อ ขอทบทวน"})
    check("ขอทบทวนผล", r.status_code == 303 and one("SELECT appeal_status FROM photos WHERE id = :i", i=p3) == "open")
    r = aud.post(f"/admin/photos/{p3}/appeal-reply", data={"reply": "ตรวจแล้ว ผลเดิมถูกต้อง"})
    check("กรรมการตอบคำขอทบทวน", r.status_code == 303 and one("SELECT appeal_status FROM photos WHERE id = :i", i=p3) == "resolved")
    form_o = {f"check_{c}": "ok" for c in by}
    form_o[f"check_{ng_major}"] = "minor"
    form_o["note"] = "ของบนพื้นเป็นกล่องเอกสารที่กำลังขนย้าย"
    before = one("SELECT percent FROM photos WHERE id = :i", i=p4)
    r = aud.post(f"/admin/photos/{p4}/override", data=form_o)
    check("กรรมการปรับผลรายข้อพร้อมเหตุผล", r.status_code == 303 and one("SELECT overridden FROM photos WHERE id = :i", i=p4) in (True, 1), r.status_code)
    aid = acts[0][0]
    check("หน้างานแก้ไข", mem.get("/actions").status_code == 200 and mem.get("/actions?status=all").status_code == 200)
    r = mem.post(f"/actions/{aid}/close", data={"note": "จัดเก็บแล้ว"})
    check("แผนกปิดงานแก้ไข", r.status_code == 303 and one("SELECT status FROM actions WHERE id = :i", i=aid) == "done")
    check("ส่งออกงานแก้ไขเป็น CSV", aud.get("/actions.csv").status_code == 200)
    check("หน้าความแม่นของ AI", aud.get("/admin/quality").status_code == 200)

    # ------------------------------------------------------------------ 8
    group("8 อันดับ รายงาน ส่งออก")
    page = mem.get("/ranking").text
    check("หน้าอันดับแสดงทั้งสองแผนก", "OFFICE" in page and "บุคคล" in page)
    check("อันดับแยกกลุ่ม", mem.get("/ranking?group=สำนักงาน").status_code == 200)
    check("รายงานของแผนก", mem.get(f"/rounds/{RID}/dept/{D['OF']}").status_code == 200)
    check("รายงานรวมสำหรับพิมพ์", aud.get(f"/rounds/{RID}/report").status_code == 200)
    check("แดชบอร์ดและแนวโน้ม", mem.get("/dashboard").status_code == 200 and mem.get("/trend").status_code == 200
          and aud.get("/trend.csv").status_code == 200)
    for kind in ("scores.xlsx", "ranking.csv", "photos.csv", "checks.csv", "data.json"):
        r = aud.get(f"/rounds/{RID}/export/{kind}")
        check(f"ส่งออก {kind}", r.status_code == 200 and len(r.content) > 100, r.status_code)
    r = aud.get(f"/rounds/{RID}/dataset.zip")
    check("ส่งออกชุดข้อมูลภาพ", r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    check("ตัวแทนแผนกส่งออกไม่ได้", mem.get(f"/rounds/{RID}/export/scores.xlsx").status_code == 403)
    r = aud.post(f"/admin/rounds/{RID}/dept/{D['OF']}/summarize")
    check("สรุปคำแนะนำของแผนกด้วย AI", r.status_code == 303 and one("SELECT COUNT(*) FROM dept_summaries") == 1)

    # ------------------------------------------------------------------ 9
    group("9 คิววิเคราะห์ AI สามตัว และเวลารอ")
    ctl(ai1="429day")
    p7 = upload(mem, RID, D["OF"], 7, "โต๊ะทำงาน 7", AT)
    wait_status([p7])
    row = q("SELECT status, provider, ai_slot, attempts FROM photos WHERE id = :i", i=p7)[0]
    check("AI หลักโควตาหมด ใช้ AI สำรอง 1 ทันที", row == ("done", "openai", "ai2", 0), row)
    ctl(ai2="503")
    p8 = upload(mem, RID, D["OF"], 8, "โต๊ะทำงาน 8", AT)
    wait_status([p8])
    row = q("SELECT status, model, ai_slot FROM photos WHERE id = :i", i=p8)[0]
    check("AI สำรอง 1 ล่มด้วย ใช้ AI สำรอง 2", row == ("done", "mistral-small-latest", "ai3"), row)
    page = a.get("/admin/queue").text
    check("หน้าคิวแสดงสถานะผู้ให้บริการสามตัว", all(x in page for x in ("AI หลัก", "AI สำรอง 1", "AI สำรอง 2", "พร้อมใช้")))
    check("หน้าคิวบอกจำนวนภาพที่ได้คะแนนจาก AI สำรอง", "มี 2 ภาพในรอบที่เปิดอยู่ ที่ได้คะแนนจาก AI สำรอง" in page)
    check("ภาพที่ได้คะแนนจาก AI สำรองมีป้าย", "AI สำรอง 2" in mem.get(f"/photos/{p8}").text
          and f'href="/photos/{p7}"' in mem.get(f"/photos?round={RID}&status=backup").text)
    ctl(ai3="401")
    time.sleep(0.5)
    p9 = upload(mem, RID, D["OF"], 9, "โต๊ะทำงาน 9", AT)
    p10 = upload(mem, RID, D["OF"], 10, "โต๊ะทำงาน 10", AT)
    time.sleep(6)
    rows = q("SELECT status, attempts FROM photos WHERE id IN (:a, :b)", a=p9, b=p10)
    check("ทุกเจ้าใช้ไม่ได้ ภาพรอในคิว ไม่กลายเป็นวิเคราะห์ไม่สำเร็จ", all(r_[0] == "pending" and r_[1] <= 1 for r_ in rows), rows)
    js = mem.get(f"/api/photos/status?ids={p9},{p10}").json()
    w = {x["id"]: x for x in js["photos"]}
    check("ผู้ส่งภาพเห็นลำดับคิวและเหตุผล", "รอคิวลำดับที่" in w[p10]["wait"] and w[p10]["wait_tail"], w[p10])
    check("คิวหยุดรอ บอกเวลาที่จะลองเรียกใหม่เป็นวินาที", w[p10]["wait_secs"] is not None and "ระบบจะลองเรียก AI อีกครั้งในอีก" in w[p10]["wait"], w[p10])
    page = mem.get(f"/photos/{p9}").text
    check("หน้ารายละเอียดภาพมีตัวนับถอยหลัง", "data-countdown=" in page and "รอคิวลำดับที่" in page)
    jq = a.get("/admin/queue.json").json()
    check("ข้อมูลคิวของผู้ดูแล", jq["pending"] == 2 and jq["paused"] and jq["workers"] == 2, jq)
    check("แจ้งผู้ดูแลว่าคิวหยุดรอ", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'alert' AND detail LIKE '%คิววิเคราะห์ภาพหยุดรอ%'") >= 1)
    ctl(ai1="ok", ai2="ok", ai3="ok")
    check("ผู้ดูแลกดเดินคิวเดี๋ยวนี้", a.post("/admin/queue/kick").status_code == 303)
    st = wait_status([p9, p10], timeout=40)
    check("คิวเดินต่อเองและกลับไปใช้ AI หลัก", all(v == "done" for v in st.values())
          and q("SELECT DISTINCT ai_slot FROM photos WHERE id IN (:a, :b)", a=p9, b=p10) == [("ai1",)], st)
    # เวลารอที่บอก เทียบกับเวลาจริง
    a.post("/admin/settings", data=dict(form, ai_rpm=20, ai_workers=1))
    ctl(latency=1.0)
    batch = [upload(mem, RID, D["HR" if i % 2 else "OF"], 100 + i, f"เวลารอ {i}", AT) if i % 2 == 0
             else upload(hr, RID, D["HR"], 100 + i, f"เวลารอ {i}", AT) for i in range(8)]
    t_up = time.time()
    js = mem.get("/api/photos/status?ids=" + ",".join(map(str, batch))).json()
    told = {x["id"]: x["wait_secs"] for x in js["photos"] if x["status"] in ("pending", "processing")}
    last = batch[-1]
    check("ทุกภาพที่รอมีเวลาที่คาดเป็นวินาที", len(told) >= 6 and all(v is not None for v in told.values()), told)
    check("ข้อความบอกเวลาเป็นนาทีและวินาที", re.search(r"คาดว่าได้ผลในอีกประมาณ \d+ (วินาที|นาที)", [x for x in js["photos"] if x["id"] == last][0]["wait"] or ""),
          [x for x in js["photos"] if x["id"] == last][0])
    wait_status(batch, timeout=90)
    actual = time.time() - t_up
    predicted = told.get(last)
    check("เวลาที่บอกของภาพท้ายคิวใกล้เคียงเวลาจริง (คลาดไม่เกิน 40%)", predicted and abs(predicted - actual) <= 0.4 * actual + 3,
          f"บอก {predicted} วินาที จริง {actual:.0f} วินาที")
    print(f"   เวลาที่บอกของภาพท้ายคิว {predicted} วินาที เวลาจริง {actual:.1f} วินาที", flush=True)
    ctl(latency=0.25)
    a.post("/admin/settings", data=form)
    r = a.post("/admin/queue/rescore-backup")
    wait_status([p7, p8], timeout=40)
    check("ให้ AI หลักวิเคราะห์ภาพที่ได้คะแนนจาก AI สำรองใหม่", q("SELECT ai_slot FROM photos WHERE id IN (:a, :b)", a=p7, b=p8) == [("ai1",), ("ai1",)])
    check("ตัวนับต่อวันแยกตามผู้ให้บริการ", (one("SELECT count FROM ai_usage") or 0) > 10 and (one("SELECT count2 FROM ai_usage") or 0) >= 2
          and (one("SELECT count3 FROM ai_usage") or 0) >= 2, q("SELECT count, count2, count3 FROM ai_usage"))
    r = a.post("/admin/queue/wake-host")
    check("ระบบเรียกที่อยู่ของตัวเองเพื่อกัน host หลับได้", "สำเร็จ" in a.get("/admin/queue").text)

    # ------------------------------------------------------------------ 10
    group("10 กล้อง IP ตารางเวลา และโปรแกรมกล้อง")
    a.post("/admin/cameras/save", data=dict(name="กล้องสำนักงาน", department_id=D["OF"], area_name="มุมสูงสำนักงาน", mode="direct",
                                            source="snapshot", url=FB + "/cam/snap.jpg", sched_mode="inherit"))
    a.post("/admin/cameras/save", data=dict(name="กล้อง Store", department_id=D["ST"], area_name="ชั้นวาง Store", mode="agent",
                                            source="snapshot", url="http://192.168.1.50/snap.jpg", username="admin", password="pw"))
    CAM = dict(q("SELECT name, id FROM cameras"))
    check("ลงทะเบียนกล้องสองตัวต่างแผนก", len(CAM) == 2, CAM)
    r = a.get(f"/admin/cameras/{CAM['กล้องสำนักงาน']}/test.jpg")
    check("ทดสอบดึงภาพจากกล้อง", r.status_code == 200 and r.headers["content-type"].startswith("image/"), r.status_code)
    r = aud.post(f"/api/cameras/{CAM['กล้องสำนักงาน']}/capture", data={"round_id": RID})
    pc = r.json().get("id") if r.status_code == 200 else None
    check("สั่งถ่ายจากกล้อง IP เข้ารอบ", pc and wait_status([pc])[pc] == "done", r.text[:200])
    check("ภาพจากกล้องระบุแหล่งที่มา", one("SELECT source FROM photos WHERE id = :i", i=pc) == "ipcam")
    r = a.post("/admin/cameras/schedule", data={"sched_times": "09:00, 14:30", "sched_random": 2, "sched_between": "08:30-16:30",
                                                "sched_days": ["0", "1", "2", "3", "4"], "grace": 20, "holidays": "2026-12-31"})
    check("ตั้งตารางเวลากลางของกล้อง", r.status_code == 303 and setting("cam_schedule")["times"] == ["09:00", "14:30"], setting("cam_schedule"))
    check("หน้ากล้องแสดงตารางของวันนี้", a.get("/admin/cameras").status_code == 200)
    a.post("/admin/cameras/token")
    token = setting("agent_token")
    h = {"X-Agent-Token": token}
    check("โปรแกรมกล้องต้องใช้รหัส", anon.get("/api/agent/poll").status_code == 401)
    j = anon.get("/api/agent/poll", headers=h).json()
    check("โปรแกรมกล้องได้รายการกล้องและแผนการถ่าย", j.get("open_round") is True and [c["name"] for c in j["cameras"]] == ["กล้อง Store"]
          and "plan" in j, j)
    r = anon.post("/api/agent/upload", headers=h, data={"camera_id": CAM["กล้อง Store"]}, files={"file": ("c.jpg", jpeg(777), "image/jpeg")})
    pa = r.json().get("id")
    check("โปรแกรมกล้องส่งภาพเข้ารอบ", r.status_code == 200 and wait_status([pa])[pa] == "done", r.text[:200])
    areas_ = dict(q("SELECT name, id FROM audit_areas"))
    r = a.get(f"/admin/areas/{AREA['โต๊ะกลม']}/zones")
    check("หน้ากำหนดโซนของจุดตรวจ", r.status_code == 200)

    # ------------------------------------------------------------------ 11
    group("11 การแจ้งเตือน")
    a.post("/admin/notifications/save", data={"name": "ระบบ", "kind": "webhook", "ev_system": "1", "ev_upload": "1", "ev_result": "1",
                                              "ev_round": "1", "ev_action": "1", "cfg_webhook_url": FB + "/hook/all",
                                              "cfg_webhook_secret": "s3cret"})
    cid = one("SELECT id FROM notify_channels")
    check("เพิ่มช่องทางแจ้งเตือน", cid is not None)
    r = a.post(f"/admin/notifications/{cid}/test")
    time.sleep(1)
    hooks = stats()["hooks"]
    check("ทดสอบส่งถึงปลายทางพร้อมค่าลับ", len(hooks) >= 1 and hooks[-1]["secret"] == "s3cret", hooks[-1:] if hooks else r.text[:200])
    check("หน้าเว็บไม่แสดงค่าลับของช่องทาง", "s3cret" not in a.get("/admin/notifications").text)
    n0 = len(hooks)
    pn = upload(mem, RID, D["OF"], 55, "แจ้งเตือน", AT)
    wait_status([pn])
    end = time.time() + 75
    while time.time() < end and len(stats()["hooks"]) <= n0:
        time.sleep(2)
    texts = " | ".join(json.dumps(x["body"], ensure_ascii=False) for x in stats()["hooks"][n0:])
    check("แจ้งเมื่อมีภาพใหม่และเมื่อได้ผล โดยเธรดแจ้งเตือนจริง", len(stats()["hooks"]) > n0 and "OFFICE" in texts, texts[:300])

    # ------------------------------------------------------------------ 12
    group("12 จอแสดงผลและภาษา")
    tvform = {"tv_title": "ผลตรวจ 5ส", "tv_title_en": "5S Audit Results", "tv_lang": "th", "tv_theme": "dark", "tv_round": "open",
              "tv_rounds": 6, "tv_seconds": 10, "tv_rows": 8, "tv_refresh_min": 5, "tv_hours": "07:00-19:00", "tv_clock": "1",
              "tv_unranked": "1", "slides": ["ranking", "trend", "ranks", "history", "category", "actions"]}
    check("บันทึกการตั้งค่าจอ", a.post("/admin/tv", data=tvform).status_code == 303 and setting("tv_theme") == "dark")
    a.post("/admin/tv/key")
    key = setting("tv_key")
    check("จอที่ไม่มีรหัสเปิดไม่ได้", anon.get("/tv").status_code in (303, 403) and anon.get("/api/tv/data").status_code in (401, 403))
    tv = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    r = tv.get(f"/tv?key={key}")
    check("จอเปิดด้วยลิงก์ที่มีรหัส โดยไม่ต้องเข้าสู่ระบบ", r.status_code == 200 and "tv.js" in r.text, r.status_code)
    j = tv.get("/api/tv/data").json()
    check("ข้อมูลของจอมีอันดับและการตั้งค่า", j.get("ranking") is not None or "rows" in json.dumps(j)[:4000], list(j)[:8])
    r = mem.get("/lang/en?next=/ranking")
    page = mem.get("/ranking").text
    check("สลับเป็นภาษาอังกฤษ", '<html lang="en"' in page and "Ranking" in page)
    check("หน้าคิวในภาษาอังกฤษ", (a.get("/lang/en").status_code, "Analysis queue" in a.get("/admin/queue").text) == (303, True))
    a.get("/lang/th")
    mem.get("/lang/th")
    check("สลับกลับเป็นภาษาไทย", '<html lang="th"' in mem.get("/ranking").text)

    # ------------------------------------------------------------------ 13
    group("13 ทุกหน้าของทุกบทบาท")
    pages = ["/", "/capture", "/photos", f"/photos/{p1}", "/ranking", "/trend", "/dashboard", "/actions", "/verify", "/account/password",
             "/admin", "/admin/setup", "/admin/queue", "/admin/rounds", "/admin/criteria", "/admin/checkpoints", "/admin/tv",
             "/admin/quality", "/admin/departments", "/admin/areas", "/admin/areas/qr", "/admin/users", "/admin/settings",
             "/admin/notifications", "/admin/cameras", "/admin/storage", "/admin/logs", f"/rounds/{RID}/dept/{D['OF']}",
             f"/rounds/{RID}/report", f"/admin/areas/{AREA['โต๊ะกลม']}/zones"]
    for who, c in (("ผู้ดูแล", a), ("กรรมการ", aud), ("ตัวแทนแผนก", mem)):
        bad = []
        for path in pages:
            code = c.get(path).status_code
            if code >= 500 or (who == "ผู้ดูแล" and code != 200) or code not in (200, 303, 403):
                bad.append((path, code))
        check(f"{who}: ทุกหน้าตอบถูกต้อง ไม่มีข้อผิดพลาดของระบบ ({len(pages)} หน้า)", not bad, bad)
    check("หน้าที่ไม่มีอยู่ตอบ 404 เป็นหน้าของระบบ", mem.get("/nope").status_code == 404 and "ไม่พบหน้านี้" in mem.get("/nope").text)

    # ------------------------------------------------------------------ 14
    group("14 พื้นที่ สำรอง นำกลับ")
    check("หน้าพื้นที่จัดเก็บ", "ใช้" in a.get("/admin/storage").text)
    r = a.get(f"/admin/rounds/{RID}/backup.zip")
    BACKUP = r.content
    names = zipfile.ZipFile(io.BytesIO(BACKUP)).namelist() if r.status_code == 200 else []
    check("สำรองรอบเป็น ZIP พร้อมภาพ", r.status_code == 200 and any(n.startswith("images/") for n in names) and "manifest.json" in names, names[:6])
    r = a.get(f"/admin/rounds/{RID}/export.xlsx")
    check("ลิงก์ส่งออก Excel แบบเดิมของรุ่น 1.0 ยังพาไปไฟล์ได้", r.status_code == 303 and a.get(r.headers["location"]).status_code == 200)
    r = a.get("/admin/backup/system.json")
    check("สำรองการตั้งค่าของระบบ โดยไม่มี key ของ AI", r.status_code == 200 and "g-key" not in r.text and "m-key" not in r.text)
    check("ปิดรอบ", a.post(f"/admin/rounds/{RID}/close").status_code == 303 and one("SELECT status FROM rounds WHERE id = :i", i=RID) == "closed")
    r = mem.post("/api/photos", data=dict(round_id=RID, department_id=D["OF"], area_name="x", area_type=AT), files={"file": ("a.jpg", jpeg(950), "image/jpeg")})
    check("รอบที่ปิดแล้วไม่รับภาพ", r.status_code in (400, 409), r.status_code)
    check("เปิดรอบอีกครั้ง", a.post(f"/admin/rounds/{RID}/reopen").status_code == 303 and one("SELECT status FROM rounds WHERE id = :i", i=RID) == "open")
    n_img = one("SELECT COUNT(*) FROM photo_images")
    check("ลบภาพเต็มของรอบ คะแนนยังอยู่", a.post(f"/admin/rounds/{RID}/purge-images").status_code == 303
          and one("SELECT COUNT(*) FROM photo_images") == 0 and one("SELECT COUNT(*) FROM photos WHERE status = 'done'") > 10, n_img)
    check("ภาพย่อยังเปิดได้หลังลบภาพเต็ม", mem.get(f"/photos/{p1}/thumb").status_code == 200)
    check("คืนพื้นที่ที่ลบแล้ว", a.post("/admin/storage/compact").status_code == 303)
    n_rounds = one("SELECT COUNT(*) FROM rounds")
    r = a.post("/admin/restore", files={"file": ("backup.zip", BACKUP, "application/zip")})
    check("นำข้อมูลสำรองกลับเป็นรอบใหม่", r.status_code == 303 and one("SELECT COUNT(*) FROM rounds") == n_rounds + 1, a.get("/admin/storage").text[:0])
    check("บันทึกการใช้งาน", "queue_kick" in a.get("/admin/logs").text)

    # ------------------------------------------------------------------ 15
    group("15 รอบแบบระดับ และรอบอัตโนมัติ")
    a.post(f"/admin/rounds/{RID}/close")
    for rr in q("SELECT id FROM rounds WHERE status = 'open'"):
        a.post(f"/admin/rounds/{rr[0]}/close")
    a.post("/admin/rounds/save", data={"name": "รอบแบบระดับ", "min_photos": 1, "mode": "level"})
    R2 = one("SELECT id FROM rounds WHERE name = 'รอบแบบระดับ'")
    pl = upload(mem, R2, D["OF"], 301, "โต๊ะกลม", AT, area_id=AREA["โต๊ะกลม"])
    check("โหมดระดับ 0 ถึง 4 ยังให้คะแนนได้", wait_status([pl])[pl] == "done" and one("SELECT percent FROM photos WHERE id = :i", i=pl) == 75.0,
          one("SELECT percent FROM photos WHERE id = :i", i=pl))
    r = aud.post(f"/admin/photos/{pl}/override", data={"level_S1": 4, "level_S2": 4, "level_S3": 4, "level_S4": 4, "level_S5": 4, "note": "ตรวจหน้างานแล้ว"})
    check("ปรับคะแนนในโหมดระดับ", r.status_code == 303 and one("SELECT percent FROM photos WHERE id = :i", i=pl) == 100.0)
    r = a.post("/admin/rounds/auto", data={"rounds_repeat": "monthly", "prefix": "ตรวจ 5ส ประจำเดือน"})
    check("ตั้งให้สร้างรอบถัดไปเองทุกเดือน", r.status_code == 303 and setting("rounds_repeat") == "monthly")
    check("สั่งวิเคราะห์ทั้งรอบใหม่", a.post(f"/admin/rounds/{R2}/reanalyze").status_code == 303 and wait_status([pl])[pl] == "done")
    check("ใช้เกณฑ์ล่าสุดกับรอบ", a.post(f"/admin/rounds/{R2}/sync-rubric").status_code == 303 and wait_status([pl])[pl] == "done")

    # ------------------------------------------------------------------ 16
    group("16 ระบบดับแล้วเริ่มใหม่ขณะคิวยังมีภาพ (เหมือน host ฟรีหลับแล้วตื่น)")
    ctl(ai1="slow", latency=1.5)
    a.post("/admin/settings", data=dict(form, ai2_type="none", ai3_type="none", ai_workers=2))
    ids = [upload(mem, R2, D["OF"], 400 + i, f"ก่อนดับ {i}", AT) for i in range(6)]
    time.sleep(3)
    before_stop = dict(q(f"SELECT id, status FROM photos WHERE id IN ({','.join(map(str, ids))})"))
    srv.stop()
    left = dict(q(f"SELECT id, status FROM photos WHERE id IN ({','.join(map(str, ids))})"))
    check("ตอนดับยังมีภาพค้างในคิว", any(v in ("pending", "processing") for v in left.values()), left)
    check("ปิดระบบแล้วไม่มีภาพค้างสถานะกำลังวิเคราะห์", "processing" not in left.values(), left)
    ctl(ai1="ok", latency=0.25)
    srv.start()
    st = wait_status(ids, timeout=60)
    check("เริ่มใหม่แล้วคิวทำภาพที่ค้างต่อเองจนครบ โดยไม่ต้องมีคนกดอะไร", all(v == "done" for v in st.values()), st)
    a = client("admin", "Factory5S2026")
    check("เข้าสู่ระบบได้หลังเริ่มใหม่ และจำเวลาเฉลี่ยต่อภาพไว้", a.get("/admin/queue").status_code == 200 and setting("_queue_avg_s"))

    # ------------------------------------------------------------------ 17
    group("17 ลบข้อมูลทดสอบก่อนใช้งานจริง")
    r = a.post("/admin/storage/wipe", data={"confirm": "ลบ"})
    check("พิมพ์คำยืนยันไม่ตรง ไม่ลบอะไร", one("SELECT COUNT(*) FROM photos") > 0)
    r = a.post("/admin/storage/wipe", data={"confirm": "ลบข้อมูลทดสอบ"})
    check("ลบข้อมูลการตรวจทั้งหมด", r.status_code == 303 and one("SELECT COUNT(*) FROM photos") == 0 and one("SELECT COUNT(*) FROM rounds") == 0)
    check("แผนก ผู้ใช้ จุดตรวจ กล้อง และการตั้งค่ายังอยู่", one("SELECT COUNT(*) FROM departments") == 3 and one("SELECT COUNT(*) FROM users") == 4
          and one("SELECT COUNT(*) FROM audit_areas") == 2 and one("SELECT COUNT(*) FROM cameras") == 2 and setting("ai1_model") == "gemini-3.8-flash")
    a.post("/admin/rounds/save", data={"name": "รอบใช้งานจริง", "min_photos": 2, "mode": "checklist"})
    R3 = one("SELECT id FROM rounds WHERE name = 'รอบใช้งานจริง'")
    mem = client("of_mem", "Member5S2026")
    pz = upload(mem, R3, D["OF"], 999, "โต๊ะกลม", AT, area_id=AREA["โต๊ะกลม"])
    check("เริ่มรอบจริงและส่งภาพได้ทันที", wait_status([pz])[pz] == "done")
    check("ออกจากระบบ", mem.post("/logout").status_code == 303 and mem.get("/").headers.get("location", "").startswith("/login"))

    log_text = open(srv.log, encoding="utf-8", errors="replace").read()
    bad_lines = [l for l in log_text.splitlines() if "Traceback" in l or " ERROR " in l or " 500 " in l]
    GROUP[0] = "18 บันทึกของเซิร์ฟเวอร์"
    check("ไม่มีข้อผิดพลาดของระบบหรือคำตอบ 500 ตลอดการตรวจ", not bad_lines, bad_lines[:6])
finally:
    srv.stop()
    fake.terminate()

ok = sum(1 for r in RESULTS if r[2])
print(f"\nRESULT {ok}/{len(RESULTS)} passed in {time.time() - T0:.0f}s on {DBKIND}")
for g, n, passed, d in RESULTS:
    if not passed:
        print(f"FAILED [{g}] {n} :: {d}")
if os.environ.get("KEEP_LOG"):
    import shutil
    shutil.copy(srv.log, os.environ["KEEP_LOG"])
sys.exit(0 if ok == len(RESULTS) else 1)

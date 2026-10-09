"""ชุดทดสอบของรุ่น 1.8: คิววิเคราะห์ที่ไม่ปล่อยให้ภาพค้าง

ครอบคลุมสาเหตุที่ทำให้ภาพค้างสถานะ รอ AI วิเคราะห์ เป็นชั่วโมงในรุ่นก่อนหน้า
- ผู้ให้บริการตอบว่าเกินโควตา (429) พร้อมเวลารอยาว
- ขัดข้องชั่วคราว ตั้งค่าผิด และภาพที่มีปัญหาเฉพาะตัว
- ฐานข้อมูลสะดุดระหว่างบันทึกผล ภาพค้างสถานะกำลังวิเคราะห์
- หลายเธรดหยิบภาพพร้อมกัน เธรดจริงทำงานจนคิวหมด
- เพดานต่อวันแยกตามผู้ให้บริการ การกัน host หลับ หน้าคิวของผู้ดูแล
"""
import io
import json
import os
import random
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v18_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, db as dbm, keepalive, notify, providers, settings_store, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
S = {}
MODE = {"ai1": "ok", "ai2": "ok", "ai3": "ok"}
CALLS = []                      # (ช่อง, หมายเหตุของภาพ, เวลา)
HOOK = {"fn": None}             # ฟังก์ชันที่ให้ทำงานระหว่างที่ AI กำลังตอบ
CLOCK = {"off": 0.0}            # เลื่อนนาฬิกาของผู้ให้บริการไปข้างหน้า (วินาที)
FLAKY = {"fail": 0, "skip": 0}  # ให้ commit ล้มเหลวกี่ครั้ง หลังข้ามไปกี่ครั้ง
LOCK = threading.Lock()

BODIES = {
    "429min": (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                               "message": "You exceeded your current quota, please check your plan and billing details. "
                                          "Please retry in 34.5s.",
                               "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                                   {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                                   {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}),
    "429day": (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                               "message": "You exceeded your current quota, please check your plan and billing details.",
                               "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                                   {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                                   {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12310s"}]}}),
    "503": (503, {"error": {"message": "The model is overloaded. Please try again later."}}),
    "404": (404, {"error": {"message": "This model models/gemini-2.5-flash is no longer available to new users. "
                                       "Please update your code to use models/gemini-3.8-flash for the latest features."}}),
    "400": (400, {"error": {"message": "Request contains an invalid argument."}}),
    "401": (401, {"error": {"message": "Incorrect API key provided."}}),
}


def jpeg() -> bytes:
    im = Image.new("RGB", (900, 700), (random.randrange(256), random.randrange(256), random.randrange(256)))
    for _ in range(20):
        x, y = random.randrange(800), random.randrange(600)
        im.paste((random.randrange(256), random.randrange(256), random.randrange(256)), (x, y, x + 80, y + 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def ai_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    body = json.loads(request.content)
    if "/chat/completions" in url:
        slot, prompt = ("ai3" if "third.test" in url else "ai2"), body["messages"][1]["content"][0]["text"]
    else:
        slot, prompt = "ai1", body["contents"][0]["parts"][-1]["text"]
    note = re.search(r"- หมายเหตุ: (.*)", prompt).group(1).strip()
    with LOCK:
        CALLS.append((slot, note, time.time()))
    if HOOK["fn"]:
        fn, HOOK["fn"] = HOOK["fn"], None
        fn()
    mode = MODE[slot]
    if "POISON" in note:
        mode = "503"
    if mode in BODIES:
        code, data = BODIES[mode]
        return httpx.Response(code, json=data)
    if mode == "garbage":
        text_ = "ขออภัย ตอบไม่ได้"
    else:
        codes = re.search(r"ต้องมีครบทุกเกณฑ์ตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        text_ = json.dumps(dict(image_ok=True, scene="-", summary="-", top_actions=[],
                                criteria=[dict(code=c, na=False, level=3, reason="-", findings=[], recommendations=[])
                                          for c in codes]), ensure_ascii=False)
    if slot != "ai1":
        return httpx.Response(200, json={"choices": [{"message": {"content": text_}}]})
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text_}]}}]})


_real_commit = Session.commit


def _flaky_commit(self):
    if FLAKY["fail"] > 0:
        if FLAKY["skip"] > 0:
            FLAKY["skip"] -= 1
        else:
            FLAKY["fail"] -= 1
            self.rollback()
            raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))
    return _real_commit(self)


@pytest.fixture(scope="module", autouse=True)
def wiring():
    old, old_wait, old_clock = ai._transport, worker._stop.wait, providers._clock
    ai._transport = httpx.MockTransport(ai_handler)
    providers._clock = lambda: time.time() + CLOCK["off"]
    Session.commit = _flaky_commit
    providers.reset()
    yield
    Session.commit = _real_commit
    ai._transport, providers._clock = old, old_clock
    worker._stop.wait = old_wait
    providers.reset()
    worker.stop()


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


def photo(pid):
    with dbm.SessionLocal() as s:
        return s.get(dbm.Photo, pid)


def edit(pid, **kw):
    with dbm.SessionLocal() as s:
        p = s.get(dbm.Photo, pid)
        for k, v in kw.items():
            setattr(p, k, v)
        s.commit()


def shoot(note="", c=None) -> int:
    r = (c or S["mem"]).post("/api/photos", data=dict(round_id=S["rid"], department_id=S["did"], area_type="สำนักงาน",
                                                    area_name=f"จุด {random.randrange(10 ** 7)}", note=note),
                             files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def drain(limit=100) -> int:
    n = 0
    while worker.process_one() and n < limit:
        n += 1
    return n


def calls(slot=None, note=None) -> int:
    with LOCK:
        return sum(1 for s_, n, _ in CALLS if (slot is None or s_ == slot) and (note is None or n == note))


def alerts() -> list:
    with dbm.SessionLocal() as s:
        return [x.detail for x in s.query(dbm.AuditLog).filter(dbm.AuditLog.action == "alert").order_by(dbm.AuditLog.id).all()]


def fresh(**kw):
    """เริ่มกรณีทดสอบใหม่: ผู้ให้บริการพร้อมใช้ นาฬิกาตรง ไม่มีภาพค้างจากกรณีก่อน"""
    providers.reset()
    CLOCK["off"] = 0.0
    MODE.update(ai1="ok", ai2="ok", ai3="ok")
    worker._probes.clear()
    worker.state.update(paused_reason="", paused_kind="", resume_at=None, paused_since=None, last_error="")
    with dbm.SessionLocal() as s:
        s.query(dbm.Photo).filter(dbm.Photo.status.in_(["pending", "processing"])).update(
            {"status": "error", "error": "จบกรณีทดสอบ"}, synchronize_session=False)
        s.query(dbm.AiUsage).delete()
        s.commit()
    base = dict(ai1_type="gemini", ai1_key="g", ai1_model="gemini-3.8-flash", ai1_base="", ai2_type="none", ai2_base="",
                ai2_key="", ai2_model="", ai3_type="none", ai3_base="", ai3_key="", ai3_model="", ai_passes=1, ai_daily=100000,
                ai2_daily=0, ai3_daily=0, ai_rpm=60, ai2_rpm=60, ai3_rpm=60, ai_max_attempts=4, ai_workers=2, queue_alert_min=30)
    base.update(kw)
    save(**base)
    with LOCK:
        CALLS.clear()


BACKUP = dict(ai2_type="openai", ai2_base="https://backup.test/v1", ai2_key="o", ai2_model="vision-b")
THIRD = dict(ai3_type="openai", ai3_base="https://third.test/v1", ai3_key="t", ai3_model="vision-c")


def test_01_upgrade_from_1_7_keeps_waiting_photos():
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
    save(scoring_mode="level", verify_required=False, require_coverage=False, allow_free_area=True, max_photos_per_dept=0,
         storage_budget_mb=350, retention_days=0, area_types=["สำนักงาน", "สายการผลิต"], rounds_repeat="off", auto_actions=False,
         gallery_max_age_h=0, member_see_all=True, preset="", public_url="", keep_awake="queue", keep_awake_hours="")
    assert a.post("/admin/departments/save", data={"code": "Q1", "name": "สำนักงานคิว"}).status_code == 303
    assert a.post("/admin/rounds/save", data={"name": "รอบทดสอบคิว 1.8", "min_photos": 1, "mode": "level"}).status_code == 303
    with dbm.SessionLocal() as s:
        S["did"] = s.query(dbm.Department).filter_by(code="Q1").one().id
        S["rid"] = s.query(dbm.Round).filter_by(name="รอบทดสอบคิว 1.8").one().id
    a.post("/admin/users/save", data={"username": "q_mem", "full_name": "ตัวแทนคิว", "role": "member", "department_id": S["did"],
                                      "password": "Member5S2026", "active": "1"})
    a.post("/admin/users/save", data={"username": "q_aud", "full_name": "กรรมการคิว", "role": "auditor",
                                      "password": "Auditor5S2026", "active": "1"})
    S["mem"] = client("q_mem", "Member5S2026", "Member5S2026x")
    S["aud"] = client("q_aud", "Auditor5S2026", "Auditor5S2026x")
    fresh()
    old = shoot("ภาพจากรุ่นเดิม")
    # ฐานข้อมูลของรุ่น 1.7 ไม่มีคอลัมน์ของคิวรุ่นใหม่
    with dbm.engine.begin() as conn:
        conn.execute(text('ALTER TABLE photos DROP COLUMN queued_at'))
        conn.execute(text('ALTER TABLE photos DROP COLUMN started_at'))
        conn.execute(text('ALTER TABLE photos DROP COLUMN ai_slot'))
        conn.execute(text('ALTER TABLE ai_usage DROP COLUMN count2'))
        conn.execute(text('ALTER TABLE ai_usage DROP COLUMN count3'))
    dbm.engine.dispose()
    dbm.init_db()
    with dbm.SessionLocal() as s:
        p = s.get(dbm.Photo, old)
        assert p.status == "pending" and p.queued_at is None and p.started_at is None
        assert worker.waits(s, [old])[old]["position"] == 1          # ภาพเดิมที่ไม่มีเวลาเข้าคิว ยังนับลำดับได้
        assert worker.queue_info(s)["waited_min"] == 0
    assert drain() == 1 and photo(old).status == "done"
    with dbm.SessionLocal() as s:
        assert worker.usage_today(s) == 1 and worker.usage_backup_today(s) == 0
    assert photo(shoot()).queued_at is not None
    drain()


def test_02_rate_limit_pauses_the_provider_not_the_photo():
    fresh()
    MODE["ai1"] = "429min"
    a_ = shoot("A")
    assert worker.process_one() is True
    p = photo(a_)
    assert p.status == "pending" and p.attempts == 0 and p.next_try_at is None and p.started_at is None
    assert "429" in p.error and worker.state["paused_kind"] == ""
    b_ = shoot("B")
    assert worker.process_one() is False and calls("ai1") == 1      # ผู้ให้บริการพักอยู่ ไม่เรียกซ้ำให้เปลืองโควตา
    assert worker.state["paused_kind"] == "rate" and "เรียกถี่เกินโควตาต่อนาที" in worker.state["paused_reason"]
    wait_s = (worker.state["resume_at"] - dbm.now()).total_seconds()
    assert 33 < wait_s < 37 and 1 <= worker.state["idle"] <= 37     # 34.5 วินาทีตามที่ผู้ให้บริการบอก บวกกันชน 1 วินาที
    with dbm.SessionLocal() as s:
        w = worker.waits(s, [a_, b_])
        assert (w[a_]["position"], w[b_]["position"]) == (1, 2) and "เรียกถี่" in w[b_]["reason"]
        assert 30 < w[a_]["eta_s"] < 60                               # เวลาที่คาดรวมช่วงที่คิวหยุดรอแล้ว
        with dbm.SessionLocal() as s2:
            usage = providers.load_usage(s2)
        assert usage["ai1"] == 0                                      # ครั้งที่ถูกปฏิเสธเพราะโควตา ไม่นับเข้าเพดานต่อวัน
    MODE["ai1"] = "ok"
    CLOCK["off"] = 36
    assert drain() == 2
    assert photo(a_).status == "done" and photo(b_).status == "done" and photo(a_).attempts == 0
    order = [n for _s, n, _t in CALLS]
    assert order == ["A", "A", "B"]                                   # ภาพที่ส่งก่อนได้ทำก่อน
    assert worker.state["paused_reason"] == ""


def test_03_daily_quota_with_long_retry_delay_never_parks_a_photo_for_hours():
    fresh()
    MODE["ai1"] = "429day"
    c_ = shoot("C")
    worker.process_one()
    assert worker.process_one() is False
    assert worker.state["paused_kind"] == "quota" and "AI หลักแจ้งว่าโควตาของวันนี้หมดแล้ว" in worker.state["paused_reason"]
    wait_s = (worker.state["resume_at"] - dbm.now()).total_seconds()
    assert 12 < wait_s <= 17                                          # ผู้ให้บริการบอกให้รอ 12310 วินาที ระบบลองใหม่ใน 15 วินาที
    assert worker.state["idle"] <= worker.BLOCKED_MAX
    p = photo(c_)
    assert p.status == "pending" and p.attempts == 0 and p.next_try_at is None
    assert any("คิววิเคราะห์ภาพหยุดรอ มี 1 ภาพในคิว" in x and "โควตาของวันนี้" in x for x in alerts())
    # มี AI สำรอง: ใช้แทนทันทีโดยไม่ไปเรียกตัวหลักที่โควตาหมด
    save(**BACKUP)
    before = calls("ai1")
    assert drain() == 1
    p = photo(c_)
    assert p.status == "done" and p.provider == "openai" and p.model == "vision-b" and calls("ai1") == before
    d_ = shoot("D")
    drain()
    assert photo(d_).provider == "openai" and calls("ai1") == before
    with dbm.SessionLocal() as s:
        assert worker.usage_today(s) == 0 and worker.usage_backup_today(s) == 2
        snap = {x["slot"]: x for x in worker.queue_info(s)["providers"]}
        assert snap["ai1"]["ok"] is False and snap["ai1"]["kind"] == "quota" and snap["ai2"]["ok"] is True
    # ไม่มีสำรอง: ครบ 30 นาทีจึงลองเชิงหนึ่งครั้ง ยังไม่ได้ก็รอต่อ ภาพไม่เสียจำนวนครั้ง
    save(ai2_type="none")
    e_ = shoot("E")
    assert worker.process_one() is False and calls("ai1") == before
    CLOCK["off"] = providers.QUOTA_PROBE + 5
    assert worker.process_one() is True and calls("ai1") == before + 1
    assert photo(e_).status == "pending" and photo(e_).attempts == 0
    assert worker.process_one() is False
    # ยังหมดอยู่: ช่วงลองใหม่ยาวขึ้นเป็น 15 วินาที, 1, 3, 10 แล้วทุก 30 นาที และไม่เกินเวลาที่โควตารอบใหม่เริ่ม
    cfg = ai.profiles(settings_store.load())[0]
    providers.reset()
    steps, want = [], []
    for step in (15, 60, 180, 600, 1800, 1800):
        t0 = providers.now()
        want.append(min(step, max(15, round(providers.quota_reset_at() - t0))))
        providers.failure(cfg, ai.AIError("429", kind="quota", retry_after=12310, status=429))
        providers.failure(cfg, ai.AIError("429", kind="quota", retry_after=12310, status=429))   # เธรดที่สองเจอเหตุเดียวกัน: ไม่นับซ้ำ
        steps.append(round(providers.blocked(cfg, settings_store.load())[1] - t0))
        assert "โควตารอบใหม่เริ่มประมาณ" in providers.blocked(cfg, settings_store.load())[2]
        CLOCK["off"] += steps[-1] + 1
    assert steps == want
    MODE["ai1"] = "ok"
    assert drain() == 1 and photo(e_).status == "done" and photo(e_).provider == "gemini"


def test_04_repeated_rate_limits_escalate_and_pacing_adapts():
    fresh(ai_rpm=6)
    s = settings_store.load()
    cfg = ai.profiles(s)[0]
    # จังหวะการเรียก: 6 ครั้งต่อนาที = ห่างกัน 10 วินาที แม้หลายเธรดจองพร้อมกัน
    assert [round(providers.reserve(cfg, s)) for _ in range(3)] == [0, 10, 20]
    providers.reset()
    cools = []
    for _ in range(6):
        t = providers.now()
        providers.failure(cfg, ai.AIError("429", kind="rate", retry_after=2, status=429))
        providers.failure(cfg, ai.AIError("429", kind="rate", retry_after=2, status=429))        # เธรดที่สองเจอเหตุเดียวกัน: ไม่นับซ้ำ
        cools.append(round(providers.blocked(cfg, s)[1] - t))
        CLOCK["off"] += cools[-1] + 1                                 # ครบเวลาพักแล้วลองใหม่ ยังโดนอีก
    assert cools == [5, 5, 5, 240, 480, 960]                          # สามครั้งแรกเชื่อผู้ให้บริการ จากนั้นถอยห่างขึ้นเอง
    h = providers.health(cfg)
    assert h.pace == providers.PACE_MAX
    snap = providers.snapshot(s)[0]
    assert snap["slowed"] is True and snap["rpm_now"] == 0.8 and snap["rpm"] == 6
    for _ in range(60):
        providers.success(cfg)
    assert providers.health(cfg).pace == 1.0 and providers.blocked(cfg, s) is None
    # อ่านเวลาที่ผู้ให้บริการขอให้รอได้ทุกรูปแบบ
    for raw, want in (("34.5s", 34.5), ("7m12.5s", 432.5), ("1h2m3s", 3723.0), ("450ms", 0.45), ("x", None)):
        assert ai.parse_delay(raw) == want
    groq = httpx.Response(429, json={"error": {"message": "Rate limit reached for model `scout` on requests per day (RPD): "
                                                          "Limit 1000, Used 1000. Please try again in 7m12.5s.", "code": "rate_limit_exceeded"}})
    e = ai._http_error(groq)
    assert e.kind == "quota" and e.retry_after == 432.5
    tpm = httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "Rate limit reached on tokens per minute (TPM)"}})
    e = ai._http_error(tpm)
    assert e.kind == "rate" and e.retry_after == 7
    kinds = {code: ai._http_error(httpx.Response(code, json=BODIES[str(code)][1])).kind for code in (503, 404, 400, 401)}
    assert kinds == {503: "transient", 404: "config", 400: "content", 401: "config"}
    bad_key = httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}})
    assert ai._http_error(bad_key).kind == "config"


def test_05_outage_opens_the_breaker_and_probes_do_not_burn_attempts():
    fresh()
    MODE["ai1"] = "503"
    d1, d2 = shoot("D1"), shoot("D2")
    assert worker.process_one() is True
    p = photo(d1)
    assert p.status == "pending" and p.attempts == 1 and 8 < (p.next_try_at - dbm.now()).total_seconds() <= 10
    assert worker.state["paused_reason"] == ""                        # ล้มเหลวครั้งเดียวยังไม่พักผู้ให้บริการ
    assert worker.process_one() is True and photo(d2).attempts == 1  # ครั้งที่สองติดกัน: เบรกเกอร์เปิด
    assert worker.process_one() is False and worker.state["paused_kind"] == "transient"
    assert "ขัดข้องชั่วคราว" in worker.state["paused_reason"] and "503" in worker.state["paused_reason"]
    assert 3 < (worker.state["resume_at"] - dbm.now()).total_seconds() <= 6
    # ระหว่างที่ผู้ให้บริการยังล่ม การลองเชิงไม่นับเป็นความผิดของภาพ และช่วงพักยาวขึ้นเป็นขั้น
    steps = []
    for i in range(4):
        edit(d1, next_try_at=None)
        edit(d2, next_try_at=None)
        CLOCK["off"] += 400
        t = providers.now()
        assert worker.process_one() is True
        assert worker.process_one() is False
        steps.append(round(providers.blocked(ai.profiles(settings_store.load())[0], settings_store.load())[1] - t))
    assert steps == [15, 30, 60, 120]
    assert photo(d1).attempts == 1 and photo(d2).attempts == 1 and photo(d1).status == "pending"
    MODE["ai1"] = "ok"
    CLOCK["off"] += 400
    assert drain() == 2
    assert photo(d1).status == "done" and photo(d2).status == "done"
    assert not any("AI วิเคราะห์ภาพไม่สำเร็จ" in x and "503" in x for x in alerts())


def test_06_a_photo_that_fails_by_itself_does_not_block_the_queue():
    fresh(ai_max_attempts=3)
    bad, ok1, ok2 = shoot("POISON 1"), shoot("ปกติ 1"), shoot("ปกติ 2")
    assert drain() == 3
    assert photo(ok1).status == "done" and photo(ok2).status == "done"
    p = photo(bad)
    assert p.status == "pending" and p.attempts == 1 and worker.state["paused_reason"] == ""
    for want in (2, 3):
        edit(bad, next_try_at=None)
        ok = shoot("ปกติ")                                           # ภาพใหม่ที่ยังไม่เคยล้มเหลวได้ทำก่อนภาพที่มีปัญหา
        assert worker.process_one() is True and photo(ok).status == "done"
        assert worker.process_one() is True and photo(bad).attempts == want
    p = photo(bad)
    assert p.status == "error" and "503" in p.error and p.next_try_at is None
    assert any("AI วิเคราะห์ภาพไม่สำเร็จ" in x for x in alerts())
    # ระยะรอของภาพที่ล้มเหลวเอง: 10, 30, 60, 120 แล้วไม่เกิน 300 วินาที (รุ่นก่อนรอได้ถึง 3600)
    assert worker.BACKOFF == (10.0, 30.0, 60.0, 120.0, 300.0) and max(worker.BACKOFF) <= 300
    # ผู้ให้บริการล่มจริงและมีภาพเดียว: ลองเชิงครบจำนวนแล้วจึงถือว่าภาพมีปัญหาเอง ไม่วนไม่สิ้นสุด
    fresh(ai_max_attempts=3)
    MODE["ai1"] = "503"
    lone = shoot("ภาพเดียว")
    for _ in range(worker.PROBE_LIMIT + 4):
        edit(lone, next_try_at=None) if photo(lone).status == "pending" else None
        CLOCK["off"] += 400
        worker.process_one()
    p = photo(lone)
    assert p.status == "error" and "ลองหลายครั้งแล้วไม่สำเร็จ" in p.error and p.attempts == 2


def test_07_wrong_settings_keep_photos_waiting_and_resume_after_fix():
    fresh(ai1_model="gemini-2.5-flash")
    MODE["ai1"] = "404"
    g1, g2 = shoot("G1"), shoot("G2")
    assert drain() == 1 and calls("ai1") == 1                         # ภาพแรกเจอ 404 แล้วไม่เรียกซ้ำกับภาพอื่น
    assert photo(g1).status == "pending" and photo(g1).attempts == 0 and photo(g2).status == "pending"
    assert worker.state["paused_kind"] == "config"
    assert "วิธีแก้: พิมพ์ gemini-3.8-flash ในช่องโมเดล" in worker.state["paused_reason"]
    assert any("คิววิเคราะห์ภาพหยุดรอ มี 2 ภาพในคิว" in x for x in alerts())
    r = S["mem"].get(f"/api/photos/status?ids={g1},{g2}").json()
    assert all("รอคิวลำดับที่" in x["wait"] and "gemini-3.8-flash" in x["wait"] for x in r["photos"])
    assert "gemini-3.8-flash" in S["mem"].get(f"/photos/{g1}").text
    # ผู้ดูแลแก้ชื่อโมเดลแล้วบันทึก: คิวเดินต่อเอง ไม่ต้องสั่งวิเคราะห์ใหม่
    MODE["ai1"] = "ok"
    assert worker.process_one() is False
    save(ai1_model="gemini-3.8-flash")
    assert drain() == 2 and photo(g1).status == "done" and photo(g2).model == "gemini-3.8-flash"
    # คำขอถูกปฏิเสธด้วย 400 สามภาพติดกัน = ปัญหาของการตั้งค่า: หยุดก่อนที่ทั้งชุดจะกลายเป็นผิดพลาด
    fresh()
    MODE["ai1"] = "400"
    ids = [shoot(f"H{i}") for i in range(5)]
    drain()
    states = [photo(i).status for i in ids]
    assert states == ["error", "error", "pending", "pending", "pending"] and worker.state["paused_kind"] == "config"
    # key ของตัวสำรองผิดด้วย: ทั้งสองเจ้าใช้ไม่ได้ ภาพยังรอ ไม่กลายเป็นผิดพลาด
    fresh(**BACKUP)
    MODE.update(ai1="401", ai2="401")
    k = shoot("K")
    assert drain() == 1 and photo(k).status == "pending" and photo(k).attempts == 0
    with dbm.SessionLocal() as s:
        snap = worker.queue_info(s)["providers"]
        assert [x["kind"] for x in snap] == ["config", "config"] and all("API key" in x["reason"] for x in snap)
    MODE.update(ai1="ok", ai2="ok")
    assert S["admin"].post("/admin/queue/kick").status_code == 303
    assert drain() == 1 and photo(k).status == "done"


def test_08_database_trouble_never_leaves_a_photo_stuck():
    fresh()
    worker._stop.wait = lambda seconds=None: False                    # ไม่ต้องรอจริงระหว่างลองบันทึกซ้ำ
    # บันทึกผลพลาดหนึ่งครั้งเพราะการเชื่อมต่อหลุด: ลองซ้ำเองแล้วสำเร็จ ไม่ต้องเรียก AI ใหม่
    one = shoot("สะดุดหนึ่งครั้ง")
    HOOK["fn"] = lambda: FLAKY.update(fail=1, skip=1)                 # ข้าม commit ของตัวนับ แล้วให้ commit ของผลล้มเหลว
    assert worker.process_one() is True
    assert photo(one).status == "done" and calls(note="สะดุดหนึ่งครั้ง") == 1 and FLAKY["fail"] == 0
    # บันทึกไม่ได้เลยทุกครั้ง: ภาพค้างสถานะกำลังวิเคราะห์ แต่ผู้เฝ้าคิวคืนเข้าคิวให้เอง
    stuck = shoot("บันทึกไม่ได้")
    HOOK["fn"] = lambda: FLAKY.update(fail=4, skip=1)
    assert worker.process_one() is True
    assert photo(stuck).status == "processing" and FLAKY["fail"] == 0
    assert worker.process_one() is False                              # คิวไม่หยิบภาพที่กำลังวิเคราะห์ซ้ำ
    assert worker.recover_stuck(worker.STALE_AFTER) == 0              # ยังไม่นานพอ อาจกำลังวิเคราะห์จริง
    edit(stuck, started_at=dbm.now() - timedelta(seconds=worker.STALE_AFTER + 30))
    with worker._lock:
        worker._inflight[stuck] = time.monotonic()
    assert worker.recover_stuck(worker.STALE_AFTER) == 0              # ภาพที่เธรดของโปรเซสนี้ยังถืออยู่ ไม่ถูกแย่ง
    with worker._lock:
        worker._inflight.clear()
    assert worker.watch()["recovered"] == 1 and photo(stuck).status == "pending" and photo(stuck).attempts == 0
    assert drain() == 1 and photo(stuck).status == "done"
    # ผู้ดูแลสั่งวิเคราะห์ใหม่ขณะที่ AI กำลังตอบ: ผลรอบเก่าถูกทิ้ง ภาพถูกวิเคราะห์ใหม่หนึ่งครั้ง
    again = shoot("สั่งใหม่ระหว่างทาง")
    HOOK["fn"] = lambda: S["admin"].post(f"/admin/photos/{again}/reanalyze")
    assert worker.process_one() is True
    assert photo(again).status == "pending" and photo(again).analysis is None
    assert drain() == 1 and photo(again).status == "done" and calls(note="สั่งใหม่ระหว่างทาง") == 2
    # ภาพถูกลบขณะที่ AI กำลังตอบ: ไม่มีข้อผิดพลาด คิวเดินต่อ
    gone, nxt = shoot("ถูกลบระหว่างทาง"), shoot("ภาพถัดไป")
    HOOK["fn"] = lambda: S["admin"].post(f"/photos/{gone}/delete")
    assert drain() == 2 and photo(gone) is None and photo(nxt).status == "done"
    # เดินคิวเดี๋ยวนี้: ยกเลิกเวลารอ และคืนภาพที่ไม่มีเธรดใดถืออยู่เกิน 3 นาที
    w1, w2 = shoot("รอลองใหม่"), shoot("ค้างกลางทาง")
    edit(w1, next_try_at=dbm.now() + timedelta(minutes=4), attempts=1)
    edit(w2, status="processing", started_at=dbm.now() - timedelta(seconds=worker.ORPHAN_AFTER + 20))
    assert worker.process_one() is False
    with dbm.SessionLocal() as s:
        assert worker.kick(s) == {"released": 1, "stuck": 1}
    assert drain() == 2 and photo(w1).status == "done" and photo(w2).status == "done"
    # ระบบกำลังปิด: ภาพที่กำลังวิเคราะห์ถูกคืนเข้าคิว
    held = shoot("ค้างตอนปิดระบบ")
    edit(held, status="processing", started_at=dbm.now())
    with worker._lock:
        worker._inflight[held] = time.monotonic()
    worker.stop()
    worker._stop.clear()
    with worker._lock:
        worker._inflight.clear()
    assert photo(held).status == "pending"
    assert drain() == 1


def test_09_many_threads_never_analyse_the_same_photo_twice():
    fresh()
    notes = [f"พร้อมกัน {i}" for i in range(16)]
    ids = [shoot(n) for n in notes]
    errors = []

    def run():
        try:
            while worker.process_one():
                pass
        except Exception as e:                                         # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert all(photo(i).status == "done" for i in ids)
    assert [calls(note=n) for n in notes] == [1] * 16
    with dbm.SessionLocal() as s:
        assert worker.usage_today(s) == 16                             # ตัวนับไม่หายแม้หลายเธรดเขียนพร้อมกัน
    assert not worker._inflight


def test_10_real_worker_threads_drain_the_queue_and_recover_from_faults():
    fresh(ai_workers=3, ai_rpm=120)
    worker._stop.wait = threading.Event().wait                         # ผู้เฝ้าคิวต้องรอจริง
    real_house, real_stop = worker.housekeeping, worker._stop
    worker.housekeeping = lambda: {}
    worker._stop = threading.Event()
    try:
        first = [shoot(f"เธรดจริง {i}") for i in range(3)]
        edit(first[0], status="processing", started_at=None)           # ภาพที่ค้างจากก่อนระบบดับ
        worker.start()
        more = [shoot(f"เธรดจริง {i}") for i in range(3, 7)]             # ส่งระหว่างที่คิวทำงาน: การปลุกต้องไม่หาย
        deadline = time.time() + 40
        while time.time() < deadline and any(photo(i).status != "done" for i in first + more):
            time.sleep(0.2)
        assert all(photo(i).status == "done" for i in first + more)
        assert worker.alive() == 3 and worker.state["jobs"] >= 7 and worker.state["avg_s"] > 0
        stamps = sorted(t for s_, n, t in CALLS if n.startswith("เธรดจริง"))
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        # 120 ครั้งต่อนาที = เริ่มเรียกห่างกันครึ่งวินาที เวลาที่วัดได้ฝั่งผู้ให้บริการคลาดได้บ้างตามภาระของเครื่อง
        assert len(stamps) == 7 and min(gaps) > 0.2 and stamps[-1] - stamps[0] > 6 * 0.5 * 0.8
        # ลดจำนวนเธรด: เธรดส่วนเกินเลิกเอง
        save(ai_workers=1)
        worker.wake()
        deadline = time.time() + 10
        while time.time() < deadline and worker.alive() != 1:
            time.sleep(0.1)
        assert worker.alive() == 1
        # คิวว่างแล้วมีภาพใหม่: ถูกปลุกทันที ไม่ต้องรอครบรอบพัก
        t0 = time.time()
        late = shoot("เธรดจริง ปลุก")
        while time.time() - t0 < 10 and photo(late).status != "done":
            time.sleep(0.1)
        assert photo(late).status == "done" and time.time() - t0 < 5
    finally:
        worker.stop()
        for t in list(worker._threads.values()) + [worker._supervisor]:
            t.join(10)
        assert worker.alive() == 0 and not worker._supervisor.is_alive()
        worker._threads.clear()
        worker.housekeeping, worker._stop = real_house, real_stop
    # ข้อผิดพลาดที่ไม่คาดคิดในเธรด (เช่น ฐานข้อมูลหลุด): พักสั้น ๆ แล้วลองใหม่ ไม่พักเป็นชั่วโมง
    seen, real_run, real_wait = [], worker.run_one, worker._wait
    results = iter([RuntimeError("connection dropped"), ("empty", 3600.0)])

    def fake_run(pace=False):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    def fake_wait(gen, timeout):
        seen.append(timeout)
        if len(seen) == 2:
            worker._stop.set()
    worker.run_one, worker._wait = fake_run, fake_wait
    try:
        worker._work(0)
    finally:
        worker.run_one, worker._wait = real_run, real_wait
        worker._stop.clear()
    assert seen == [30.0, 3600.0]


def test_11_daily_caps_per_provider_and_quota_day():
    fresh(ai_daily=2, **BACKUP)
    ids = [shoot(f"เพดาน {i}") for i in range(4)]
    assert drain() == 4
    assert [photo(i).provider for i in ids] == ["gemini", "gemini", "openai", "openai"]   # ตัวหลักครบเพดาน ใช้ตัวสำรองต่อ
    with dbm.SessionLocal() as s:
        assert (worker.usage_today(s), worker.usage_backup_today(s)) == (2, 2)
    # ตัวสำรองมีเพดานของตัวเอง: ครบทั้งสองจึงหยุดรอ และบอกเวลาที่โควตารอบใหม่เริ่ม
    save(ai2_daily=2)
    last = shoot("เพดาน สุดท้าย")
    assert worker.process_one() is False and photo(last).status == "pending"
    assert worker.state["paused_kind"] == "cap" and "ใช้ครบเพดานต่อวันที่ตั้งไว้แล้ว (2 ครั้ง)" in worker.state["paused_reason"]
    assert "น." in worker.state["paused_reason"] and worker.state["idle"] <= worker.BLOCKED_MAX
    assert calls() == 4
    save(ai_daily=0)                                                   # 0 = ไม่จำกัด
    assert drain() == 1 and photo(last).provider == "gemini"
    # คำตอบที่ใช้ไม่ได้ก็นับ เพราะ AI รับงานไปทำแล้ว ส่วนครั้งที่ขัดข้องไม่นับ
    fresh()
    MODE["ai1"] = "garbage"
    shoot("ตอบไม่เป็น JSON")
    worker.process_one()
    MODE["ai1"] = "503"
    shoot("ขัดข้อง")
    worker.process_one()
    with dbm.SessionLocal() as s:
        assert worker.usage_today(s) == 1
    # วันของโควตาตัดที่เที่ยงคืนเวลาแปซิฟิก ทั้งช่วงเวลาออมแสงและเวลาปกติ
    def utc(*a):
        return datetime(*a, tzinfo=timezone.utc).timestamp()
    assert providers.quota_day(utc(2026, 10, 8, 6, 59)) == "2026-10-07" and providers.quota_day(utc(2026, 10, 8, 7, 1)) == "2026-10-08"
    assert providers.quota_day(utc(2026, 1, 15, 7, 59)) == "2026-01-14" and providers.quota_day(utc(2026, 1, 15, 8, 1)) == "2026-01-15"
    reset_at = providers.quota_reset_at(utc(2026, 10, 8, 3, 0))
    assert datetime.fromtimestamp(reset_at, timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-10-08 07:00"   # 14:00 น. เวลาไทย
    fresh()
    drain()


def test_12_keep_awake_only_when_needed():
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={"ok": True})
    keepalive._transport = httpx.MockTransport(handler)
    old_env = os.environ.pop("RENDER_EXTERNAL_URL", None)
    try:
        s = dict(settings_store.load(), keep_awake="queue", public_url="")
        keepalive._state.update(last=0.0)
        assert keepalive.target(s) == "" and keepalive.tick(s, busy=True) is False        # ไม่รู้ที่อยู่ของตัวเอง
        s["public_url"] = "https://audit5s.example.com/"
        assert keepalive.target(s) == "https://audit5s.example.com"
        assert keepalive.tick(s, busy=False) is False and sent == []                         # คิวว่าง ปล่อยให้หลับ
        assert keepalive.tick(s, busy=True) is True and sent == ["https://audit5s.example.com/healthz"]
        assert keepalive.tick(s, busy=True) is False                                         # ยังไม่ครบ 10 นาที
        keepalive._state["last"] = time.time() - keepalive.EVERY - 1
        assert keepalive.tick(s, busy=True) is True and len(sent) == 2
        st = keepalive.status(s)
        assert st["ok"] is True and st["count"] >= 2 and st["source"] == "setting"
        os.environ["RENDER_EXTERNAL_URL"] = "https://audit5s.onrender.com"                    # host บอกที่อยู่เอง ใช้ค่านี้ก่อน
        assert keepalive.target(s) == "https://audit5s.onrender.com" and keepalive.status(s)["source"] == "host"
        # ปิด = ไม่เรียก, คิวหยุดเพราะตั้งค่าผิดเกิน 2 ชั่วโมง = เลิกกันหลับ
        assert keepalive.wanted(dict(s, keep_awake="off"), busy=True) == ""
        long_ago = dbm.now() - timedelta(hours=3)
        assert keepalive.wanted(s, True, "config", long_ago) == "" and keepalive.wanted(s, True, "quota", long_ago) != ""
        assert keepalive.wanted(s, True, "config", dbm.now()) != ""
        # ช่วงเวลาที่ให้ตื่นตลอด (เวลาไทย) รวมช่วงข้ามเที่ยงคืน
        noon_th = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
        assert keepalive.in_hours("07:00-18:00", noon_th) and not keepalive.in_hours("13:00-18:00", noon_th)
        assert keepalive.in_hours("22:00-13:00", noon_th) and not keepalive.in_hours("x", noon_th)
        assert keepalive.clean_hours(" 7:00 - 18:30 ") == "07:00-18:30" and keepalive.clean_hours("25:00-26:00") == ""
        hours = "%02d:00-%02d:59" % ((dbm.now() + timedelta(hours=7)).hour, (dbm.now() + timedelta(hours=7)).hour)
        assert keepalive.wanted(dict(s, keep_awake="hours", keep_awake_hours=hours), busy=False) != ""
        # /healthz ไม่ต้องเข้าสู่ระบบ และไม่แตะฐานข้อมูล
        assert TestClient(app).get("/healthz").json()["ok"] is True
        # เรียกไม่สำเร็จไม่ทำให้ระบบล้ม และบอกสาเหตุ
        keepalive._transport = httpx.MockTransport(lambda r: httpx.Response(502))
        assert keepalive.ping("https://audit5s.onrender.com") is False and keepalive.status(s)["detail"] == "HTTP 502"
    finally:
        keepalive._transport = None
        os.environ.pop("RENDER_EXTERNAL_URL", None)
        if old_env:
            os.environ["RENDER_EXTERNAL_URL"] = old_env


def test_13_queue_pages_status_and_permissions():
    fresh(public_url="https://audit5s.example.com")
    a, m, aud = S["admin"], S["mem"], S["aud"]
    MODE["ai1"] = "429day"
    p1, p2 = shoot("หน้าคิว 1"), shoot("หน้าคิว 2")
    drain()
    with dbm.SessionLocal() as s:                                       # ไม่ให้ภาพผิดพลาดของกรณีก่อนหน้ามาปน
        s.query(dbm.Photo).filter(dbm.Photo.status == "error").update({"status": "rejected"}, synchronize_session=False)
        s.commit()
    edit(p2, status="error", error="โมเดลไม่รับภาพนี้ (SAFETY)")
    r = a.get("/admin/queue")
    assert r.status_code == 200
    for want in ("คิวหยุดรอ", "โควตาของวันนี้", "เดินคิวเดี๋ยวนี้", "วิเคราะห์ภาพที่ไม่สำเร็จใหม่ (1)", "ยังไม่มี AI สำรอง",
                 f'href="/photos/{p1}"', f'href="/photos/{p2}"', "โมเดลไม่รับภาพนี้ (SAFETY)", "https://audit5s.example.com/healthz", "ภาพที่รออยู่",
                 "เธรดของคิววิเคราะห์ไม่ได้ทำงานในโปรเซสนี้"):             # ชุดทดสอบปิดเธรดไว้ หน้านี้ต้องบอก
        assert want in r.text, want
    j = a.get("/admin/queue.json").json()
    assert j["pending"] == 1 and j["errors"] == 1 and "โควตาของวันนี้" in j["paused"]
    assert 'href="/admin/queue"' in a.get("/admin").text and "คิวหยุดรอ" in a.get("/admin").text
    # ผู้ส่งภาพเห็นลำดับและเหตุผลทั้งในหน้ารายละเอียด หน้ารายการ และข้อมูลที่หน้าถ่ายภาพใช้
    st = m.get(f"/api/photos/status?ids={p1}").json()
    assert "รอคิวลำดับที่ 1 จาก 1 ภาพ" in st["photos"][0]["wait"] and "โควตาของวันนี้" in st["photos"][0]["wait"]
    page = m.get(f"/photos/{p1}").text
    assert "รอคิวลำดับที่ 1" in page and "ไม่ต้องส่งภาพซ้ำ" in page
    lst = m.get(f"/photos?round={S['rid']}").text
    assert "ที่ยังรอ AI วิเคราะห์" in lst and "คิวหยุดรอ" in lst and 'href="/admin/queue"' not in lst
    # สิทธิ์: ตัวแทนแผนกและกรรมการทั่วไปเข้าหน้าคิวไม่ได้ ผู้ที่ได้สิทธิ์จัดการรอบสั่งคิวได้
    assert m.get("/admin/queue").status_code == 403 and m.post("/admin/queue/kick").status_code == 403
    assert aud.get("/admin/queue").status_code == 403 and aud.post("/admin/queue/retry-errors").status_code == 403
    assert a.post("/admin/queue/nothing").status_code == 404
    # สั่งคิวจากหน้าเว็บ
    MODE["ai1"] = "ok"
    assert a.post("/admin/queue/retry-errors").status_code == 303 and photo(p2).status == "pending" and photo(p2).error == ""
    assert worker.process_one() is False                              # ผู้ให้บริการยังพักอยู่จนกว่าจะกด เดินคิวเดี๋ยวนี้
    assert a.post("/admin/queue/kick").status_code == 303
    assert drain() == 2 and photo(p1).status == "done" and photo(p2).status == "done"
    r = a.get("/admin/queue")
    assert "คิวว่าง" in r.text and "พร้อมใช้" in r.text and "ไม่มีภาพรอในคิว" in r.text
    keepalive._transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    try:
        assert a.post("/admin/queue/wake-host").status_code == 303
        assert "สำเร็จ" in a.get("/admin/queue").text
    finally:
        keepalive._transport = None
    with dbm.SessionLocal() as s:
        acts = [x.action for x in s.query(dbm.AuditLog).order_by(dbm.AuditLog.id.desc()).limit(12)]
        assert "queue_kick" in acts and "queue_retry_errors" in acts
    # หน้าตั้งค่า: ช่องใหม่ของคิวบันทึกได้ ค่าที่ผิดรูปแบบไม่ถูกเก็บ
    page = a.get("/admin/settings").text
    for name in ("ai_workers", "ai_timeout", "ai2_rpm", "ai2_daily", "keep_awake", "keep_awake_hours", "queue_alert_min"):
        assert f'name="{name}"' in page, name
    cur = settings_store.load()
    form = {"org_name": cur["org_name"], "ai1_type": "gemini", "ai1_model": "gemini-3.8-flash", "ai2_type": "none",
            "ai_rpm": 8, "ai_daily": 0, "ai_max_attempts": 4, "ai_workers": 9, "ai_timeout": 5, "ai2_rpm": 12, "ai2_daily": 300,
            "keep_awake": "hours", "keep_awake_hours": "7:30-17:45", "queue_alert_min": 45, "img_max_side": 1280,
            "img_quality": 78, "storage_budget_mb": 350, "retention_days": 0, "member_see_all": "1", "allow_gallery": "1",
            "allow_free_area": "1", "after_replaces": "1", "public_url": "https://audit5s.example.com"}
    assert a.post("/admin/settings", data=form).status_code == 303
    cur = settings_store.load()
    assert (cur["ai_workers"], cur["ai_timeout"], cur["ai2_rpm"], cur["ai2_daily"], cur["ai_daily"]) == (4, 20, 12, 300, 0)
    assert (cur["keep_awake"], cur["keep_awake_hours"], cur["queue_alert_min"]) == ("hours", "07:30-17:45", 45)
    assert ai.profiles(cur)[0]["timeout"] == 20.0
    form.update(keep_awake="sometimes", keep_awake_hours="เช้าถึงเย็น")
    a.post("/admin/settings", data=form)
    cur = settings_store.load()
    assert cur["keep_awake"] == "hours" and cur["keep_awake_hours"] == ""
    # หน้าภาษาอังกฤษยังแสดงได้
    a.get("/lang/en")
    assert a.get("/admin/queue").status_code == 200
    a.get("/lang/th")


def test_14_admin_is_told_when_photos_wait_too_long():
    fresh(queue_alert_min=30, public_url="https://audit5s.example.com")
    save(_alert_queue_slow="")
    MODE["ai1"] = "429day"
    p1 = shoot("รอนาน")
    drain()
    before = len(alerts())
    assert worker.watch()["alert"] is False                           # เพิ่งรอ ยังไม่ถึงเวลาที่ตั้ง
    edit(p1, queued_at=dbm.now() - timedelta(minutes=47))
    out = worker.watch()
    assert out["alert"] is True
    msg = alerts()[-1]
    assert "มี 1 ภาพรอ AI วิเคราะห์" in msg and "รอมาแล้ว 47 นาที" in msg and "โควตาของวันนี้" in msg
    assert "https://audit5s.example.com/admin/queue" in msg
    assert worker.watch()["alert"] is False and len(alerts()) == before + 1          # ไม่แจ้งซ้ำภายใน 6 ชั่วโมง
    save(queue_alert_min=0, _alert_queue_slow="")
    assert worker.watch()["alert"] is False                           # 0 = ปิดการแจ้ง
    with dbm.SessionLocal() as s:
        info = worker.queue_info(s)
        assert info["pending"] == 1 and info["waited_min"] == 47
        # โควตาของผู้ให้บริการหมด: ไม่รู้ว่าจะกลับมาเมื่อไร จึงไม่เดาเวลาที่จะได้ผล แต่บอกเวลาที่จะลองเรียกใหม่
        assert info["eta_s"] is None and info["retry_s"] is not None and 0 <= info["retry_s"] <= 1800
        w = worker.waits(s, [p1])[p1]
        assert w["eta_s"] is None and w["retry_s"] == info["retry_s"]
        text_ = worker.wait_text(w)
        assert "คาดว่าได้ผล" not in text_ and "ระบบจะลองเรียก AI อีกครั้งในอีก" in text_ and "โควตาของวันนี้" in text_
    MODE["ai1"] = "ok"
    fresh()
    notify.flush(force=True)


def test_15_two_passes_and_checklist_mode_still_score():
    fresh(ai_passes=2)
    two = shoot("สองรอบ")
    assert drain() == 1
    p = photo(two)
    assert p.status == "done" and p.percent == 75.0 and calls(note="สองรอบ") == 2
    with dbm.SessionLocal() as s:
        assert worker.usage_today(s) == 2
    # รอบที่สองล้มเหลวเพราะเรียกถี่: ใช้ผลรอบแรก ภาพไม่ต้องรอ
    seq = iter(["ok", "429min"])
    half = shoot("รอบสองไม่ผ่าน")
    HOOK["fn"] = lambda: None
    real = ai.call

    def flip(cfg, system, user, image=None):
        MODE["ai1"] = next(seq, "ok")
        return real(cfg, system, user, image)
    ai.call = flip
    try:
        assert worker.process_one() is True
    finally:
        ai.call = real
    assert photo(half).status == "done" and calls(note="รอบสองไม่ผ่าน") == 2
    fresh()


def test_16_after_a_pause_only_one_thread_tries_the_provider():
    fresh()
    s = settings_store.load()
    cfg = ai.profiles(s)[0]
    assert providers.begin(cfg) and providers.begin(cfg)              # ผู้ให้บริการปกติ: ทุกเธรดเรียกได้พร้อมกัน
    providers.failure(cfg, ai.AIError("429", kind="rate", retry_after=30, status=429))
    assert providers.begin(cfg) is False and providers.blocked(cfg, s)[0] == "rate"
    CLOCK["off"] += 40                                                # ครบเวลาพัก
    assert providers.blocked(cfg, s) is None
    assert providers.begin(cfg) is True                               # เธรดแรกได้สิทธิ์ลองเรียก
    assert providers.begin(cfg) is False                              # เธรดอื่นรอผลก่อน ไม่ยิงซ้ำพร้อมกัน
    kind, until, reason = providers.blocked(cfg, s)
    assert kind == "trial" and 0 < until - providers.now() <= providers.TRIAL_MAX and reason
    assert providers.plan(s)["ready"] == [] and providers.plan(s)["kind"] == "trial"
    providers.success(cfg)                                            # ลองแล้วผ่าน: ทุกเธรดทำงานต่อ
    assert providers.blocked(cfg, s) is None and providers.begin(cfg) and providers.begin(cfg)
    # ลองแล้วไม่ผ่าน: พักต่อ และสิทธิ์ลองถูกคืน
    providers.failure(cfg, ai.AIError("503", kind="transient", status=503))
    providers.failure(cfg, ai.AIError("503", kind="transient", status=503))
    CLOCK["off"] += 20
    assert providers.begin(cfg) is True and providers.begin(cfg) is False
    providers.failure(cfg, ai.AIError("503", kind="transient", status=503))
    assert providers.blocked(cfg, s)[0] == "transient" and providers.health(cfg).trial_until == 0.0
    # เธรดที่ลองหายไปกลางทาง (ระบบกำลังปิด): คืนสิทธิ์ได้ และสิทธิ์หมดอายุเองถ้าไม่มีใครคืน
    CLOCK["off"] += 400
    assert providers.begin(cfg) is True
    providers.end(cfg)
    assert providers.begin(cfg) is True
    CLOCK["off"] += providers.TRIAL_MAX + 1
    assert providers.blocked(cfg, s) is None and providers.begin(cfg) is True
    # คิวจริง: ภาพที่มาระหว่างการลองเรียกไม่ถูกหยิบ และไม่เสียจำนวนครั้ง
    fresh()
    cfg = ai.profiles(settings_store.load())[0]
    providers.failure(cfg, ai.AIError("429", kind="rate", retry_after=5, status=429))
    CLOCK["off"] += 10
    assert providers.begin(cfg) is True                               # สมมติว่าอีกเธรดกำลังลองอยู่
    waiting = shoot("มาระหว่างลอง")
    assert worker.process_one() is False and worker.state["paused_kind"] == "trial" and calls() == 0
    assert photo(waiting).status == "pending" and photo(waiting).attempts == 0
    providers.success(cfg)
    assert drain() == 1 and photo(waiting).status == "done"
    fresh()


def test_17_three_providers_fail_over_in_order_and_backup_scores_are_labelled():
    fresh(**BACKUP, **THIRD)
    a, m = S["admin"], S["mem"]
    with dbm.SessionLocal() as s:                                       # ไม่ให้ภาพของกรณีก่อนหน้ามาปนในจำนวนที่นับ
        s.query(dbm.Photo).update({"ai_slot": ""}, synchronize_session=False)
        s.commit()
    assert [c["slot"] for c in ai.profiles(settings_store.load())] == ["ai1", "ai2", "ai3"]
    # ตัวหลักใช้ได้: ใช้ตัวหลักเสมอ
    p_main = shoot("สามเจ้า หลัก")
    drain()
    assert (photo(p_main).provider, photo(p_main).ai_slot) == ("gemini", "ai1")
    # ตัวหลักโควตาหมด: ไปตัวที่ 2
    MODE["ai1"] = "429day"
    p_two = shoot("สามเจ้า สอง")
    drain()
    assert (photo(p_two).model, photo(p_two).ai_slot, photo(p_two).attempts) == ("vision-b", "ai2", 0)
    # ตัวที่ 2 ล่มด้วย: ไปตัวที่ 3 ในการทำงานรอบเดียวกัน ภาพไม่ต้องรอ
    MODE["ai2"] = "503"
    p_three = shoot("สามเจ้า สาม")
    assert worker.process_one() is True
    assert (photo(p_three).status, photo(p_three).model, photo(p_three).ai_slot) == ("done", "vision-c", "ai3")
    assert [s_ for s_, n, _t in CALLS if n == "สามเจ้า สาม"] == ["ai2", "ai3"]      # ตัวหลักพักอยู่ จึงไม่ถูกเรียก
    with dbm.SessionLocal() as s:
        assert worker.usage_all(s) == {"ai1": 1, "ai2": 1, "ai3": 1}
        snap = {x["slot"]: x for x in worker.queue_info(s)["providers"]}
        assert [snap[k]["name"] for k in ("ai1", "ai2", "ai3")] == ["AI หลัก", "AI สำรอง 1", "AI สำรอง 2"]
        assert (snap["ai1"]["kind"], snap["ai3"]["ok"], snap["ai3"]["used"]) == ("quota", True, 1)
    # ตัวที่ 3 มีเพดานต่อวันของตัวเอง: ครบแล้วและตัวอื่นยังใช้ไม่ได้ คิวจึงหยุดรอ พร้อมบอกเหตุผล
    save(ai3_daily=1)
    MODE["ai2"] = "401"
    p_wait = shoot("สามเจ้า รอ")
    drain()
    assert photo(p_wait).status == "pending" and photo(p_wait).attempts == 0 and worker.state["paused_reason"]
    with dbm.SessionLocal() as s:
        kinds = {x["slot"]: x["kind"] for x in worker.queue_info(s)["providers"]}
    assert kinds["ai2"] == "config" and kinds["ai3"] == "cap" and kinds["ai1"] in ("quota", "trial")
    # ทุกเจ้ากลับมา: คิวเดินต่อเอง และกลับไปใช้ตัวหลัก
    MODE.update(ai1="ok", ai2="ok")
    assert a.post("/admin/queue/kick").status_code == 303
    drain()
    assert (photo(p_wait).status, photo(p_wait).ai_slot) == ("done", "ai1")
    # ป้ายและตัวกรองของภาพที่ได้คะแนนจาก AI สำรอง
    page = m.get(f"/photos?round={S['rid']}&status=backup").text
    assert f'href="/photos/{p_two}"' in page and f'href="/photos/{p_three}"' in page and f'href="/photos/{p_main}"' not in page
    assert "AI สำรอง" in page
    assert "AI สำรอง 2" in m.get(f"/photos/{p_three}").text and "AI สำรอง 1" in m.get(f"/photos/{p_two}").text
    assert "AI สำรอง" not in m.get(f"/photos/{p_main}").text.split("<main")[1]
    # ผู้ดูแลสั่งให้ AI หลักวิเคราะห์ภาพกลุ่มนั้นใหม่: ไม่แตะภาพที่คนยืนยันหรือปรับผลแล้ว
    q = a.get("/admin/queue").text
    assert "มี 2 ภาพในรอบที่เปิดอยู่ ที่ได้คะแนนจาก AI สำรอง" in q and "ให้ AI หลักวิเคราะห์ใหม่" in q
    edit(p_three, verified_at=dbm.now(), verified_by="กรรมการ")
    assert "มี 1 ภาพในรอบที่เปิดอยู่" in a.get("/admin/queue").text
    MODE["ai1"] = "429day"                                             # ตัวหลักยังใช้ไม่ได้: ไม่ส่ง คะแนนเดิมยังอยู่
    hold = shoot("ทำให้ตัวหลักพัก")
    drain()
    assert photo(hold).ai_slot == "ai2"
    edit(hold, verified_at=dbm.now())
    assert a.post("/admin/queue/rescore-backup").status_code == 303
    assert photo(p_two).status == "done" and photo(p_two).ai_slot == "ai2"
    assert "ยังส่งไม่ได้" in a.get("/admin/queue").text
    MODE["ai1"] = "ok"
    providers.reset()
    assert a.post("/admin/queue/rescore-backup").status_code == 303
    assert photo(p_two).status == "pending" and photo(p_three).status == "done"        # ภาพที่ยืนยันแล้วไม่ถูกส่ง
    drain()
    assert (photo(p_two).status, photo(p_two).ai_slot, photo(p_two).provider) == ("done", "ai1", "gemini")
    assert "ภาพในรอบที่เปิดอยู่ ที่ได้คะแนนจาก AI สำรอง" not in a.get("/admin/queue").text
    assert S["mem"].post("/admin/queue/rescore-backup").status_code == 403
    # เพดานต่อวันของตัวหลักเหลือไม่พอ: ส่งเท่าที่เหลือ ที่เหลือยังคงคะแนนเดิม
    fresh(**BACKUP)
    MODE["ai1"] = "429day"
    ids = [shoot(f"ส่งบางส่วน {i}") for i in range(3)]
    drain()
    assert [photo(i).ai_slot for i in ids] == ["ai2"] * 3
    MODE["ai1"] = "ok"
    providers.reset()
    save(ai_daily=2)
    with dbm.SessionLocal() as s:
        out = worker.rescore_with_main(s)
    assert (out["sent"], out["total"]) == (2, 3) and "เหลือไม่พอ" in out["why"]
    drain()
    assert [photo(i).ai_slot for i in ids] == ["ai1", "ai1", "ai2"]
    # หน้าตั้งค่า: ช่องที่ 3 บันทึกได้ ทดสอบและดึงรายชื่อโมเดลของช่องที่ 3 ได้
    page = a.get("/admin/settings").text
    for want in ('name="ai3_type"', 'name="ai3_model"', 'name="ai3_rpm"', 'name="ai3_daily"', "AI สำรอง 2", "https://api.mistral.ai/v1",
                 "ผู้ให้บริการฟรีที่ใช้กับโปรแกรมนี้ได้"):
        assert want in page, want
    cur = settings_store.load()
    form = {"org_name": cur["org_name"], "ai1_type": "gemini", "ai1_model": "gemini-3.8-flash", "ai2_type": "openai",
            "ai2_base": "https://backup.test/v1", "ai2_model": "vision-b", "ai3_type": "openai", "ai3_base": "https://third.test/v1",
            "ai3_key": "t-key", "ai3_model": "vision-c", "ai_rpm": 10, "ai_daily": 200, "ai2_rpm": 1, "ai2_daily": 30, "ai3_rpm": 4,
            "ai3_daily": 45, "ai_max_attempts": 4, "img_max_side": 1280, "img_quality": 78, "storage_budget_mb": 350,
            "retention_days": 0, "member_see_all": "1", "allow_gallery": "1", "allow_free_area": "1", "after_replaces": "1"}
    assert a.post("/admin/settings", data=form).status_code == 303
    cur = settings_store.load()
    assert (cur["ai3_type"], cur["ai3_key"], cur["ai3_rpm"], cur["ai3_daily"], cur["ai2_rpm"]) == ("openai", "t-key", 4, 45, 1)
    cfgs = {c["slot"]: c for c in ai.profiles(cur)}
    assert providers.rpm_of(cfgs["ai3"], cur) == 4 and providers.cap_of(cfgs["ai3"], cur) == 45 and providers.rpm_of(cfgs["ai2"], cur) == 1
    assert "t-key" not in a.get("/admin/settings").text
    r = a.post("/admin/ai/test", json={"slot": "ai3", "type": "openai", "base": "https://third.test/v1", "model": "vision-c"})
    assert r.json()["ok"] is True, r.text
    with dbm.SessionLocal() as s:
        assert worker.usage_all(s)["ai3"] == 1                          # การทดสอบนับเข้าช่องที่ทดสอบ ไม่ใช่ช่องของ AI หลัก
    fresh()


def test_18_waiting_time_is_given_in_minutes_and_seconds():
    assert [worker.duration_text(x) for x in (0, 7, 59, 60, 61, 150, 3599, 3600, 4260, 7200)] == [
        "0 วินาที", "7 วินาที", "59 วินาที", "1 นาที", "1 นาที 1 วินาที", "2 นาที 30 วินาที", "59 นาที 59 วินาที",
        "1 ชั่วโมง", "1 ชั่วโมง 11 นาที", "2 ชั่วโมง"]
    assert worker.eta_text(95) == "ประมาณ 1 นาที 35 วินาที" and worker.eta_text(None) == ""
    fresh(ai_rpm=6, ai_workers=1)
    worker.state.update(avg_s=12.0, jobs=5)
    a, m = S["admin"], S["mem"]
    ids = [shoot(f"เวลารอ {i}") for i in range(4)]
    # คิวเดินปกติ: ภาพแรก = เวลาวิเคราะห์หนึ่งภาพ ภาพถัดไปบวกทีละช่วงของอัตราที่ตั้ง (6 ครั้งต่อนาที = 10 วินาที)
    with dbm.SessionLocal() as s:
        w = worker.waits(s, ids)
    assert [w[i]["position"] for i in ids] == [1, 2, 3, 4] and all(w[i]["retry_s"] is None for i in ids)
    assert [w[i]["eta_s"] for i in ids] == [12, 24, 36, 48]
    parts = worker.wait_parts(w[ids[2]])
    assert parts["text"] == "รอคิวลำดับที่ 3 จาก 4 ภาพ คาดว่าได้ผลในอีกประมาณ 36 วินาที" and parts["secs"] == 36
    st = {x["id"]: x for x in m.get("/api/photos/status?ids=" + ",".join(map(str, ids))).json()["photos"]}
    last = st[ids[3]]
    assert last["wait"] == "รอคิวลำดับที่ 4 จาก 4 ภาพ คาดว่าได้ผลในอีกประมาณ 48 วินาที"
    assert (last["wait_head"], last["wait_lead"], last["wait_secs"], last["wait_tail"]) == (
        "รอคิวลำดับที่ 4 จาก 4 ภาพ", "คาดว่าได้ผลในอีกประมาณ", 48, "")
    page = m.get(f"/photos/{ids[1]}").text
    assert 'data-countdown="24">24 วินาที</b>' in page and "รอคิวลำดับที่ 2 จาก 4 ภาพ" in page
    lst = m.get(f"/photos?round={S['rid']}").text
    assert "มี 4 ภาพในหน้านี้ที่ยังรอ AI วิเคราะห์ คาดว่าได้ผลครบในอีกประมาณ" in lst and 'data-countdown="48">48 วินาที</b>' in lst
    q = a.get("/admin/queue").text
    assert "คาดว่าหมดในอีกประมาณ" in q and 'data-countdown="48">48 วินาที</span>' in q
    assert a.get("/admin/queue.json").json()["eta_s"] == 48
    # ภาพที่กำลังวิเคราะห์: เหลือเวลาตามที่ใช้ไปแล้ว
    edit(ids[0], status="processing", started_at=dbm.now() - timedelta(seconds=5))
    with dbm.SessionLocal() as s:
        w = worker.waits(s, ids)
    assert 6 <= w[ids[0]]["eta_s"] <= 7 and 18 <= w[ids[1]]["eta_s"] <= 19          # ภาพถัดไป: รอช่วงห่างที่เหลือ 7 วินาที แล้ววิเคราะห์ 12 วินาที
    assert 30 <= w[ids[2]]["eta_s"] <= 31 and 42 <= w[ids[3]]["eta_s"] <= 43
    assert worker.wait_parts(w[ids[0]])["text"].startswith("AI กำลังวิเคราะห์ภาพนี้ คาดว่าได้ผลในอีกประมาณ")
    edit(ids[0], status="pending", started_at=None)
    # พักเพราะเรียกถี่: รู้เวลาเดินต่อแน่ จึงรวมเวลาพักเข้าไปด้วย เป็นนาทีและวินาที
    MODE["ai1"] = "429min"
    assert worker.process_one() is True and worker.process_one() is False
    with dbm.SessionLocal() as s:
        w = worker.waits(s, ids)
    assert 44 <= w[ids[0]]["eta_s"] <= 49 and 80 <= w[ids[3]]["eta_s"] <= 85 and w[ids[3]]["retry_s"] is not None
    text_ = worker.wait_text(w[ids[3]])
    assert "คาดว่าได้ผลในอีกประมาณ 1 นาที 2" in text_ and "เรียกถี่" in text_
    # ครบเพดานที่ตั้งเอง: รู้เวลาที่โควตารอบใหม่เริ่ม จึงบอกเป็นชั่วโมงและนาที
    fresh(ai_daily=1, ai_rpm=6, ai_workers=1)
    worker.state.update(avg_s=12.0, jobs=5)
    one, two = shoot("เพดาน ก"), shoot("เพดาน ข")
    assert drain() == 1 and worker.process_one() is False and worker.state["paused_kind"] == "cap"
    with dbm.SessionLocal() as s:
        w = worker.waits(s, [two])[two]
    left = providers.quota_reset_at() - providers.now()
    assert abs(w["eta_s"] - (left + 12)) < 5 and worker.duration_text(w["eta_s"]) in worker.wait_text(w)
    # ยังไม่ได้ตั้งค่า AI: ไม่มีเวลาให้บอก บอกเหตุผลแทน
    fresh(ai1_type="none")
    lone = shoot("ยังไม่มี AI")
    with dbm.SessionLocal() as s:
        w = worker.waits(s, [lone])[lone]
    assert w["eta_s"] is None and w["retry_s"] is None
    assert worker.wait_text(w) == "รอคิวลำดับที่ 1 จาก 1 ภาพ (ยังไม่ได้ตั้งค่า AI)"
    assert "ยังไม่ได้ตั้งค่า AI" in m.get(f"/photos?round={S['rid']}").text
    fresh()
    drain()


def test_19_notifications_survive_data_deleted_while_sending():
    fresh()
    notify.flush(force=True)
    pid = shoot("แจ้งเตือนระหว่างลบ")
    drain()
    with dbm.SessionLocal() as s:
        for i in range(3):
            s.add(dbm.NotifyEvent(kind="system", payload={"text": f"เรื่องที่ {i}"}))
        s.commit()
        assert s.query(dbm.NotifyEvent).filter(dbm.NotifyEvent.done.is_(False)).count() >= 3
    real = notify.build

    def wipe_then_build(db, kind, rid, did, events):
        with dbm.SessionLocal() as other:                              # ผู้ดูแลล้างข้อมูลทดสอบพอดีกับที่เธรดแจ้งเตือนกำลังส่ง
            other.query(dbm.NotifyEvent).delete(synchronize_session=False)
            other.commit()
        return real(db, kind, rid, did, events)
    notify.build = wipe_then_build
    try:
        assert notify.flush(force=True) == 0                           # รุ่นก่อนโยน StaleDataError ที่จุดนี้
    finally:
        notify.build = real
    with dbm.SessionLocal() as s:
        assert s.query(dbm.NotifyEvent).count() == 0
    assert photo(pid).status == "done"


def test_20_long_waits_left_by_an_older_version_are_released_at_start():
    fresh()
    old, new_, near = shoot("พักไว้ 60 นาทีโดยรุ่นเดิม"), shoot("ยังไม่เคยล้มเหลว"), shoot("รอ 30 วินาทีตามปกติ")
    edit(old, next_try_at=dbm.now() + timedelta(minutes=58), attempts=1, error="จะลองใหม่อัตโนมัติ: 429")
    edit(near, next_try_at=dbm.now() + timedelta(seconds=30), attempts=1)
    assert worker.release_long_waits() == 1
    assert photo(old).next_try_at is None and photo(old).attempts == 1 and photo(near).next_try_at is not None
    assert worker.release_long_waits() == 0
    assert drain() == 2 and photo(old).status == "done" and photo(new_).status == "done" and photo(near).status == "pending"
    fresh()


def test_21_many_threads_raise_one_alert_and_settings_saves_do_not_collide():
    fresh()
    save(_alert_ai_quota="")
    with dbm.SessionLocal() as s:
        s.query(dbm.Setting).filter(dbm.Setting.key.in_(["_alert_ai_quota", "_alert_race_test"])).delete(synchronize_session=False)
        s.commit()
    settings_store.load(force=True)
    before = len(alerts())
    errors, results = [], []
    gate = threading.Barrier(6)

    def raise_alert():
        try:
            gate.wait(10)
            with dbm.SessionLocal() as s:
                results.append(notify.alert_admin(s, "race_test", "คิววิเคราะห์ภาพหยุดรอ ทดสอบแจ้งพร้อมกัน", 6))
        except Exception as e:                                         # pragma: no cover
            errors.append(repr(e))
    threads = [threading.Thread(target=raise_alert) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert sorted(results) == [False] * 5 + [True] and len(alerts()) == before + 1        # แจ้งครั้งเดียว ไม่มีเธรดใดล้ม
    # หลายเธรดสร้างค่าตั้งคีย์ใหม่คีย์เดียวกันพร้อมกัน: ไม่มีข้อผิดพลาด และค่าสุดท้ายเป็นค่าของเธรดใดเธรดหนึ่ง
    gate2 = threading.Barrier(6)

    def write(i):
        try:
            gate2.wait(10)
            with dbm.SessionLocal() as s:
                settings_store.save(s, {"_race_key": i})
        except Exception as e:                                         # pragma: no cover
            errors.append(repr(e))
    threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert settings_store.load(force=True)["_race_key"] in range(6)
    # คิวจริงสองเธรดพบว่าผู้ให้บริการพักพร้อมกัน: แจ้งผู้ดูแลครั้งเดียว และไม่มีเธรดใดล้ม
    MODE["ai1"] = "429day"
    shoot("แจ้งพร้อมกัน 1")
    shoot("แจ้งพร้อมกัน 2")
    worker.process_one()
    save(_alert_ai_quota="")
    n0 = sum("คิววิเคราะห์ภาพหยุดรอ" in x and "โควตาของวันนี้" in x for x in alerts())
    gate3 = threading.Barrier(4)

    def blocked_run():
        try:
            gate3.wait(10)
            worker.run_one(pace=False)
        except Exception as e:                                         # pragma: no cover
            errors.append(repr(e))
    threads = [threading.Thread(target=blocked_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert sum("คิววิเคราะห์ภาพหยุดรอ" in x and "โควตาของวันนี้" in x for x in alerts()) == n0 + 1
    fresh()

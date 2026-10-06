"""ชุดทดสอบของรุ่น 1.3: โหมดรายการตรวจ (rule engine) ภาพหลักฐานพร้อมกรอบ คะแนนสร้างนิสัยที่ระบบคำนวณ โซนของกล้องติดตาย
งานแก้ไข ความแม่นของ AI ภาพรวม และการส่งออกชุดข้อมูล
"""
import base64
import csv
import io
import json
import os
import random
import re
import tempfile
import zipfile
from datetime import timedelta

import httpx
import pytest
from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v13_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import actions, ai, cameras, db as dbm, notify, rules, scoring, settings_store, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
AI = {"status": {}, "queue": [], "drop": None, "bad": None, "image_ok": True, "prompts": [], "images": []}
OUT = []
S = {}


def jpeg(seed: int, size=(1000, 750)) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (rnd.randrange(200), rnd.randrange(200), 40 + rnd.randrange(60)))
    for _ in range(25):
        x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
        im.paste((rnd.randrange(256), rnd.randrange(200), 30), (x, y, x + 70, y + 50))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ai_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    parts = body["contents"][0]["parts"]
    prompt = parts[-1]["text"]
    AI["prompts"].append(prompt)
    AI["images"].append(base64.b64decode(parts[0]["inline_data"]["data"]) if "inline_data" in parts[0] else None)
    if "รายการตรวจ (แต่ละข้อคือสภาพที่ถูกต้อง)" in prompt:
        codes = re.search(r"ต้องมีครบทุกข้อตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        status = AI["queue"].pop(0) if AI["queue"] else AI["status"]
        checks = []
        for c in codes:
            if c == AI["drop"]:
                continue
            st = "wrong" if c == AI["bad"] else status.get(c, "ok")
            checks.append(dict(code=c, status=st, evidence=f"เห็นที่ {c}", action=f"แก้ {c}" if st in ("minor", "major") else "",
                               box=[100, 200, 400, 600] if st in ("minor", "major") else None))
        out = dict(image_ok=AI["image_ok"], image_issue="" if AI["image_ok"] else "มีคนบังพื้นที่", scene="-", checks=checks,
                   summary="สรุป", top_actions=["เคลียร์ทางเดิน"])
    else:
        codes = re.search(r"ต้องมีครบทุกเกณฑ์ตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        out = dict(image_ok=True, scene="-", summary="สรุป", top_actions=[],
                   criteria=[dict(code=c, na=False, level=3, reason="เหตุผล", findings=[], recommendations=[]) for c in codes])
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(out, ensure_ascii=False)}]}}]})


def out_handler(request: httpx.Request) -> httpx.Response:
    OUT.append(json.loads(request.content)["content"])
    return httpx.Response(204)


def cam_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=jpeg(random.randrange(10 ** 6) + 70000, (1280, 960)), headers={"content-type": "image/jpeg"})


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


def upload(c, rid, did, seed, area, area_type="สายการผลิต", expect=200, **data):
    r = c.post("/api/photos", data=dict(round_id=rid, department_id=did, area_name=area, area_type=area_type, **data),
               files={"file": ("p.jpg", jpeg(seed), "image/jpeg")})
    assert r.status_code == expect, r.text
    return r.json().get("id")


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


def checks_of(pid):
    return {x["code"]: x for x in photo(pid).analysis["checks"]}


def crit_of(pid):
    return {c["code"]: c for c in photo(pid).analysis["criteria"]}


def new_round(a, name, mode="checklist"):
    with dbm.SessionLocal() as s:
        for r in s.query(dbm.Round).filter_by(status="open"):
            r.status = "closed"
        s.commit()
    assert a.post("/admin/rounds/save", data={"name": name, "min_photos": 1, "mode": mode}).status_code == 303
    with dbm.SessionLocal() as s:
        return s.query(dbm.Round).filter_by(name=name).one().id


def test_01_rules_are_factory_controlled_and_versioned():
    a = admin()
    S["admin"] = a
    for code, name in (("X1", "SECRETDEPT หนึ่ง"), ("X2", "SECRETDEPT สอง")):
        a.post("/admin/departments/save", data={"code": code, "name": name})
    with dbm.SessionLocal() as s:
        S["d"] = {d.code: d.id for d in s.query(dbm.Department).all()}
        s.query(dbm.Channel).delete()
        s.query(dbm.Action).delete()
        s.commit()
    notify.refresh_channels()
    save(**{k: "" for k in settings_store.load() if k.startswith(("_alert_", "_stamp_"))} or {"_alert_x": ""})
    save(ai1_type="gemini", ai1_key="g-key", ai1_model="gemini-flash", ai2_type="none", ai_daily=100000, ai_passes=1,
         ai_max_attempts=3, scoring_mode="checklist", auto_actions=True, action_due_days=7, action_due_days_major=3,
         verify_required=False, require_coverage=False, after_replaces=True, allow_free_area=True, max_photos_per_dept=0,
         area_types=["สายการผลิต", "คลัง"], retention_days=0, storage_budget_mb=350, ranking_visibility="always",
         member_see_all=True, public_url="https://5s.example.test", ai_extra="")
    assert a.post("/admin/criteria/reset").status_code == 303 and a.post("/admin/checkpoints/reset").status_code == 303
    rev0 = settings_store.load()["_rule_rev"]
    page = a.get("/admin/checkpoints")
    assert page.status_code == 200 and "C04" in page.text and "ระบบคำนวณเอง" in page.text      # S5 เป็นหมวดที่ระบบคำนวณ
    base = dict(crit_code="S2", minor_hint="ซ้อน 4 ชั้น", major_hint="ซ้อนเกิน 4 ชั้น", points=5, minor_points=3, sort_order=200, allow_na="1")
    assert a.post("/admin/checkpoints/save", data=dict(base, code="C20", text="พาเลทวางซ้อนไม่เกิน 3 ชั้น", area_types=["คลัง"])).status_code == 303
    a.post("/admin/checkpoints/save", data=dict(base, code="Z99", text="รหัส Z สงวนไว้ให้โซน"))
    a.post("/admin/checkpoints/save", data=dict(base, code="C20", text="รหัสซ้ำ"))
    a.post("/admin/checkpoints/save", data=dict(base, code="C21", text="แต้มเล็กน้อยมากกว่าแต้มเต็ม", minor_points=9))
    with dbm.SessionLocal() as s:
        codes = [k.code for k in s.query(dbm.Checkpoint).order_by(dbm.Checkpoint.code)]
        assert len(codes) == 14 and "C20" in codes and "Z99" not in codes and "C21" not in codes
        assert s.query(dbm.Checkpoint).filter_by(code="C20").one().area_types == ["คลัง"]
    rev1 = settings_store.load()["_rule_rev"]
    assert rev1 == rev0 + 1                                              # แก้เกณฑ์ 1 ครั้งที่สำเร็จ = ฉบับใหม่ 1 ฉบับ
    assert f"ฉบับที่ {rev1}: C20" in a.get("/admin/criteria").text          # ประวัติการแก้เกณฑ์
    S["r1"] = new_round(a, "รอบ 1.3 A")
    with dbm.SessionLocal() as s:
        r = s.get(dbm.Round, S["r1"])
        assert (r.mode, r.rule_rev, len(r.checklist)) == ("checklist", rev1, 14)
        assert [c["kind"] for c in r.rubric] == ["ai", "ai", "ai", "ai", "sustain"]
        S["codes"] = [c["code"] for c in r.rubric]
    assert "โหมดรายการตรวจ" in a.get("/admin/rounds").text and f"เกณฑ์ฉบับที่ {rev1}" in a.get("/admin/rounds").text
    for u, role, dept, perms in (("x_mem", "member", S["d"]["X1"], {}), ("x_m2", "member", S["d"]["X2"], {}),
                                 ("x_aud", "auditor", "", {"perm_score": "1"})):
        a.post("/admin/users/save", data=dict(username=u, full_name=u, role=role, department_id=dept, password="Start1234", **perms))
        S[u] = client(u, "Start1234", "Member5S99x")
    assert S["x_mem"].get("/admin/checkpoints").status_code == 403


def test_02_ai_reports_conditions_and_rule_engine_scores():
    d, m, r1 = S["d"], S["x_mem"], S["r1"]
    AI["prompts"].clear()
    AI["status"] = {"C04": "major", "C08": "minor", "C06": "na"}
    p1 = upload(m, r1, d["X1"], 1, "ไลน์เชื่อม A")
    drain()
    prompt = AI["prompts"][0]
    assert "[C04] ทางเดินโล่ง" in prompt and "minor เมื่อ:" in prompt and "C20" not in prompt     # C20 ใช้เฉพาะประเภท คลัง
    assert "SECRETDEPT" not in prompt and "S5" not in prompt and "คุณไม่ได้เป็นผู้ให้คะแนน" in ai.CHECK_SYSTEM
    p = photo(p1)
    k, c = checks_of(p1), crit_of(p1)
    assert p.status == "done" and p.analysis["mode"] == "checklist" and len(k) == 13
    assert (k["C04"]["status"], k["C04"]["points"], k["C08"]["points"], k["C06"]["status"], k["C01"]["points"]) == ("major", 0.0, 3.0, "na", 5.0)
    assert k["C04"]["box"] == [200, 100, 600, 400] and k["C01"]["box"] is None                  # [ymin,xmin,ymax,xmax] -> [x1,y1,x2,y2]
    # S1 3/3 ข้อ = 20, S2 (0+5+5)/15 x 20 = 13.33 (C06 มองไม่เห็น ไม่นับ), S3 (3+5+5)/15 x 20 = 17.33, S4 = 20, S5 รอบแรกไม่คิด
    assert (c["S1"]["score"], c["S2"]["score"], c["S3"]["score"], c["S4"]["score"]) == (20.0, 13.33, 17.33, 20.0)
    assert c["S5"]["na"] is True and c["S5"]["computed"] is True and "รอบแรก" in c["S5"]["reason"]
    assert (p.score, p.max_score) == (70.66, 80.0) and abs(p.percent - 88.33) < 0.02
    assert (c["S2"]["n_ok"], c["S2"]["n_major"], c["S3"]["n_minor"]) == (2, 1, 1)
    page = S["admin"].get(f"/photos/{p1}").text
    assert 'class="fbox fbox-major" style="left:20.0%;top:10.0%;width:40.0%;height:30.0%"' in page       # กรอบชี้จุดบนภาพหลักฐาน
    assert "บกพร่องมาก" in page and "เห็นที่ C04" in page and "ควรทำ: แก้ C04" in page and "ระบบคำนวณ" in page
    # ประเภทพื้นที่ คลัง ได้ข้อ C20 เพิ่ม
    AI["status"] = {"C01": "minor"}
    p2 = upload(m, r1, d["X1"], 2, "ชั้นวางคลัง B", area_type="คลัง")
    drain()
    assert len(checks_of(p2)) == 14 and "C20" in checks_of(p2) and "[C20] พาเลทวางซ้อนไม่เกิน 3 ชั้น" in AI["prompts"][-1]
    # คำตอบขาดข้อ / สถานะไม่ถูกต้อง -> ไม่รับ ลองใหม่
    AI["drop"] = "C13"
    p3 = upload(m, r1, d["X1"], 3, "ทดสอบคำตอบไม่ครบ")
    worker.process_one()
    assert photo(p3).status == "pending" and "ขาดข้อ C13" in photo(p3).error
    AI["drop"], AI["bad"] = None, "C02"
    with dbm.SessionLocal() as s:
        s.get(dbm.Photo, p3).next_try_at = None
        s.commit()
    worker.process_one()
    assert photo(p3).status == "pending" and "สถานะของข้อ C02" in photo(p3).error and photo(p3).attempts == 2
    AI["bad"], AI["image_ok"] = None, False
    with dbm.SessionLocal() as s:
        s.get(dbm.Photo, p3).next_try_at = None
        s.commit()
    drain()
    assert photo(p3).status == "rejected" and "มีคนบังพื้นที่" in photo(p3).analysis["image_issue"]
    AI["image_ok"] = True
    # ทุกหน้าที่แสดงผลรายข้อ
    a = S["admin"]
    for url in (f"/photos/{p2}", f"/verify?round={r1}", f"/rounds/{r1}/dept/{d['X1']}", f"/rounds/{r1}/report", f"/ranking?round={r1}"):
        r = a.get(url)
        assert r.status_code == 200 and "Traceback" not in r.text, url
    assert "fbox" in a.get(f"/rounds/{r1}/report").text and "AI ไม่ได้ให้คะแนนเอง" in a.get(f"/ranking?round={r1}").text
    assert "ไม่ผ่าน 2 ข้อ" in a.get(f"/verify?round={r1}").text
    S.update(p1=p1, p2=p2, p3=p3)


def test_03_confirmed_findings_become_actions_with_owner_and_due_date():
    a, d, m, aud = S["admin"], S["d"], S["x_mem"], S["x_aud"]
    a.post("/admin/notifications/save", data={"name": "Discord X1", "kind": "discord", "department_id": d["X1"], "ev_action": "1",
                                              "cfg_discord_webhook_url": "https://discord.com/api/webhooks/3/x"})
    OUT.clear()
    r = aud.post(f"/admin/photos/{S['p1']}/verify")
    assert r.status_code == 303
    today = actions.today()
    with dbm.SessionLocal() as s:
        acts = {x.check_code: x for x in s.query(dbm.Action).filter_by(photo_id=S["p1"])}
        assert set(acts) == {"C04", "C08"}
        assert (acts["C04"].severity, acts["C04"].due_date, acts["C04"].title) == ("major", today + timedelta(days=3), "แก้ C04")
        assert (acts["C08"].severity, acts["C08"].due_date, acts["C08"].area_name) == ("minor", today + timedelta(days=7), "ไลน์เชื่อม A")
        S["act_c04"], S["act_c08"] = acts["C04"].id, acts["C08"].id
    aud.post(f"/admin/photos/{S['p1']}/verify")                                   # ยืนยันซ้ำ ไม่สร้างงานซ้ำ
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Action).filter_by(photo_id=S["p1"]).count() == 2
    notify.flush(force=True)
    assert len(OUT) == 1 and "งานแก้ไขของ SECRETDEPT หนึ่ง" in OUT[0] and "มีงานแก้ไขใหม่ 2 งาน" in OUT[0] and "/actions?dept=" in OUT[0]
    page = m.get("/actions")
    assert page.status_code == 200 and "แก้ C04" in page.text and "บกพร่องมาก" in page.text
    assert "แก้ C04" in a.get(f"/photos/{S['p1']}").text
    # แผนกเจ้าของงานกำหนดผู้รับผิดชอบและปิดงานได้ แผนกอื่นทำไม่ได้
    due = (today + timedelta(days=2)).isoformat()
    assert S["x_m2"].post("/actions/save", data={"id": S["act_c08"], "title": "x", "pic": "ผม"}).status_code == 403
    assert m.post("/actions/save", data={"id": S["act_c08"], "title": "กวาดเศษใต้โต๊ะเชื่อม", "pic": "สมชาย", "due_date": due}).status_code == 303
    assert m.post(f"/actions/{S['act_c08']}/close", data={"note": ""}).status_code == 303
    with dbm.SessionLocal() as s:
        x = s.get(dbm.Action, S["act_c08"])
        assert (x.pic, x.due_date.isoformat(), x.status, x.title) == ("สมชาย", due, "open", "กวาดเศษใต้โต๊ะเชื่อม")
    assert m.post(f"/actions/{S['act_c08']}/close", data={"note": "กวาดและตั้งเวรทำความสะอาดแล้ว"}).status_code == 303
    assert m.post(f"/actions/{S['act_c08']}/reopen").status_code == 403           # เปิดงานที่ปิดแล้วต้องมีสิทธิ์ยืนยันผล
    with dbm.SessionLocal() as s:
        x = s.get(dbm.Action, S["act_c08"])
        assert (x.status, x.closed_by) == ("done", "x_mem") and x.closed_at is not None
    assert "ไม่มีงานแก้ไข" not in m.get("/actions?status=done").text and "กวาดเศษใต้โต๊ะเชื่อม" in m.get("/actions?status=done").text
    assert S["x_m2"].get("/actions").status_code == 200 and "แก้ C04" in S["x_m2"].get("/actions").text      # ดูได้ (ตั้งค่าให้ดูทุกแผนก)
    # เพิ่มงานเองจากหน้าภาพ
    assert m.post("/actions/save", data={"photo_id": S["p1"], "title": "ทำป้ายชี้บ่งตู้เชื่อม", "pic": "วิชัย", "due_date": due}).status_code == 303
    rows = list(csv.reader(io.StringIO(a.get("/actions.csv").content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["เลขที่งาน", "แผนก", "จุดตรวจ", "งานแก้ไข"] and len(rows) == 4
    assert m.get("/actions.csv").status_code == 403


def test_04_supervisor_corrections_are_kept_and_measured():
    a, aud, m = S["admin"], S["x_aud"], S["x_mem"]
    p2 = S["p2"]
    before = checks_of(p2)
    assert before["C01"]["status"] == "minor"
    form = {f"check_{c}": "ok" for c in before}
    form.update(check_C05="major", note="C01 ที่ AI ชี้คือถังที่ใช้งานอยู่ ส่วน C05 มีกล่องวางนอกเส้น")
    assert m.post(f"/admin/photos/{p2}/override", data=form).status_code == 403
    assert aud.post(f"/admin/photos/{p2}/override", data=dict(form, check_C02="")).status_code == 303
    assert photo(p2).overridden is False                                          # เลือกไม่ครบ ไม่บันทึก
    assert aud.post(f"/admin/photos/{p2}/override", data=form).status_code == 303
    k, p = checks_of(p2), photo(p2)
    assert (k["C01"]["status"], k["C01"]["ai_status"], k["C01"]["changed"]) == ("ok", "minor", True)       # AI แจ้งเกิน
    assert (k["C05"]["status"], k["C05"]["ai_status"], k["C05"]["changed"]) == ("major", "ok", True)       # AI มองข้าม
    assert k["C09"]["changed"] is False and p.overridden and p.verified_by == "x_aud"
    c = crit_of(p2)
    assert c["S1"]["score"] == 20.0 and c["S2"]["score"] == 16.0                  # S2 ในคลังมี 5 ข้อ: (0+5+5+5+5)/25 x 20
    assert p.analysis["ai_original"]["percent"] is not None
    with dbm.SessionLocal() as s:
        acts = [x.check_code for x in s.query(dbm.Action).filter_by(photo_id=p2)]
        assert acts == ["C05"]                                                    # งานแก้ไขมาจากผลที่คนยืนยัน ไม่ใช่ผลเดิมของ AI
        assert "วัสดุและชิ้นงานอยู่ภายในเส้น" in s.query(dbm.Action).filter_by(photo_id=p2).one().title   # AI ไม่ได้เสนอวิธีแก้ ใช้ข้อความของข้อ
    page = a.get(f"/photos/{p2}").text
    assert "กรรมการแก้จาก บกพร่องเล็กน้อย" in page and "กรรมการปรับผลของภาพนี้" in page
    q = a.get("/admin/quality")
    assert q.status_code == 200
    with dbm.SessionLocal() as s:
        stats = scoring.ai_quality(s, S["r1"])
    assert (stats["photos"], stats["total"]["false_alarm"], stats["total"]["miss"]) == (2, 1, 1)
    assert stats["total"]["n"] == 27 and stats["total"]["agree"] == 25 and stats["total"]["pct"] == 92.6   # 13 + 14 ข้อ
    worst = stats["rows"][0]["code"]
    assert worst in ("C01", "C05") and "92.6%" in q.text
    assert m.get("/admin/quality").status_code == 403


def test_05_sustain_score_recurrence_and_auto_close():
    a, d, m, aud = S["admin"], S["d"], S["x_mem"], S["x_aud"]
    with dbm.SessionLocal() as s:                       # งาน C04 ของรอบก่อนเกินกำหนดแล้ว
        s.get(dbm.Action, S["act_c04"]).due_date = actions.today() - timedelta(days=2)
        s.commit()
    r2 = new_round(a, "รอบ 1.3 B")
    AI["status"] = {"C04": "major"}                      # C04 ยังไม่แก้, C08 แก้แล้ว
    p = upload(m, r2, d["X1"], 20, "ไลน์เชื่อม A")
    drain()
    k, c = checks_of(p), crit_of(p)
    assert k["C04"].get("repeat") is True and not k["C08"].get("repeat")
    # รอบก่อนพบ 2 ข้อ (C04, C08) แก้ได้ 1 พบซ้ำ 1 -> 50% หักงานเกินกำหนด 1 งาน 10% -> 40% ของ 20 = 8
    assert (c["S5"]["na"], c["S5"]["score"], c["S5"]["repeats"], c["S5"]["overdue"]) == (False, 8.0, 1, 1)
    assert "พบซ้ำ 1 ข้อ" in c["S5"]["reason"] and "พบซ้ำจากรอบก่อน: ทางเดินโล่ง" in c["S5"]["findings"][0]
    assert photo(p).max_score == 100.0                   # รอบนี้หมวดสร้างนิสัยถูกนับแล้ว
    page = a.get(f"/photos/{p}").text
    assert "พบซ้ำจากรอบก่อน" in page and "งานแก้ไขของจุดนี้เกินกำหนด 1 งาน" in page
    rep = list(csv.reader(io.StringIO(a.get(f"/rounds/{r2}/export/checks.csv").content.decode("utf-8-sig"))))
    assert any(x[0] == str(p) and x[6] == "C04" and x[rep[0].index("พบซ้ำจากรอบก่อน")] == "ใช่" for x in rep[1:])
    dash = a.get(f"/dashboard?round={r2}")
    assert dash.status_code == 200 and "ข้อที่พบซ้ำจากรอบก่อน" in dash.text and "ข้อที่ไม่ผ่านบ่อยที่สุด" in dash.text
    with dbm.SessionLocal() as s:
        data = scoring.dashboard(s, s.get(dbm.Round, r2))
    assert data["pareto"][0]["code"] == "C04" and data["pareto"][0]["major"] == 1 and data["actions"]["overdue"] == 1
    assert len(data["repeats"]) == 1 and data["checklist"] is True
    # แจ้งแผนกเรื่องงานเกินกำหนด วันละครั้ง
    notify.flush(force=True)
    OUT.clear()
    assert worker.housekeeping()["actions"] == 1
    notify.flush(force=True)
    assert len(OUT) == 1 and "งานแก้ไขเกินกำหนด 1 งาน" in OUT[0] and "เกินกำหนด 2 วัน" in OUT[0]
    assert worker.housekeeping()["actions"] == 0
    # ภาพหลังแก้ไขผ่าน -> ระบบปิดงานของภาพเดิมให้เอง และคะแนนสร้างนิสัยของภาพใหม่ดีขึ้น
    aud.post(f"/admin/photos/{p}/verify")
    with dbm.SessionLocal() as s:
        act = s.query(dbm.Action).filter_by(photo_id=p, check_code="C04").one()
        assert act.status == "open"
        s.get(dbm.Action, S["act_c04"]).status = "done"   # ปิดงานเก่าที่เกินกำหนด
        s.commit()
        act_id = act.id
    AI["status"] = {}
    fixed = upload(m, r2, d["X1"], 21, "ไลน์เชื่อม A", after_of=p)
    drain()
    with dbm.SessionLocal() as s:
        act = s.get(dbm.Action, act_id)
        assert (act.status, act.closed_by, act.after_photo_id) == ("done", "ระบบ", fixed) and str(fixed) in act.close_note
    assert crit_of(fixed)["S5"]["score"] == 20.0 and photo(fixed).percent == 100.0
    assert "ดูภาพหลังแก้ไข" in m.get("/actions?status=done").text
    S.update(r2=r2, p_b=p, p_fixed=fixed)


def test_06_zone_logic_for_fixed_cameras():
    a, d, m, r2 = S["admin"], S["d"], S["x_mem"], S["r2"]
    a.post("/admin/areas/save", data=dict(department_id=d["X1"], name="ทางเดินหลัก", area_type="สายการผลิต", required="1", active="1"))
    with dbm.SessionLocal() as s:
        aid = s.query(dbm.AuditArea).filter_by(name="ทางเดินหลัก").one().id
    assert "ยังไม่มีภาพของจุดนี้ให้วาดกรอบ" in a.get(f"/admin/areas/{aid}/zones").text
    a.post("/admin/cameras/save", data=dict(name="กล้องทางเดิน", department_id=d["X1"], area_name="ทางเดินหลัก", mode="direct",
                                            source="snapshot", url="http://10.2.2.2/s.jpg"))
    with dbm.SessionLocal() as s:
        cid = s.query(dbm.Camera).filter_by(name="กล้องทางเดิน").one().id
    cam1 = a.post(f"/api/cameras/{cid}/capture", data={"round_id": r2}).json()["id"]
    drain()
    page = a.get(f"/admin/areas/{aid}/zones").text
    assert 'id="zone-stage"' in page and f"/photos/{cam1}/image" in page and "ไม่ได้มาจากกล้องติดตาย" not in page
    zone = dict(text="ทางเดิน ต้องไม่มีสิ่งของวาง", minor_hint="ล้ำเข้ามา 1 จุด", major_hint="มีของวางในกรอบ", crit_code="S2",
                points=10, minor_points=5, x1=100, y1=500, x2=900, y2=950)
    assert a.post(f"/admin/areas/{aid}/zones/save", data=dict(zone, x2=110)).status_code == 303          # กรอบเล็กเกินไป
    assert a.post(f"/admin/areas/{aid}/zones/save", data=zone).status_code == 303
    with dbm.SessionLocal() as s:
        z = s.query(dbm.Checkpoint).filter_by(area_id=aid).one()
        assert z.code == f"Z{z.id}" and z.zone == [100, 500, 900, 950] and z.allow_na is False
        zcode = z.code
    assert S["x_mem"].get(f"/admin/areas/{aid}/zones").status_code == 403
    # รอบที่เปิดอยู่ยังใช้รายการชุดเดิม จนกว่าจะกด ใช้เกณฑ์ล่าสุดกับรอบนี้
    with dbm.SessionLocal() as s:
        assert zcode not in [k["code"] for k in s.get(dbm.Round, r2).checklist]
    phone = upload(m, r2, d["X1"], 30, "", area_id=aid)                           # ภาพจากมือถือของจุดเดียวกัน
    assert a.post(f"/admin/rounds/{r2}/sync-rubric").status_code == 303
    with dbm.SessionLocal() as s:
        r = s.get(dbm.Round, r2)
        assert zcode in [k["code"] for k in r.checklist] and r.rule_rev == settings_store.load()["_rule_rev"]
    AI["prompts"].clear()
    AI["images"].clear()
    AI["status"] = {zcode: "major"}
    drain()
    k = checks_of(cam1)
    assert k[zcode]["status"] == "major" and k[zcode]["zone"] == [100, 500, 900, 950] and k[zcode]["points"] == 0.0
    # S2: 4 ข้อทั่วไปผ่าน (20 แต้ม) + โซน 0 จาก 10 -> 20/30 x 20 = 13.33
    assert crit_of(cam1)["S2"]["score"] == 13.33
    i = [n for n, pr in enumerate(AI["prompts"]) if f"[{zcode}] ภายในกรอบสีน้ำเงินที่มีป้าย {zcode} บนภาพ: ทางเดิน" in pr]
    assert len(i) == 1                                                            # มีภาพเดียวที่ได้ข้อโซน = ภาพจากกล้อง
    sent = Image.open(io.BytesIO(AI["images"][i[0]]))
    stored = Image.open(io.BytesIO(a.get(f"/photos/{cam1}/image").content))
    x, y = int(sent.width * 0.1) + 1, int(sent.height * 0.7)
    r_, g_, b_ = sent.getpixel((x, y))
    assert b_ > 200 and r_ < 80                                                   # ภาพที่ส่งให้ AI มีกรอบสีน้ำเงิน
    assert stored.getpixel((x, y)) != sent.getpixel((x, y))                       # ภาพหลักฐานที่เก็บไว้ไม่ถูกวาดทับ
    assert zcode not in checks_of(phone) and photo(phone).area_id == aid          # ภาพมือถือไม่ใช้ข้อโซน
    page = a.get(f"/photos/{cam1}").text
    assert 'class="zbox" style="left:10.0%;top:50.0%;width:80.0%;height:45.0%"' in page and "กรอบสีน้ำเงินคือโซน" in page
    # แก้ผลของข้อโซนด้วยกรรมการ: ข้อโซนไม่มีตัวเลือก มองไม่เห็น
    form = {f"check_{c}": "ok" for c in k}
    assert S["x_aud"].post(f"/admin/photos/{cam1}/override", data=dict(form, **{f"check_{zcode}": "na"}, note="ทดสอบ na กับโซน")).status_code == 303
    assert photo(cam1).overridden is False
    assert S["x_aud"].post(f"/admin/photos/{cam1}/override", data=dict(form, note="เข็นรถออกแล้ว ตรวจหน้างาน")).status_code == 303
    assert checks_of(cam1)[zcode]["status"] == "ok" and checks_of(cam1)[zcode]["zone"] == [100, 500, 900, 950]
    AI["status"] = {}
    S.update(aid=aid, cam1=cam1, zcode=zcode, phone=phone)


def test_07_two_pass_check_in_checklist_mode():
    d, m, r2 = S["d"], S["x_mem"], S["r2"]
    save(ai_passes=2)
    AI["queue"] = [{"C09": "ok", "C10": "minor"}, {"C09": "major", "C10": "ok"}]
    p = upload(m, r2, d["X1"], 40, "มุมเครื่องเชื่อม 2")
    drain()
    k = checks_of(p)
    assert k["C09"]["status"] == "major" and k["C09"]["seen"] == "ok และ major" and photo(p).review_flag is True
    assert k["C10"]["status"] == "minor" and k["C10"]["seen"] == "minor และ ok"      # ต่างกัน 1 ขั้น ยึดผลที่แย่กว่า
    assert photo(p).analysis["unstable"] == ["C09", "C10"]
    assert "สองรอบได้ ok และ major" in S["admin"].get(f"/photos/{p}").text
    save(ai_passes=1)
    S["p_two"] = p


def test_08_exports_dataset_backup_and_level_mode_still_works():
    a, d, r1, r2 = S["admin"], S["d"], S["r1"], S["r2"]
    rows = list(csv.reader(io.StringIO(a.get(f"/rounds/{r1}/export/checks.csv").content.decode("utf-8-sig"))))
    head = rows[0]
    assert head[6:9] == ["รหัสข้อ", "รายการตรวจ", "ผล"] and len(rows) == 1 + 13 + 14
    fixed = [x for x in rows[1:] if x[0] == str(S["p2"]) and x[6] == "C01"][0]
    assert (fixed[head.index("ผล")], fixed[head.index("ผลที่ AI เสนอ")], fixed[head.index("กรรมการแก้ไข")]) == ("ผ่าน", "บกพร่องเล็กน้อย", "ใช่")
    assert S["x_mem"].get(f"/rounds/{r1}/export/checks.csv").status_code == 403
    # ชุดข้อมูลสำหรับฝึกโมเดล: เฉพาะภาพที่คนยืนยันแล้ว
    z = zipfile.ZipFile(io.BytesIO(a.get(f"/rounds/{r1}/dataset.zip").content))
    labels = [json.loads(x) for x in z.read("labels.jsonl").decode().splitlines()]
    assert {x["photo_id"] for x in labels} == {S["p1"], S["p2"]} and f"images/{S['p1']}.jpg" in z.namelist()
    one = [x for x in labels if x["photo_id"] == S["p1"]][0]
    c04 = [x for x in one["checks"] if x["code"] == "C04"][0]
    assert (c04["status"], c04["ai_status"], c04["box"]) == ("major", "major", [200, 100, 600, 400]) and one["verified_by"] == "x_aud"
    assert "C04" in json.loads(z.read("classes.json")) and "NOT corrected by a person" in z.read("README.txt").decode()
    # สำรองและนำกลับ: โหมด รายการตรวจ และผลรายข้ออยู่ครบ
    raw = a.get(f"/admin/rounds/{r1}/backup.zip").content
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("manifest.json"))
    assert manifest["round"]["mode"] == "checklist" and len(manifest["round"]["checklist"]) == 14
    assert "fbox" in zipfile.ZipFile(io.BytesIO(raw)).read("report.html").decode()
    assert a.post("/admin/restore", files={"file": ("r.zip", raw, "application/zip")}).status_code == 303
    with dbm.SessionLocal() as s:
        back = s.query(dbm.Round).order_by(dbm.Round.id.desc()).first()
        assert back.mode == "checklist" and "นำกลับ" in back.name
        copy = s.query(dbm.Photo).filter_by(round_id=back.id, area_name="ไลน์เชื่อม A").one()
        assert copy.analysis["checks"][3]["code"] == "C04" and copy.verified_by == "x_aud"
        rid_back = back.id
    assert a.get(f"/photos/{copy.id}").status_code == 200 and a.get(f"/dashboard?round={rid_back}").status_code == 200
    a.post(f"/admin/rounds/{rid_back}/delete")
    # รอบแบบระดับ 0 ถึง 4 ยังใช้ได้เหมือนเดิม
    r3 = new_round(a, "รอบ 1.3 ระดับ", mode="level")
    p = upload(S["x_mem"], r3, d["X1"], 50, "ไลน์เชื่อม A")
    drain()
    ph = photo(p)
    assert ph.status == "done" and ph.analysis.get("mode") != "checklist" and ph.percent == 75.0 and "checks" not in ph.analysis
    assert S["x_aud"].post(f"/admin/photos/{p}/verify").status_code == 303
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Action).filter_by(photo_id=p).count() == 0             # โหมดระดับไม่มีผลรายข้อ จึงไม่สร้างงานเอง
    page = a.get(f"/dashboard?round={r3}").text
    assert "รอบนี้ใช้โหมดระดับ 0 ถึง 4" in page and "รายการตรวจ" in a.get(f"/ranking?round={r2}").text
    S["r3"] = r3


def test_09_pages_for_every_role_and_upgrade_from_1_2():
    a, d = S["admin"], S["d"]
    urls = ["/", "/capture", "/photos", f"/photos/{S['p1']}", f"/photos/{S['p_fixed']}", "/ranking", "/trend", "/actions",
            "/actions?status=all", f"/rounds/{S['r2']}/dept/{d['X1']}", f"/dashboard?round={S['r2']}"]
    for name in ("admin", "x_aud", "x_mem", "x_m2"):
        for url in urls:
            r = S[name].get(url)
            assert r.status_code == 200 and "Traceback" not in r.text, (name, url)
    for url in ("/verify", "/admin", "/admin/checkpoints", "/admin/criteria", "/admin/areas", f"/admin/areas/{S['aid']}/zones",
                "/admin/quality", "/admin/rounds", "/admin/settings", "/admin/storage", f"/rounds/{S['r2']}/report",
                f"/rounds/{S['r2']}/export/scores.xlsx", f"/rounds/{S['r2']}/export/photos.csv", f"/rounds/{S['r2']}/export/data.json"):
        assert a.get(url).status_code == 200, url
    form = {"org_name": "AHP", "area_types": "สายการผลิต\nคลัง", "ai1_type": "gemini", "ai1_model": "gemini-flash", "ai2_type": "none",
            "scoring_mode": "checklist", "action_due_days": 10, "action_due_days_major": 2, "member_see_all": "1", "allow_free_area": "1"}
    a.post("/admin/settings", data=form)
    s_ = settings_store.load()
    assert (s_["scoring_mode"], s_["auto_actions"], s_["action_due_days"], s_["action_due_days_major"]) == ("checklist", False, 10, 2)
    # ฐานข้อมูลรุ่น 1.2: ไม่มีตารางและคอลัมน์ของรุ่น 1.3
    from sqlalchemy import inspect, text
    with dbm.engine.begin() as conn:
        n = conn.execute(text("select count(*) from photos")).scalar()
        for table, col in (("rounds", "mode"), ("rounds", "checklist"), ("rounds", "rule_rev"), ("criteria", "kind")):
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
        conn.execute(text("DROP TABLE actions"))
        conn.execute(text("DROP TABLE checkpoints"))
    dbm.init_db()
    insp = inspect(dbm.engine)
    assert {"mode", "checklist", "rule_rev"} <= {c["name"] for c in insp.get_columns("rounds")}
    assert {"actions", "checkpoints"} <= set(insp.get_table_names())
    with dbm.engine.begin() as conn:
        assert conn.execute(text("select count(*) from photos")).scalar() == n
        assert conn.execute(text("select count(*) from rounds where mode = 'level'")).scalar() >= 3      # รอบเดิมเป็นโหมดระดับ
        assert conn.execute(text("select count(*) from criteria where kind = 'ai'")).scalar() == 5
        assert conn.execute(text("select count(*) from checkpoints")).scalar() == 13                    # ได้รายการตรวจตั้งต้น
    for url in ("/", "/photos", f"/photos/{S['p1']}", "/ranking", "/actions", "/dashboard", "/admin/checkpoints", "/admin/rounds", "/capture"):
        assert a.get(url).status_code == 200, url


def test_10_backup_provider_models_are_probed_not_guessed():
    """ผู้ให้บริการแบบ OpenAI-compatible ที่ไม่บอกว่ารุ่นใดรับภาพได้: ระบบถามแต่ละรุ่นด้วยภาพทดสอบ ไม่เดาจากชื่อ"""
    a = S["admin"]
    names = ["allam-2-7b", "whisper-large-v3", "llama-3.3-70b-versatile", "qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": n} for n in names]})
        model = json.loads(request.content)["model"]
        asked.append(model)
        if model == "retired-vision-model":
            return httpx.Response(404, json={"error": {"message": "The model `retired-vision-model` does not exist or you do not have access to it."}})
        if model.startswith("qwen/"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "<think>red square</think>{\"ok\": true}"}}]})
        return httpx.Response(400, json={"error": {"message": "this model does not support image input"}})
    old = ai._transport
    ai._transport = httpx.MockTransport(handler)
    try:
        body = {"slot": "ai2", "type": "openai", "base": "https://api.groq.test/openai/v1", "key": "gsk_x"}
        r = a.post("/admin/ai/models", json=body).json()
        assert r["tested"] is True and r["likely"] == ["qwen/qwen3.8-27b", "qwen/qwen3.6-27b"]          # รุ่นใหม่สุดขึ้นก่อน
        assert r["models"][:2] == r["likely"] and "allam-2-7b" not in asked and "whisper-large-v3" not in asked
        asked.clear()
        r = a.post("/admin/ai/models", json=dict(body, base="http://192.168.1.50:11434/v1")).json()
        assert r["tested"] is False and asked == []                                # เครื่องในโรงงาน: ไม่ไล่โหลดทุกรุ่น
        r = a.post("/admin/ai/test", json=dict(body, model="retired-vision-model")).json()
        assert r["ok"] is False and "does not exist" in r["error"] and "ตรวจชื่อโมเดล" in r["error"]
        assert ai.extract_json("<think>{draft}</think>\n{\"image_ok\": true}") == {"image_ok": True}
    finally:
        ai._transport = old

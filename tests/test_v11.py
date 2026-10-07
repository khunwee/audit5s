"""ชุดทดสอบของรุ่น 1.1: สิทธิ์รายบัญชี การตั้งค่า การส่งออก การแจ้งเตือนทุกช่องทาง กล้อง IP โปรแกรมกล้อง
การประเมินสองรอบ และภาพก่อนกับหลังแก้ไข — ทุกการเรียกออกนอกระบบใช้ปลายทางจำลอง
"""
import csv
import io
import json
import os
import random
import tempfile
import time

import httpx
import pytest
from PIL import Image

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fives_v11_"))
os.environ["DISABLE_WORKER"] = "1"
os.environ.setdefault("ADMIN_PASSWORD", "admin1234")

from fastapi.testclient import TestClient  # noqa: E402

from app import ai, cameras, db as dbm, notify, scoring, settings_store, worker  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PW = "Factory5S2026"
AI = {"queue": [], "default": [3, 3, 3, 3, 3], "image_ok": True, "bodies": []}
OUT = {"calls": [], "fail": set(), "mail": []}
CAM = {"mode": "ok", "hits": []}
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


# ---------------------------------------------------------------- ปลายทางจำลอง
def ai_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    AI["bodies"].append(body)
    text_in = json.dumps(body, ensure_ascii=False)
    if "ข้อมูลผลการตรวจ 5ส ของแผนกหนึ่ง" in text_in:
        out = dict(overview="สรุป", strengths=[], priorities=[])
    else:
        levels = AI["queue"].pop(0) if AI["queue"] else AI["default"]
        crit = [dict(code=c, na=False, level=levels[i], reason=f"เหตุผล {c}", findings=[f"พบ {c}"], recommendations=[f"แก้ {c}"])
                for i, c in enumerate(S["codes"])]
        out = dict(image_ok=AI["image_ok"], image_issue="" if AI["image_ok"] else "ภาพมืดเกินไป", scene="-", criteria=crit,
                   summary="สรุป", top_actions=["จัดของบนพื้นเข้าชั้น"])
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(out, ensure_ascii=False)}]}}]})


def out_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    body = json.loads(request.content) if request.content else {}
    OUT["calls"].append(dict(url=url, body=body, headers=dict(request.headers)))
    for key in OUT["fail"]:
        if key in url:
            return httpx.Response(401, json={"description": "Unauthorized"})
    if "discord" in url:
        return httpx.Response(204)
    return httpx.Response(200, json={"ok": True})


def cam_handler(request: httpx.Request) -> httpx.Response:
    CAM["hits"].append(dict(url=str(request.url), auth=request.headers.get("authorization", "")))
    path = request.url.path
    if path == "/basic.jpg":
        if not request.headers.get("authorization", "").startswith("Basic "):
            return httpx.Response(401)
        return httpx.Response(200, content=jpeg(len(CAM["hits"]) + 9000), headers={"content-type": "image/jpeg"})
    if path == "/digest.jpg":
        if not request.headers.get("authorization", "").startswith("Digest "):
            return httpx.Response(401, headers={"www-authenticate": 'Digest realm="cam", nonce="abc123", qop="auth", algorithm=MD5'})
        return httpx.Response(200, content=jpeg(len(CAM["hits"]) + 9100), headers={"content-type": "image/jpeg"})
    if path == "/mjpeg":
        frame = jpeg(len(CAM["hits"]) + 9200)
        data = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame[:900]
        return httpx.Response(200, content=data, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"})
    if path == "/broken":
        return httpx.Response(500, content=b"camera error")
    return httpx.Response(404)


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.user = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.user = (user, password)

    def send_message(self, msg):
        OUT["mail"].append(dict(host=self.host, port=self.port, tls=self.tls, user=self.user, subject=msg["Subject"],
                                to=msg["To"], body=msg.get_content()))


@pytest.fixture(scope="module", autouse=True)
def wiring():
    import smtplib
    old = (ai._transport, notify._transport, cameras._transport, smtplib.SMTP)
    ai._transport = httpx.MockTransport(ai_handler)
    notify._transport = httpx.MockTransport(out_handler)
    cameras._transport = httpx.MockTransport(cam_handler)
    smtplib.SMTP = FakeSMTP
    yield
    ai._transport, notify._transport, cameras._transport, smtplib.SMTP = old


# ---------------------------------------------------------------- ตัวช่วย
def client(username, password, new_password=None):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303 and r.headers["location"] == "/", (username, r.headers.get("location"))
    if c.get("/").headers.get("location") == "/account/password":
        c.post("/account/password", data={"current": password, "new": new_password, "confirm": new_password})
    assert c.get("/").status_code == 200
    return c


def admin():
    with dbm.SessionLocal() as s:
        fresh = s.get(dbm.User, 1).must_change
    return client("admin", "admin1234", ADMIN_PW) if fresh else client("admin", ADMIN_PW)


def upload(c, did, seed, area="จุดตรวจ", expect=200, **extra):
    data = dict(round_id=S["rid"], department_id=did, area_name=area, area_type=extra.pop("area_type", "สำนักงาน"), **extra)
    r = c.post("/api/photos", data=data, files={"file": ("p.jpg", jpeg(seed), "image/jpeg")})
    assert r.status_code == expect, r.text
    return r.json().get("id") if expect == 200 else r.json()


def drain():
    n = 0
    while worker.process_one() and n < 100:
        n += 1
    return n


def photo(pid):
    with dbm.SessionLocal() as s:
        return s.get(dbm.Photo, pid)


def save_settings(**kw):
    with dbm.SessionLocal() as s:
        settings_store.save(s, kw)


def sent_to(part):
    return [c for c in OUT["calls"] if part in c["url"]]


# ---------------------------------------------------------------- ทดสอบ
def test_01_setup_and_account_permissions():
    a = admin()
    S["admin"] = a
    for code, name in (("V1", "ประกอบ"), ("V2", "พ่นสี"), ("V3", "ขึ้นรูป")):
        a.post("/admin/departments/save", data={"code": code, "name": name})
    with dbm.SessionLocal() as s:
        S["d"] = {d.code: d.id for d in s.query(dbm.Department).all()}
        for r in s.query(dbm.Round).filter_by(status="open"):
            r.status = "closed"
        s.query(dbm.Channel).delete()
        s.commit()
    notify.refresh_channels()
    d = S["d"]
    save_settings(ai1_type="gemini", ai1_key="g-key", ai1_model="gemini-flash", ai2_type="none", ai_daily=100000,
                  ai_passes=1, ai_max_attempts=3, storage_budget_mb=350, retention_days=0, ranking_visibility="closed",
                  member_see_all=False, after_replaces=True, public_url="https://5s.example.test")
    a.post("/admin/rounds/save", data={"name": "รอบทดสอบ 1.1", "min_photos": 1})
    with dbm.SessionLocal() as s:
        rnd = s.query(dbm.Round).order_by(dbm.Round.id.desc()).first()
        S["rid"], S["codes"] = rnd.id, [c["code"] for c in rnd.rubric]

    def make(username, role, dept, extra=(), **perms):
        data = {"username": username, "full_name": username, "role": role, "department_id": dept or "", "password": "Start1234",
                "extra_depts": [str(x) for x in extra]}
        data.update({f"perm_{k}": v for k, v in perms.items()})
        assert a.post("/admin/users/save", data=data).status_code == 303
    make("v_mem", "member", d["V1"])
    make("v_lead", "member", d["V1"], extra=[d["V2"]], export="1", score="1", rounds="1")
    make("v_aud", "auditor", None, export="0")
    with dbm.SessionLocal() as s:
        lead = s.query(dbm.User).filter_by(username="v_lead").one()
        assert lead.perms == {"export": True, "score": True, "rounds": True} and lead.extra_depts == [d["V2"]]
    mem, lead, aud = (client(u, "Start1234", "Member5S99x") for u in ("v_mem", "v_lead", "v_aud"))
    S.update(mem=mem, lead=lead, aud=aud)
    rid = S["rid"]

    # ตัวแทนแผนกทั่วไป: ไม่มีสิทธิ์เพิ่ม
    assert mem.get(f"/rounds/{rid}/export/ranking.csv").status_code == 403
    assert mem.get("/admin").status_code == 403 and mem.get("/trend.csv").status_code == 403
    assert "ยังไม่เปิดให้ดู" in mem.get("/ranking").text
    assert upload(mem, d["V2"], 1, expect=403)["detail"]
    assert "จัดการระบบ" not in mem.get("/").text
    # หัวหน้า: แผนกเพิ่มเติม + ส่งออก + ปรับคะแนน + จัดการรอบ แต่ไม่ใช่ผู้ดูแล
    S["p_v2"] = upload(lead, d["V2"], 2, area="ห้องพ่นสี")
    S["p_v1"] = upload(lead, d["V1"], 3, area="โต๊ะประกอบ A")
    assert upload(lead, d["V3"], 4, expect=403)["detail"]
    assert lead.get(f"/rounds/{rid}/export/ranking.csv").status_code == 200
    page = lead.get("/admin")
    assert page.status_code == 200 and "/admin/rounds" in page.text and "/admin/users" not in page.text
    assert lead.get("/admin/rounds").status_code == 200
    for url in ("/admin/users", "/admin/settings", "/admin/notifications", "/admin/cameras", "/admin/storage",
                "/admin/logs", "/admin/criteria", f"/admin/rounds/{rid}/backup.zip"):
        assert lead.get(url).status_code == 403, url
    assert lead.post(f"/admin/rounds/{rid}/delete").status_code == 403           # ลบรอบต้องมีสิทธิ์สำรองและลบข้อมูล
    assert lead.post("/admin/users/save", data={"username": "hack", "role": "admin", "password": "Hack12345"}).status_code == 403
    assert lead.get(f"/photos/{S['p_v2']}").status_code == 200
    # กรรมการที่ถูกปิดสิทธิ์ส่งออก: ยังดูอันดับและส่งภาพแทนทุกแผนกได้
    assert aud.get(f"/rounds/{rid}/export/ranking.csv").status_code == 403
    assert "ยังไม่เปิดให้ดู" not in aud.get("/ranking").text
    S["p_v3"] = upload(aud, d["V3"], 5, area="เครื่องปั๊ม 3")
    assert aud.get("/admin").status_code == 403
    # ผู้ดูแลเห็นหน้าผู้ใช้พร้อมตารางสิทธิ์
    page = a.get("/admin/users")
    assert page.status_code == 200 and "ตั้งสิทธิ์เฉพาะบัญชี" in page.text and "perm_export" in page.text
    drain()
    assert all(photo(S[k]).status == "done" for k in ("p_v1", "p_v2", "p_v3"))
    # สิทธิ์ปรับคะแนนใช้ได้จริง และตัวแทนทั่วไปทำไม่ได้
    data = {f"level_{c}": 4 for c in S["codes"]}
    assert mem.post(f"/admin/photos/{S['p_v1']}/override", data=dict(data, note="ขอคะแนนเต็ม")).status_code == 403
    assert lead.post(f"/admin/photos/{S['p_v1']}/override", data=dict(data, note="ภาพของตัวเอง")).status_code == 403   # ปรับภาพที่ตัวเองส่งไม่ได้
    assert lead.post(f"/admin/photos/{S['p_v3']}/override", data=dict(data, note="ตรวจหน้างานแล้วเรียบร้อย")).status_code == 303
    assert photo(S["p_v3"]).percent == 100.0 and photo(S["p_v3"]).overridden and photo(S["p_v3"]).verified_by == "v_lead"


def test_02_admin_settings_take_effect():
    a, d = S["admin"], S["d"]
    form = {"org_name": "AHP", "public_url": "https://5s.example.test/", "area_types": "โซนเชื่อม\nโซนประกอบ\n\nคลัง FG\nโซนเชื่อม",
            "band_good": 90, "band_mid": 70, "ai_extra": "ทางเดินต้องมีเส้นสีเหลืองกว้าง 10 ซม.", "ai_passes": "1",
            "after_replaces": "1", "webcam_width": 1280, "ai1_type": "gemini", "ai1_model": "gemini-flash", "ai2_type": "none",
            "ai_rpm": 30, "ai_daily": 100000, "ai_max_attempts": 3, "img_max_side": 1280, "img_quality": 78,
            "allow_gallery": "1", "storage_budget_mb": 350, "retention_days": 0, "purge_requires_backup": "1",
            "ranking_visibility": "closed"}
    assert a.post("/admin/settings", data=form).status_code == 303
    s = settings_store.load()
    assert s["area_types"] == ["โซนเชื่อม", "โซนประกอบ", "คลัง FG"] and s["public_url"] == "https://5s.example.test"
    assert (s["band_good"], s["band_mid"], s["webcam_width"], s["ai1_key"]) == (90, 70, 1280, "g-key")
    from app.web import band
    assert (band(92), band(85), band(60)) == ("good", "mid", "low")
    page = S["lead"].get("/capture")
    assert page.status_code == 200 and "คลัง FG</option>" in page.text and "สำนักงาน</option>" not in page.text
    assert 'data-webcam-width="1280"' in page.text and 'id="webcam-open"' in page.text
    AI["bodies"].clear()
    p1 = upload(S["lead"], d["V1"], 20, area="จุดเชื่อม 1", area_type="โซนเชื่อม")
    p2 = upload(S["lead"], d["V1"], 21, area="จุดเชื่อม 2", area_type="ประเภทที่ไม่มี")
    assert photo(p1).area_type == "โซนเชื่อม" and photo(p2).area_type == "คลัง FG"      # ค่าที่ไม่รู้จัก ใช้ประเภทสุดท้ายของรายการ
    drain()
    prompt = json.dumps(AI["bodies"][0], ensure_ascii=False)
    assert "เส้นสีเหลืองกว้าง 10 ซม." in prompt and "โซนเชื่อม" in prompt and "ประกอบ" not in prompt.split("ข้อมูลประกอบจากผู้ถ่าย")[0]
    assert "ป้ายสีเหลือง" not in a.get(f"/photos/{p1}").text and a.get(f"/photos/{p1}").status_code == 200
    S.update(p_w1=p1, p_w2=p2)


def test_03_exports():
    a, rid, d = S["admin"], S["rid"], S["d"]
    r = a.get(f"/rounds/{rid}/export/ranking.csv")
    assert r.status_code == 200 and r.content[:3] == b"\xef\xbb\xbf" and "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["อันดับ", "รหัสแผนก", "แผนก", "คะแนนเฉลี่ย (%)"] and len(rows[0]) == 14 + len(S["codes"])
    ranked = [x for x in rows[1:] if x[-1] == "จัดอันดับแล้ว"]
    assert [x[0] for x in ranked] == ["1", "2", "2"] and {x[1] for x in ranked} == {"V1", "V2", "V3"}   # คะแนนเท่ากัน อันดับร่วม
    r = a.get(f"/rounds/{rid}/export/photos.csv?dept={d['V1']}")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert len(rows) == 4 and all(x[1] == "V1" for x in rows[1:]) and "เหตุผล" in rows[1][rows[0].index(f"{S['codes'][0]} เหตุผล")]
    assert len(list(csv.reader(io.StringIO(a.get(f"/rounds/{rid}/export/photos.csv").content.decode("utf-8-sig"))))) == 6
    data = a.get(f"/rounds/{rid}/export/data.json").json()
    assert data["format"] == "fives-round-export/1" and len(data["photos"]) == 5 and len(data["ranking"]) == 3
    assert data["ranking"][0]["rank"] == 1 and "analysis" in data["photos"][0]
    from openpyxl import load_workbook
    assert load_workbook(io.BytesIO(a.get(f"/rounds/{rid}/export/scores.xlsx").content))["อันดับ"]["A5"].value == 1
    assert a.get(f"/rounds/{rid}/export/secret.txt").status_code == 404
    page = a.get(f"/ranking?round={rid}")
    assert "export/photos.csv" in page.text and "backup.zip" in page.text
    assert "export/photos.csv" not in S["aud"].get(f"/ranking?round={rid}").text       # ไม่มีสิทธิ์ ไม่เห็นเมนู
    page = a.get("/trend")
    assert page.status_code == 200 and "รอบทดสอบ 1.1" in page.text and "ประกอบ" in page.text
    rows = list(csv.reader(io.StringIO(a.get("/trend.csv").content.decode("utf-8-sig"))))
    assert rows[0][0] == "แผนก" and "รอบทดสอบ 1.1" in rows[0]
    assert S["lead"].get("/trend").status_code == 200
    with dbm.SessionLocal() as s:
        assert s.query(dbm.AuditLog).filter_by(action="export").count() >= 5


def channel(a, name, kind, dept, events, **cfg):
    data = {"name": name, "kind": kind, "department_id": dept or ""}
    data.update({f"ev_{e}": "1" for e in events})
    data.update({f"cfg_{kind}_{k}": v for k, v in cfg.items()})
    assert a.post("/admin/notifications/save", data=data).status_code == 303
    with dbm.SessionLocal() as s:
        return s.query(dbm.Channel).filter_by(name=name).one().id


def test_04_notifications_all_channels():
    a, d, rid = S["admin"], S["d"], S["rid"]
    with dbm.SessionLocal() as s:
        assert s.query(dbm.NotifyEvent).count() == 0            # ยังไม่มีช่องทาง ระบบไม่เขียนคิวแจ้งเตือนเลย
    ch = dict(
        tg=channel(a, "TG ประกอบ", "telegram", d["V1"], ["upload", "result"], bot_token="111:AAA", chat_id="-100200"),
        dc=channel(a, "Discord กรรมการ", "discord", None, ["result", "round", "system"], webhook_url="https://discord.com/api/webhooks/1/x"),
        ln=channel(a, "LINE พ่นสี", "line", d["V2"], ["problem"], token="line-token", to="C123"),
        wh=channel(a, "Webhook กลาง", "webhook", None, ["upload", "result"], url="https://hooks.example.test/in", secret="s3cret"),
        bv=channel(a, "อีเมลประกอบ", "email_brevo", d["V1"], ["result"], api_key="brevo-key", sender="5s@ahp.test", to="a@ahp.test, b@ahp.test"),
        sm=channel(a, "SMTP ในโรงงาน", "email_smtp", d["V1"], ["result"], host="smtp.ahp.test", port="587", security="starttls",
                   username="u", password="p", sender="5s@ahp.test", to="boss@ahp.test"))
    assert a.post("/admin/notifications/save", data={"name": "ไม่เลือกเรื่อง", "kind": "discord"}).status_code == 303
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Channel).count() == 6
    # แก้ไขโดยเว้นช่องลับว่าง = ใช้ค่าเดิม และหน้าเว็บไม่แสดงค่าลับ
    a.post("/admin/notifications/save", data={"id": ch["tg"], "name": "TG ประกอบ", "kind": "telegram", "department_id": d["V1"],
                                              "ev_upload": "1", "ev_result": "1", "cfg_telegram_bot_token": "", "cfg_telegram_chat_id": "-100200"})
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Channel, ch["tg"]).config == {"bot_token": "111:AAA", "chat_id": "-100200"}
    page = a.get("/admin/notifications").text
    assert "111:AAA" not in page and "line-token" not in page and "s3cret" not in page and "brevo-key" not in page

    # --- ภาพเข้าระบบ: รวมเป็นข้อความเดียวต่อแผนก
    OUT["calls"].clear()
    AI["queue"] = [[4, 4, 4, 4, 4], [2, 2, 2, 2, 2]]
    p1 = upload(S["mem"], d["V1"], 40, area="ชั้นวาง Jig")
    p2 = upload(S["mem"], d["V1"], 41, area="โต๊ะประกอบ B")
    assert notify.flush() == 2 and not OUT["calls"]             # ยังไม่ครบ 30 วินาทีหลังภาพล่าสุด รอรวมชุดก่อน
    with dbm.SessionLocal() as s:
        s.query(dbm.NotifyEvent).update({"created_at": dbm.now() - __import__("datetime").timedelta(seconds=40)})
        s.commit()
    assert notify.flush() == 0
    tg = sent_to("api.telegram.org")
    assert len(tg) == 1 and tg[0]["url"].endswith("/bot111:AAA/sendMessage") and tg[0]["body"]["chat_id"] == "-100200"
    text = tg[0]["body"]["text"]
    assert "ภาพใหม่จาก ประกอบ" in text and "ส่ง 2 ภาพ โดย v_mem" in text and "- ชั้นวาง Jig" in text
    assert f"https://5s.example.test/photos?round={rid}&dept={d['V1']}" in text
    wh = sent_to("hooks.example.test")
    assert len(wh) == 1 and wh[0]["headers"]["x-5s-secret"] == "s3cret" and wh[0]["body"]["event"] == "upload"
    assert wh[0]["body"]["count"] == 2 and "ภาพใหม่จาก ประกอบ" in wh[0]["body"]["text"]
    assert not sent_to("discord.com") and not sent_to("api.line.me") and not sent_to("brevo")

    # --- ผลวิเคราะห์: ส่งเมื่อคิวของแผนกว่าง
    OUT["calls"].clear()
    worker.process_one()
    assert notify.flush() == 1 and not OUT["calls"]             # ยังเหลืออีก 1 ภาพในคิวของแผนก รอให้ครบชุด
    drain()
    assert notify.flush() == 0
    text = sent_to("api.telegram.org")[0]["body"]["text"]
    assert "ผลวิเคราะห์ของ ประกอบ" in text and "ให้คะแนนแล้ว 2 ภาพ เฉลี่ย 75%" in text
    assert "- โต๊ะประกอบ B: 50% ควรทำก่อน: จัดของบนพื้นเข้าชั้น" in text and "- ชั้นวาง Jig: 100%" in text
    assert f"/rounds/{rid}/dept/{d['V1']}" in text and "คะแนนของแผนกตอนนี้" in text
    assert sent_to("discord.com")[0]["body"]["content"].startswith("[5ส Vision] ผลวิเคราะห์ของ ประกอบ")
    bv = sent_to("api.brevo.com")[0]
    assert bv["headers"]["api-key"] == "brevo-key" and bv["body"]["sender"]["email"] == "5s@ahp.test"
    assert [x["email"] for x in bv["body"]["to"]] == ["a@ahp.test", "b@ahp.test"] and "เฉลี่ย 75%" in bv["body"]["textContent"]
    mail = OUT["mail"][-1]
    assert (mail["host"], mail["port"], mail["tls"], mail["user"], mail["to"]) == ("smtp.ahp.test", 587, True, ("u", "p"), "boss@ahp.test")
    assert "ผลวิเคราะห์ของ ประกอบ" in mail["subject"] and "เฉลี่ย 75%" in mail["body"]
    assert sent_to("hooks.example.test")[0]["body"]["scored"] == 2 and not sent_to("api.line.me")

    # --- ภาพมีปัญหา: ช่องที่รับเฉพาะปัญหาได้ข้อความสั้น ช่องของแผนกอื่นไม่ได้รับ
    OUT["calls"].clear()
    OUT["mail"].clear()
    AI["image_ok"] = False
    upload(S["lead"], d["V2"], 42, area="ตู้อบสี")
    drain()
    AI["image_ok"] = True
    notify.flush(force=True)
    ln = sent_to("api.line.me")
    assert len(ln) == 1 and ln[0]["headers"]["authorization"] == "Bearer line-token" and ln[0]["body"]["to"] == "C123"
    msg = ln[0]["body"]["messages"][0]["text"]
    assert "ภาพที่มีปัญหา 1 ภาพ" in msg and "ตู้อบสี: ใช้ประเมินไม่ได้ ถ่ายใหม่ (ภาพมืดเกินไป)" in msg and "ให้คะแนนแล้ว" not in msg
    assert "ภาพที่มีปัญหา 1 ภาพ" in sent_to("discord.com")[0]["body"]["content"]
    assert not sent_to("api.telegram.org") and not sent_to("api.brevo.com") and not OUT["mail"]

    # --- ช่องทางหนึ่งล้มเหลว ช่องอื่นยังได้รับ และผู้ดูแลเห็นสาเหตุ
    OUT["calls"].clear()
    OUT["fail"] = {"api.telegram.org"}
    upload(S["mem"], d["V1"], 43, area="รถเข็นชิ้นงาน")
    drain()
    notify.flush(force=True)
    assert sent_to("api.brevo.com") and sent_to("hooks.example.test")
    with dbm.SessionLocal() as s:
        assert "401" in s.get(dbm.Channel, ch["tg"]).last_error
        assert s.query(dbm.NotifyLog).filter_by(ok=False, channel="TG ประกอบ").count() >= 1
        assert s.query(dbm.NotifyEvent).filter_by(done=False).count() == 0
    assert "ส่งครั้งล่าสุดไม่สำเร็จ" in a.get("/admin/notifications").text
    r = a.post(f"/admin/notifications/{ch['tg']}/test")
    assert r.status_code == 303 and "ส่งไม่สำเร็จ" in a.get("/admin/notifications").text
    OUT["fail"] = set()
    OUT["calls"].clear()
    a.post(f"/admin/notifications/{ch['tg']}/test")
    assert "ข้อความทดสอบ" in sent_to("api.telegram.org")[0]["body"]["text"]
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Channel, ch["tg"]).last_error == "" and s.get(dbm.Channel, ch["tg"]).last_ok_at is not None

    # --- กรรมการปรับคะแนน แผนกได้รับแจ้ง
    OUT["calls"].clear()
    data = {f"level_{c}": 3 for c in S["codes"]}
    a.post(f"/admin/photos/{p2}/override", data=dict(data, note="ดูหน้างานแล้ว ดีกว่าที่ AI ให้"))
    notify.flush(force=True)
    assert "โต๊ะประกอบ B: 75% (กรรมการปรับ)" in sent_to("api.telegram.org")[0]["body"]["text"]

    # --- ปิดช่องทาง = ไม่ส่ง, ลบช่องทางได้
    a.post("/admin/notifications/save", data={"id": ch["wh"], "name": "Webhook กลาง", "kind": "webhook", "active": "0",
                                              "ev_upload": "1", "cfg_webhook_url": "https://hooks.example.test/in"})
    OUT["calls"].clear()
    upload(S["mem"], d["V1"], 44, area="หลังปิด webhook")
    notify.flush(force=True)
    assert sent_to("api.telegram.org") and not sent_to("hooks.example.test")
    assert a.post(f"/admin/notifications/{ch['sm']}/delete").status_code == 303
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Channel, ch["sm"]) is None
    drain()
    notify.flush(force=True)
    S.update(ch=ch, p_n1=p1, p_n2=p2)


def cam(a, name, dept, url, mode="direct", source="snapshot", **kw):
    data = dict(name=name, department_id=dept, area_name=kw.pop("area", name), area_type="โซนประกอบ", mode=mode, source=source,
                url=url, **kw)
    assert a.post("/admin/cameras/save", data=data).status_code == 303
    with dbm.SessionLocal() as s:
        return s.query(dbm.Camera).filter_by(name=name).one().id


def test_05_ip_cameras_direct():
    a, d, rid = S["admin"], S["d"], S["rid"]
    c1 = cam(a, "กล้องประกอบ 1", d["V1"], "http://10.0.0.5/basic.jpg", username="admin", password="cam-pass", auth="basic", area="ไลน์ประกอบ 1")
    c2 = cam(a, "กล้องพ่นสี", d["V2"], "http://10.0.0.6/digest.jpg", username="admin", password="cam-pass", auth="digest")
    c3 = cam(a, "กล้อง MJPEG", d["V3"], "http://10.0.0.7/mjpeg")
    c4 = cam(a, "กล้องเสีย", d["V1"], "http://10.0.0.8/broken")
    a.post("/admin/cameras/save", data=dict(name="ลิงก์ผิด", department_id=d["V1"], url="ftp://x", source="snapshot"))
    a.post("/admin/cameras/save", data=dict(name="rtsp ผิด", department_id=d["V1"], url="http://x", source="rtsp"))
    with dbm.SessionLocal() as s:
        assert s.query(dbm.Camera).count() == 4
    page = a.get("/admin/cameras").text
    assert "กล้องประกอบ 1" in page and "cam-pass" not in page
    # แก้ไขโดยไม่ใส่รหัส = ใช้รหัสเดิม
    a.post("/admin/cameras/save", data=dict(id=c1, name="กล้องประกอบ 1", department_id=d["V1"], area_name="ไลน์ประกอบ 1",
                                            area_type="โซนประกอบ", mode="direct", source="snapshot", url="http://10.0.0.5/basic.jpg",
                                            username="admin", password="", auth="basic"))
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Camera, c1).password == "cam-pass"
    r = a.get(f"/admin/cameras/{c1}/test.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(r.content)).size == (1000, 750)
    assert CAM["hits"][-1]["auth"].startswith("Basic ")
    # ถ่ายเข้ารอบการตรวจ: basic, digest, mjpeg
    aud = S["aud"]
    r = aud.post(f"/api/cameras/{c1}/capture", data={"round_id": rid})
    assert r.status_code == 200 and r.json()["area"] == "ไลน์ประกอบ 1"
    p = photo(r.json()["id"])
    assert (p.source, p.camera_id, p.department_id, p.area_type, p.status) == ("ipcam", c1, d["V1"], "โซนประกอบ", "pending")
    r = aud.post(f"/api/cameras/{c2}/capture", data={"round_id": rid})
    assert r.status_code == 200 and CAM["hits"][-1]["auth"].startswith("Digest ") and 'username="admin"' in CAM["hits"][-1]["auth"]
    r = aud.post(f"/api/cameras/{c3}/capture", data={"round_id": rid})
    assert r.status_code == 200 and photo(r.json()["id"]).width == 1000          # ได้ภาพแรกจากสตรีมครบทั้งภาพ
    r = aud.post(f"/api/cameras/{c4}/capture", data={"round_id": rid})
    assert r.status_code == 502 and "500" in r.json()["detail"]
    with dbm.SessionLocal() as s:
        assert "500" in s.get(dbm.Camera, c4).last_error and s.get(dbm.Camera, c1).last_capture_at is not None
    # สิทธิ์: ตัวแทนทั่วไปไม่มีสิทธิ์สั่งกล้อง, หัวหน้าที่ได้สิทธิ์ใช้ได้เฉพาะกล้องของแผนกตัวเอง
    assert S["mem"].post(f"/api/cameras/{c1}/capture", data={"round_id": rid}).status_code == 403
    assert "กล้อง IP ที่ติดตั้งในพื้นที่" not in S["mem"].get("/capture").text
    with dbm.SessionLocal() as s:
        u = s.query(dbm.User).filter_by(username="v_lead").one()
        u.perms = dict(u.perms, cameras_use=True)
        s.commit()
    page = S["lead"].get("/capture").text
    assert "กล้องประกอบ 1" in page and "กล้องพ่นสี" in page and "กล้อง MJPEG" not in page      # รวมถึงรายการแนะนำชื่อจุดตรวจ
    assert S["lead"].post(f"/api/cameras/{c3}/capture", data={"round_id": rid}).status_code == 403
    assert S["lead"].post(f"/api/cameras/{c2}/capture", data={"round_id": rid}).status_code == 200
    # RTSP ที่ต่อไม่ได้ ต้องได้ข้อความที่อ่านเข้าใจ ไม่ใช่ข้อผิดพลาดของระบบ
    c5 = cam(a, "กล้อง RTSP", d["V1"], "rtsp://127.0.0.1:9/stream", source="rtsp")
    r = a.get(f"/admin/cameras/{c5}/test.jpg")
    assert r.status_code == 502 and ("RTSP" in r.text or "opencv" in r.text)
    a.post(f"/admin/cameras/{c5}/delete")
    drain()
    notify.flush(force=True)
    S.update(c1=c1, c4=c4)


def test_06_camera_agent_api():
    a, d, rid = S["admin"], S["d"], S["rid"]
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/api/agent/poll").status_code == 401                         # ยังไม่มีรหัส = ปิดทั้งหมด
    assert anon.get("/api/agent/poll", headers={"X-Agent-Token": ""}).status_code == 401
    assert a.post("/admin/cameras/token").status_code == 303
    token = settings_store.load()["agent_token"]
    assert len(token) >= 30 and token in a.get("/admin/cameras").text
    c9 = cam(a, "กล้องคลัง (agent)", d["V3"], "http://192.168.1.50/snap.jpg", mode="agent", username="admin", password="pw", auth="digest")
    cameras.agent["seen"] = 0
    r = S["aud"].post(f"/api/cameras/{c9}/capture", data={"round_id": rid})
    assert r.status_code == 503 and "ยังไม่ได้เชื่อมต่อ" in r.json()["detail"]
    assert anon.get("/api/agent/poll", headers={"X-Agent-Token": "wrong"}).status_code == 401
    h = {"X-Agent-Token": token}
    data = anon.get("/api/agent/poll", headers=h).json()
    assert data["open_round"] is True and data["requests"] == []
    assert data["cameras"] == [dict(id=c9, name="กล้องคลัง (agent)", source="snapshot", url="http://192.168.1.50/snap.jpg",
                                    username="admin", password="pw", auth="digest", department=data["cameras"][0]["department"],
                                    area="กล้องคลัง (agent)")]
    assert "เชื่อมต่ออยู่" in a.get("/admin/cameras").text
    assert a.get(f"/admin/cameras/{c9}/test.jpg").status_code == 400
    # สั่งถ่ายจากหน้าเว็บ -> agent เห็นคำสั่ง -> ส่งภาพขึ้นมา
    r = S["aud"].post(f"/api/cameras/{c9}/capture", data={"round_id": rid})
    assert r.status_code == 200 and r.json()["queued"] is True
    assert anon.get("/api/agent/poll", headers=h).json()["requests"] == [c9]
    r = anon.post("/api/agent/upload", headers=h, data={"camera_id": c9}, files={"file": ("c.jpg", jpeg(700), "image/jpeg")})
    assert r.status_code == 200 and r.json()["ok"] is True
    p = photo(r.json()["id"])
    assert (p.source, p.camera_id, p.department_id, p.uploader_name) == ("agent", c9, d["V3"], "กล้อง กล้องคลัง (agent)")
    assert "สั่งถ่ายโดย v_aud" in p.note and anon.get("/api/agent/poll", headers=h).json()["requests"] == []
    # agent รายงานว่าถ่ายไม่ได้ -> ผู้ดูแลเห็นสาเหตุในหน้ากล้อง
    r = anon.post("/api/agent/upload", headers=h, data={"camera_id": c9, "error": "เชื่อมต่อกล้องไม่ได้: timed out"})
    assert r.json()["ok"] is False and "timed out" in a.get("/admin/cameras").text
    assert anon.post("/api/agent/upload", headers={"X-Agent-Token": "bad"}, data={"camera_id": c9},
                     files={"file": ("c.jpg", jpeg(701), "image/jpeg")}).status_code == 401
    assert anon.post("/api/agent/upload", headers=h, data={"camera_id": S["c1"]},
                     files={"file": ("c.jpg", jpeg(702), "image/jpeg")}).status_code == 404   # กล้อง direct ไม่รับจาก agent
    # รายการกล้องของ agent อัปเดตเมื่อผู้ดูแลแก้ไข และเมื่อไม่มีรอบที่เปิดอยู่
    a.post("/admin/cameras/save", data=dict(id=c9, name="กล้องคลัง (agent)", department_id=d["V3"], mode="agent", source="snapshot",
                                            url="http://192.168.1.51/snap.jpg", username="admin", auth="digest"))
    assert anon.get("/api/agent/poll", headers=h).json()["cameras"][0]["url"] == "http://192.168.1.51/snap.jpg"
    a.post("/admin/cameras/token")
    assert anon.get("/api/agent/poll", headers=h).status_code == 401              # รหัสเดิมใช้ไม่ได้ทันที
    S.update(c9=c9, token=settings_store.load()["agent_token"])
    drain()
    notify.flush(force=True)


def test_07_two_pass_consistency_check():
    a, d = S["admin"], S["d"]
    save_settings(ai_passes=2)
    with dbm.SessionLocal() as s:
        before = worker.usage_today(s)
    AI["queue"] = [[4, 4, 4, 4, 4], [4, 3, 4, 1, 4]]
    pid = upload(S["aud"], d["V3"], 800, area="แท่นปั๊ม 5")
    drain()
    p = photo(pid)
    crit = {c["code"]: c for c in p.analysis["criteria"]}
    codes = S["codes"]
    assert [crit[c]["level"] for c in codes] == [4, 3, 4, 1, 4] and p.review_flag is True     # ยึดระดับต่ำกว่า + ต่างกัน 3 ระดับ
    assert crit[codes[3]]["seen"] == "4 และ 1" and crit[codes[1]]["seen"] == "4 และ 3" and "seen" not in crit[codes[0]]
    assert p.analysis["passes"] == 2 and p.analysis["unstable"] == [codes[1], codes[3]]
    with dbm.SessionLocal() as s:
        assert worker.usage_today(s) == before + 2
    AI["queue"] = [[3, 3, 3, 3, 3], [3, 2, 3, 3, 3]]
    pid2 = upload(S["aud"], d["V3"], 801, area="แท่นปั๊ม 6")
    drain()
    assert photo(pid2).review_flag is False and photo(pid2).percent == 70.0                     # ต่างกัน 1 ระดับ = ก้ำกึ่ง ไม่ต้องตรวจ
    page = a.get(f"/photos?round={S['rid']}&status=review").text
    assert f'href="/photos/{pid}"' in page and f'href="/photos/{pid2}"' not in page
    assert "สองรอบได้ 4 และ 1" in a.get(f"/photos/{pid}").text and "ควรให้กรรมการดู" in a.get("/admin").text
    assert S["mem"].post(f"/admin/photos/{pid}/accept").status_code == 403
    assert a.post(f"/admin/photos/{pid}/accept").status_code == 303 and photo(pid).review_flag is False
    assert photo(pid).verified_at is not None and photo(pid).verified_by == "ผู้ดูแลระบบ"
    save_settings(ai_passes=1)
    notify.flush(force=True)


def test_08_before_and_after():
    a, d, rid = S["admin"], S["d"], S["rid"]
    with dbm.SessionLocal() as s:        # เริ่มนับแผนก V2 ใหม่ให้ตัวเลขชัดเจน
        ids = [p.id for p in s.query(dbm.Photo).filter_by(round_id=rid, department_id=d["V2"])]
        s.query(dbm.PhotoImage).filter(dbm.PhotoImage.photo_id.in_(ids)).delete(synchronize_session=False)
        s.query(dbm.PhotoThumb).filter(dbm.PhotoThumb.photo_id.in_(ids)).delete(synchronize_session=False)
        s.query(dbm.Photo).filter(dbm.Photo.id.in_(ids)).delete(synchronize_session=False)
        s.commit()
    AI["queue"] = [[1, 1, 1, 1, 1], [3, 3, 3, 3, 3]]
    bad = upload(S["lead"], d["V2"], 900, area="มุมเก็บถังสี")
    ok = upload(S["lead"], d["V2"], 901, area="ชั้นวางหัวพ่น")
    drain()

    def avg():
        with dbm.SessionLocal() as s:
            return scoring.find_row(scoring.round_ranking(s, s.get(dbm.Round, rid)), d["V2"])
    assert avg()["avg"] == 50.0
    page = S["lead"].get(f"/photos/{bad}").text
    assert f'href="/capture?after={bad}"' in page
    page = S["lead"].get(f"/capture?after={bad}").text
    assert f'data-after="{bad}"' in page and 'data-area="มุมเก็บถังสี"' in page and "ส่งภาพหลังแก้ไขของจุด" in page
    assert 'data-after' not in S["mem"].get(f"/capture?after={bad}").text          # ไม่ใช่แผนกของตัวเอง ไม่ผูกกับภาพนั้น
    assert upload(S["lead"], d["V1"], 902, area="ผิดแผนก", expect=400, after_of=bad)["detail"]
    AI["queue"] = [[4, 4, 4, 4, 4]]
    fixed = upload(S["lead"], d["V2"], 903, area="มุมเก็บถังสี", after_of=bad)
    row = avg()
    assert row["avg"] == 50.0 and row["scored"] == 2                               # ภาพใหม่ยังไม่ได้คะแนน ยังนับภาพเดิม
    drain()
    row = avg()
    assert (row["avg"], row["scored"], row["replaced"]) == (87.5, 2, 1)            # (100 + 75) / 2 ภาพเดิม 25% ไม่ถูกนับ
    assert photo(fixed).after_of == bad
    page = a.get(f"/photos/{fixed}").text
    assert "ก่อนแก้ไข 25%" in page and "ดีขึ้น 75 จุดจากภาพก่อนแก้ไข" in page
    assert "หลังแก้ไข 100%" in a.get(f"/photos/{bad}").text
    assert "มีภาพหลังแก้ไขแล้ว ไม่นำมาเฉลี่ย" in a.get(f"/rounds/{rid}/dept/{d['V2']}").text
    save_settings(after_replaces=False)
    assert avg()["avg"] == round((25 + 75 + 100) / 3, 2)                           # ปิดการแทนที่ = นับทุกภาพ
    save_settings(after_replaces=True)
    rows = list(csv.reader(io.StringIO(a.get(f"/rounds/{rid}/export/photos.csv?dept={d['V2']}").content.decode("utf-8-sig"))))
    col = rows[0].index("ภาพหลังแก้ไขของภาพเลขที่")
    assert [r[col] for r in rows[1:]] == ["", "", str(bad)]
    # ลบภาพก่อนแก้ไข ภาพหลังแก้ไขยังอยู่และไม่ชี้ไปหาภาพที่หายไป
    assert a.post(f"/photos/{bad}/delete").status_code == 303 and photo(fixed).after_of is None
    assert a.get(f"/photos/{fixed}").status_code == 200
    notify.flush(force=True)
    S.update(p_ok=ok, p_fixed=fixed)


def test_09_round_events_backup_and_cleanup():
    a, d, rid = S["admin"], S["d"], S["rid"]
    OUT["calls"].clear()
    r = a.get(f"/admin/rounds/{rid}/backup.zip")
    assert r.status_code == 200
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(z.read("manifest.json"))
    assert {p["source"] for p in manifest["photos"]} >= {"mobile", "ipcam", "agent"}
    assert a.post(f"/admin/rounds/{rid}/close").status_code == 303
    notify.flush()
    text = sent_to("discord.com")[0]["body"]["content"]
    assert "ปิดรอบการตรวจ: รอบทดสอบ 1.1" in text and "อันดับ 1:" in text and f"/ranking?round={rid}" in text
    anon = TestClient(app, follow_redirects=False)
    h = {"X-Agent-Token": S["token"]}
    assert anon.get("/api/agent/poll", headers=h).json()["open_round"] is False
    assert anon.post("/api/agent/upload", headers=h, data={"camera_id": S["c9"]},
                     files={"file": ("c.jpg", jpeg(990), "image/jpeg")}).status_code == 409
    assert "ยังไม่มีรอบการตรวจที่เปิดรับภาพ" in S["mem"].get("/capture").text
    # ปิดรอบแล้ว ตัวแทนแผนกเห็นอันดับ (ตั้งให้แสดงเมื่อปิดรอบ)
    assert "ยังไม่เปิดให้ดู" not in S["mem"].get(f"/ranking?round={rid}").text
    OUT["calls"].clear()
    a.post(f"/admin/rounds/{rid}/reopen")
    notify.flush()
    assert "เปิดรอบการตรวจ: รอบทดสอบ 1.1" in sent_to("discord.com")[0]["body"]["content"]
    # แจ้งผู้ดูแลเมื่อ AI ใช้ไม่ได้ (จำกัดไม่ให้แจ้งซ้ำถี่)
    OUT["calls"].clear()
    save_settings(ai1_key="", _alert_ai_error="")
    for seed in (991, 992):
        upload(S["mem"], d["V1"], seed, area=f"ไม่มี key {seed}")
    drain()
    notify.flush(force=True)
    system = [c for c in sent_to("discord.com") if "แจ้งผู้ดูแลระบบ" in c["body"]["content"]]
    assert len(system) == 1 and "API key" in system[0]["body"]["content"]
    save_settings(ai1_key="g-key")
    a.post(f"/admin/rounds/{rid}/retry-errors")
    drain()
    notify.flush(force=True)
    # แผนกที่มีกล้อง ลบไม่ได้ ถูกปิดการใช้งานแทน และลบรอบแล้วไม่เหลือคิวแจ้งเตือนค้าง
    a.post("/admin/rounds/save", data={"name": "รอบว่าง 1.1", "min_photos": 1})
    with dbm.SessionLocal() as s:
        empty = s.query(dbm.Round).filter_by(name="รอบว่าง 1.1").one().id
    assert a.post(f"/admin/rounds/{empty}/delete").status_code == 303
    notify.flush(force=True)
    with dbm.SessionLocal() as s:
        assert s.get(dbm.Round, empty) is None and s.query(dbm.NotifyEvent).filter_by(done=False).count() == 0


def test_10_all_pages_render_for_every_role():
    rid, d = S["rid"], S["d"]
    urls = ["/", "/capture", "/photos", f"/photos?round={rid}&status=override", f"/photos/{S['p_n1']}", "/ranking", "/trend",
            f"/rounds/{rid}/dept/{d['V1']}", "/account/password"]
    for name in ("admin", "lead", "aud", "mem"):
        for url in urls:
            r = S[name].get(url)
            assert r.status_code == 200, (name, url, r.text[:200])
            assert "Traceback" not in r.text and "Internal Server Error" not in r.text
    for url in ("/admin", "/admin/rounds", "/admin/criteria", "/admin/departments", "/admin/users", "/admin/settings",
                "/admin/notifications", "/admin/cameras", "/admin/storage", "/admin/logs", f"/rounds/{rid}/report"):
        assert S["admin"].get(url).status_code == 200, url
    assert S["mem"].get(f"/photos/{S['p_fixed']}").status_code == 403             # ภาพของแผนกอื่น
    assert S["mem"].get(f"/rounds/{rid}/dept/{d['V3']}").status_code == 403       # ตั้งค่าไม่ให้ดูแผนกอื่น
    assert S["lead"].get(f"/rounds/{rid}/dept/{d['V2']}").status_code == 200      # แผนกเพิ่มเติมของตัวเอง


def test_11_upgrade_from_1_0_database():
    """ฐานข้อมูลรุ่น 1.0 ไม่มีคอลัมน์และตารางของรุ่น 1.1: เริ่มระบบแล้วต้องได้ครบ และแถวเดิมได้ค่าตั้งต้น"""
    from sqlalchemy import inspect, text
    with dbm.engine.begin() as conn:
        n = conn.execute(text("select count(*) from photos")).scalar()
        for col in ("source", "review_flag", "after_of", "camera_id"):
            conn.execute(text(f"ALTER TABLE photos DROP COLUMN {col}"))
        for col in ("perms", "extra_depts"):
            conn.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))
        for table in ("cameras", "notify_events", "notify_logs", "notify_channels"):
            conn.execute(text(f"DROP TABLE {table}"))
    dbm.init_db()
    insp = inspect(dbm.engine)
    assert {"source", "review_flag", "after_of", "camera_id"} <= {c["name"] for c in insp.get_columns("photos")}
    assert {"cameras", "notify_events", "notify_logs", "notify_channels"} <= set(insp.get_table_names())
    with dbm.engine.begin() as conn:
        assert conn.execute(text("select count(*) from photos")).scalar() == n
        assert conn.execute(text("select count(*) from photos where source = 'mobile'")).scalar() == n
        assert conn.execute(text("select count(*) from photos where review_flag is null")).scalar() == 0
    notify.refresh_channels()
    cameras.invalidate()
    a = S["admin"]
    for url in ("/", "/photos", "/admin/users", "/admin/cameras", "/admin/notifications", "/capture", "/ranking"):
        assert a.get(url).status_code == 200, url
    # บัญชีเดิมที่ยังไม่มีสิทธิ์เฉพาะบัญชี ใช้สิทธิ์ตามบทบาท
    assert S["mem"].get("/").status_code == 200 and S["aud"].get(f"/rounds/{S['rid']}/export/ranking.csv").status_code == 200

"""การแจ้งเตือนหลายช่องทาง

หลักการ
- แต่ละช่องทางผูกกับแผนกเดียว (เช่น กลุ่ม LINE ของแผนกผลิต) หรือรับของทุกแผนก (เช่น กลุ่มกรรมการ 5ส)
- รวมเป็นชุด ไม่ส่งทีละภาพ: "ภาพเข้าระบบ" รวมเมื่อหยุดส่งครบ 30 วินาที, "ผลวิเคราะห์" รวมเมื่อคิวของแผนกนั้นว่าง
  ลดข้อความรบกวน และไม่ชนโควตาข้อความฟรีของ LINE
- ส่งไม่สำเร็จไม่กระทบการให้คะแนน ทุกครั้งที่ส่งมีบันทึกให้ผู้ดูแลตรวจย้อนหลัง
"""
import logging
import smtplib
import threading
from collections import defaultdict
from datetime import timedelta
from email.message import EmailMessage

import httpx

from . import settings_store
from .db import (Channel, Department, NotifyEvent, NotifyLog, Photo, Round, SessionLocal, now)

log = logging.getLogger("fives.notify")
_transport = None            # ชุดทดสอบใส่ transport จำลอง

EVENTS = {
    "upload": "มีภาพใหม่เข้าระบบ",
    "result": "ผลวิเคราะห์จาก AI (คะแนนและสิ่งที่ควรทำก่อน)",
    "problem": "เฉพาะภาพที่ใช้ไม่ได้หรือวิเคราะห์ไม่สำเร็จ",
    "round": "เปิดและปิดรอบการตรวจ",
    "action": "งานแก้ไข: มอบหมายใหม่ และงานที่เกินกำหนด",
    "system": "เรื่องถึงผู้ดูแลระบบ (พื้นที่ การสำรอง ปัญหา AI รอบใกล้สิ้นสุด)",
}

# (ชื่อช่อง, ป้าย, เป็นความลับ, คำอธิบาย)
KINDS = {
    "telegram": ("Telegram", [
        ("bot_token", "Bot token", True, "สร้างบอทกับ @BotFather แล้วคัดลอก token"),
        ("chat_id", "Chat ID", False, "เลขของกลุ่มหรือบุคคลที่จะรับข้อความ กลุ่มขึ้นต้นด้วยเครื่องหมายลบ")]),
    "line": ("LINE (Messaging API)", [
        ("token", "Channel access token", True, "จาก LINE Developers Console ของ LINE Official Account"),
        ("to", "User ID หรือ Group ID ผู้รับ", False, "ขึ้นต้นด้วย U (บุคคล) หรือ C (กลุ่ม)")]),
    "discord": ("Discord", [
        ("webhook_url", "Webhook URL", True, "ตั้งค่าช่อง > Integrations > Webhooks")]),
    "webhook": ("Webhook ทั่วไป (Slack, Google Chat, ระบบอื่น)", [
        ("url", "URL ปลายทาง", False, "ระบบส่ง JSON ที่มีช่อง text จึงใช้กับ Slack และ Google Chat ได้ทันที"),
        ("secret", "ค่าลับ", True, "ไม่บังคับ ส่งไปใน header X-5S-Secret ให้ปลายทางตรวจ")]),
    "email_brevo": ("อีเมลผ่าน Brevo API (ใช้ได้บน host ฟรี)", [
        ("api_key", "API key", True, "สมัคร brevo.com ฟรี แล้วสร้าง key ที่เมนู SMTP & API"),
        ("sender", "อีเมลผู้ส่ง", False, "ต้องยืนยันอีเมลนี้ใน Brevo ก่อน"),
        ("to", "อีเมลผู้รับ", False, "หลายคนคั่นด้วยจุลภาค")]),
    "email_smtp": ("อีเมลผ่าน SMTP (เครื่องในโรงงาน)", [
        ("host", "SMTP host", False, "เช่น smtp.gmail.com"),
        ("port", "Port", False, "587 สำหรับ STARTTLS, 465 สำหรับ SSL"),
        ("security", "การเข้ารหัส", False, "starttls, ssl หรือ none"),
        ("username", "ชื่อผู้ใช้", False, ""),
        ("password", "รหัสผ่าน", True, "Gmail ต้องใช้ App password"),
        ("sender", "อีเมลผู้ส่ง", False, ""),
        ("to", "อีเมลผู้รับ", False, "หลายคนคั่นด้วยจุลภาค")]),
}


class NotifyError(Exception):
    pass


# --------------------------------------------------------------------------- ส่งจริง
def _post(url: str, json_body: dict, headers: dict = None, ok=(200, 201, 202, 204)):
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), transport=_transport) as c:
            r = c.post(url, json=json_body, headers=headers or {})
    except httpx.HTTPError as e:
        raise NotifyError(f"เชื่อมต่อปลายทางไม่ได้ ({type(e).__name__})")
    if r.status_code not in ok:
        raise NotifyError(f"ปลายทางตอบกลับ {r.status_code}: {(r.text or '')[:200]}")
    return r


def _emails(value: str) -> list:
    out = [x.strip() for x in (value or "").replace(";", ",").split(",") if "@" in x]
    if not out:
        raise NotifyError("ยังไม่ได้ใส่อีเมลผู้รับ")
    return out


def send(kind: str, cfg: dict, title: str, text: str, data: dict = None):
    """ส่งข้อความ 1 ครั้งไปยังช่องทางเดียว ล้มเหลวจะ raise NotifyError พร้อมสาเหตุที่อ่านเข้าใจ"""
    cfg = cfg or {}
    body = f"{title}\n{text}".strip()
    if kind == "telegram":
        if not cfg.get("bot_token") or not cfg.get("chat_id"):
            raise NotifyError("ใส่ Bot token และ Chat ID ให้ครบ")
        _post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
              {"chat_id": str(cfg["chat_id"]).strip(), "text": body[:4000], "disable_web_page_preview": True})
    elif kind == "line":
        if not cfg.get("token") or not cfg.get("to"):
            raise NotifyError("ใส่ Channel access token และ ID ผู้รับให้ครบ")
        _post("https://api.line.me/v2/bot/message/push",
              {"to": cfg["to"].strip(), "messages": [{"type": "text", "text": body[:4900]}]},
              {"Authorization": f"Bearer {cfg['token']}"})
    elif kind == "discord":
        if not (cfg.get("webhook_url") or "").startswith("https://"):
            raise NotifyError("Webhook URL ต้องขึ้นต้นด้วย https://")
        _post(cfg["webhook_url"], {"content": body[:1950]})
    elif kind == "webhook":
        if not (cfg.get("url") or "").startswith(("http://", "https://")):
            raise NotifyError("URL ปลายทางต้องขึ้นต้นด้วย http:// หรือ https://")
        payload = dict(data or {}, title=title, text=body, source="5s-vision")
        _post(cfg["url"], payload, {"X-5S-Secret": cfg["secret"]} if cfg.get("secret") else {})
    elif kind == "email_brevo":
        if not cfg.get("api_key") or not cfg.get("sender"):
            raise NotifyError("ใส่ API key และอีเมลผู้ส่งให้ครบ")
        _post("https://api.brevo.com/v3/smtp/email",
              {"sender": {"name": "5ส Vision", "email": cfg["sender"].strip()},
               "to": [{"email": e} for e in _emails(cfg.get("to"))], "subject": title, "textContent": text},
              {"api-key": cfg["api_key"], "accept": "application/json"})
    elif kind == "email_smtp":
        if not cfg.get("host") or not cfg.get("sender"):
            raise NotifyError("ใส่ SMTP host และอีเมลผู้ส่งให้ครบ")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, cfg["sender"].strip(), ", ".join(_emails(cfg.get("to")))
        msg.set_content(text)
        security = (cfg.get("security") or "starttls").strip().lower()
        try:
            port = int(cfg.get("port") or (465 if security == "ssl" else 587))
            cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
            with cls(cfg["host"].strip(), port, timeout=20) as smtp:
                if security == "starttls":
                    smtp.starttls()
                if cfg.get("username"):
                    smtp.login(cfg["username"], cfg.get("password") or "")
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError, ValueError) as e:
            raise NotifyError(f"ส่งอีเมลไม่สำเร็จ: {type(e).__name__} {str(e)[:160]} "
                              "(host ฟรีบางแห่งเช่น Render ปิดพอร์ต SMTP ให้ใช้แบบ Brevo API แทน)")
    else:
        raise NotifyError(f"ไม่รู้จักช่องทางชนิด {kind}")


# --------------------------------------------------------------------------- สร้างข้อความ
def _link(path: str) -> str:
    base = (settings_store.load().get("public_url") or "").rstrip("/")
    return base + path if base else ""


def _num(v) -> str:
    return "-" if v is None else f"{float(v):.1f}".rstrip("0").rstrip(".")


def build(db, kind: str, round_id, dept_id, events: list):
    """คืน dict(title, text, problem_text, data) หรือ None ถ้าไม่มีอะไรต้องแจ้ง"""
    rnd = db.get(Round, round_id) if round_id else None
    dept = db.get(Department, dept_id) if dept_id else None
    rname, dname = (rnd.name if rnd else "-"), (dept.name if dept else "-")
    first = events[0].payload or {}
    if kind == "system":
        return dict(title="[5ส Vision] แจ้งผู้ดูแลระบบ", text=first.get("text", ""), data=dict(event="system"))
    if kind == "action":
        return dict(title=f"[5ส Vision] งานแก้ไขของ {dname}", text=first.get("text", ""),
                    data=dict(event="action", department=dname))
    if kind == "round":
        action = first.get("action")
        if action == "remind":                 # เตือนแผนกที่ยังส่งไม่ครบก่อนปิดรอบ
            return dict(title="[5ส Vision] ใกล้ปิดรอบ ภาพยังไม่ครบ", text=first.get("text", ""),
                        data=dict(event="round", action=action, round=rname, department=dname))
        if action == "open":
            text = f"เปิดรอบการตรวจ: {rname}\nเริ่มส่งภาพได้แล้ว"
            if rnd and rnd.end_date:
                text += f" ถึงวันที่ {rnd.end_date.strftime('%d/%m/%Y')}"
            text += f"\nแต่ละแผนกต้องมีภาพอย่างน้อย {rnd.min_photos if rnd else 1} ภาพ"
            link = _link("/capture")
        else:
            text = f"ปิดรอบการตรวจ: {rname}\n" + (first.get("top") or "ดูผลและอันดับได้ในระบบ")
            link = _link(f"/ranking?round={round_id}")
        if link:
            text += f"\n{link}"
        return dict(title="[5ส Vision] รอบการตรวจ", text=text, data=dict(event="round", action=action, round=rname))
    ids = [e.photo_id for e in events if e.photo_id]
    photos = {p.id: p for p in db.query(Photo).filter(Photo.id.in_(ids)).all()} if ids else {}
    if kind == "upload":
        rows = [photos[i] for i in ids if i in photos]
        if not rows:
            return None
        people = sorted({p.uploader_name for p in rows if p.uploader_name})
        lines = [f"รอบ: {rname}", f"ส่ง {len(rows)} ภาพ โดย {', '.join(people) or '-'}"]
        lines += [f"- {p.area_name}" for p in rows[:12]]
        if len(rows) > 12:
            lines.append(f"และอีก {len(rows) - 12} ภาพ")
        link = _link(f"/photos?round={round_id}&dept={dept_id}")
        if link:
            lines.append(link)
        return dict(title=f"[5ส Vision] ภาพใหม่จาก {dname}", text="\n".join(lines),
                    data=dict(event="upload", round=rname, department=dname, count=len(rows),
                              items=[dict(id=p.id, area=p.area_name) for p in rows]))
    # ---- result: สรุปผลของชุดภาพที่เพิ่งวิเคราะห์เสร็จ
    rows = [photos[i] for i in dict.fromkeys(ids) if i in photos]
    if not rows:
        return None
    done = [p for p in rows if p.status == "done" and p.percent is not None]
    bad = [p for p in rows if p.status in ("rejected", "error")]
    lines = [f"รอบ: {rname}"]
    if done:
        avg = sum(p.percent for p in done) / len(done)
        lines.append(f"ให้คะแนนแล้ว {len(done)} ภาพ เฉลี่ย {_num(avg)}%")
        for p in sorted(done, key=lambda x: x.percent)[:10]:
            line = f"- {p.area_name}: {_num(p.percent)}%"
            if p.overridden:
                line += " (กรรมการปรับ)"
            actions = (p.analysis or {}).get("top_actions") or []
            if actions and p.percent < 80:
                line += f" ควรทำก่อน: {actions[0]}"
            lines.append(line[:300])
        if len(done) > 10:
            lines.append(f"และอีก {len(done) - 10} ภาพ")
    problem_lines = []
    for p in bad[:8]:
        why = (p.analysis or {}).get("image_issue") if p.status == "rejected" else p.error
        label = "ใช้ประเมินไม่ได้ ถ่ายใหม่" if p.status == "rejected" else "วิเคราะห์ไม่สำเร็จ"
        problem_lines.append(f"- {p.area_name}: {label} ({(why or '-')[:120]})")
    if problem_lines:
        lines.append(f"ภาพที่มีปัญหา {len(bad)} ภาพ")
        lines += problem_lines
    scored = db.query(Photo).filter(Photo.round_id == round_id, Photo.department_id == dept_id,
                                    Photo.status == "done", Photo.percent.isnot(None)).all()
    if scored:
        lines.append(f"คะแนนของแผนกตอนนี้ {_num(sum(p.percent for p in scored) / len(scored))}% จาก {len(scored)} ภาพ")
    link = _link(f"/rounds/{round_id}/dept/{dept_id}")
    if link:
        lines.append(f"เหตุผลและคำแนะนำทั้งหมด: {link}")
    problem_text = ""
    if problem_lines:
        problem_text = "\n".join([f"รอบ: {rname}", f"ภาพที่มีปัญหา {len(bad)} ภาพ"] + problem_lines + ([link] if link else []))
    return dict(title=f"[5ส Vision] ผลวิเคราะห์ของ {dname}", text="\n".join(lines), problem_text=problem_text,
                data=dict(event="result", round=rname, department=dname, scored=len(done), problems=len(bad),
                          items=[dict(id=p.id, area=p.area_name, status=p.status, percent=p.percent) for p in rows]))


# --------------------------------------------------------------------------- คิวและการรวมชุด
_wake = threading.Event()
_stop = threading.Event()
_thread = None
_dirty = True                 # ตรวจฐานข้อมูลหนึ่งครั้งตอนเริ่มระบบ
_have_channels = None         # จำไว้ในหน่วยความจำ จะได้ไม่เขียนเหตุการณ์เมื่อไม่มีช่องทางเลย
_throttle = {}


def refresh_channels(db=None):
    global _have_channels
    own = db is None
    db = db or SessionLocal()
    try:
        _have_channels = db.query(Channel).filter(Channel.active.is_(True)).count() > 0
    finally:
        if own:
            db.close()
    return _have_channels


def emit(db, kind: str, round_id=None, department_id=None, photo_id=None, payload=None, throttle_key=None,
         throttle_seconds=3600):
    """บันทึกเหตุการณ์ลงคิว (ผู้เรียกเป็นคน commit) แล้วปลุกเธรดส่ง"""
    global _dirty
    if _have_channels is None:
        refresh_channels()
    if not _have_channels:
        return
    if throttle_key:
        import time
        if time.time() - _throttle.get(throttle_key, 0) < throttle_seconds:
            return
        _throttle[throttle_key] = time.time()
    db.add(NotifyEvent(kind=kind, round_id=round_id, department_id=department_id, photo_id=photo_id,
                       payload=payload or {}))
    _dirty = True
    _wake.set()


_alert_lock = threading.Lock()
_alert_busy: set = set()            # เรื่องที่มีเธรดกำลังแจ้งอยู่ในขณะนี้


def alert_admin(db, key: str, text: str, hours: float = 20) -> bool:
    """แจ้งผู้ดูแลระบบ: บันทึกไว้ให้เห็นในหน้าจัดการระบบเสมอ และส่งไปยังช่องทางที่รับเรื่องของระบบ

    เวลาที่แจ้งครั้งล่าสุดเก็บในฐานข้อมูล host ฟรีที่หลับและตื่นวันละหลายรอบจึงไม่แจ้งเรื่องเดิมซ้ำ
    หลายเธรดพบเรื่องเดียวกันพร้อมกันได้ (เธรดคิววิเคราะห์มีหลายเธรด): เธรดแรกเป็นผู้แจ้ง เธรดที่เหลือข้าม
    """
    from datetime import datetime
    from .db import AuditLog
    stamp = f"_alert_{key}"
    with _alert_lock:                 # ถือไว้เพียงชั่วครู่ ไม่ถือระหว่างเขียนฐานข้อมูล
        if key in _alert_busy:
            return False
        _alert_busy.add(key)
    try:
        last = settings_store.load().get(stamp)
        t = now()
        if hours > 0 and last:
            try:
                if (t - datetime.fromisoformat(last)).total_seconds() < hours * 3600:
                    return False
            except ValueError:
                pass
        db.add(AuditLog(username="system", action="alert", detail=text[:2000]))
        emit(db, "system", payload={"text": text})
        settings_store.save(db, {stamp: t.isoformat()})          # commit ทั้งบันทึกและคิวแจ้งเตือน
        return True
    finally:
        with _alert_lock:
            _alert_busy.discard(key)


def _ready(db, kind, round_id, dept_id, events, t) -> bool:
    oldest = min(e.created_at for e in events)
    newest = max(e.created_at for e in events)
    if kind == "upload":
        return (t - newest).total_seconds() >= 30 or (t - oldest).total_seconds() >= 300
    if kind == "result":
        waiting = db.query(Photo).filter(Photo.round_id == round_id, Photo.department_id == dept_id,
                                         Photo.status.in_(["pending", "processing"])).count()
        return waiting == 0 or (t - oldest).total_seconds() >= 900
    return True


def _deliver(db, channels, kind, dept_id, msg) -> int:
    sent = 0
    for ch in channels:
        if dept_id is not None and ch.department_id is not None and ch.department_id != dept_id:
            continue
        wanted = set(ch.events or [])
        text = msg["text"]
        if kind == "result" and "result" not in wanted:
            if "problem" in wanted and msg.get("problem_text"):
                text = msg["problem_text"]
            else:
                continue
        elif kind != "result" and kind not in wanted:
            continue
        try:
            send(ch.kind, ch.config, msg["title"], text, msg.get("data"))
            ch.last_ok_at, ch.last_error = now(), ""
            db.add(NotifyLog(channel=ch.name, kind=kind, ok=True, detail=msg["title"]))
            sent += 1
        except NotifyError as e:
            ch.last_error = str(e)[:500]
            db.add(NotifyLog(channel=ch.name, kind=kind, ok=False, detail=str(e)[:500]))
        except Exception as e:             # ช่องทางหนึ่งพังต้องไม่ทำให้ช่องทางอื่นไม่ได้รับ
            log.exception("notify send")
            db.add(NotifyLog(channel=ch.name, kind=kind, ok=False, detail=f"ข้อผิดพลาดภายใน: {type(e).__name__}"))
    return sent


def flush(force: bool = False) -> int:
    """ส่งเหตุการณ์ที่ถึงเวลา คืนจำนวนเหตุการณ์ที่ยังรอรวมชุด"""
    with SessionLocal() as db:
        events = db.query(NotifyEvent).filter(NotifyEvent.done.is_(False)).order_by(NotifyEvent.id).limit(600).all()
        if not events:
            return 0
        channels = db.query(Channel).filter(Channel.active.is_(True)).order_by(Channel.id).all()
        groups = defaultdict(list)
        for e in events:
            key = e.kind if e.kind in ("upload", "result") else f"{e.kind}:{e.id}"
            groups[(key, e.round_id, e.department_id)].append(e)
        t, remaining = now(), 0
        for (_key, rid, did), evs in groups.items():
            kind = evs[0].kind
            if not force and not _ready(db, kind, rid, did, evs, t):
                remaining += len(evs)
                continue
            try:
                msg = build(db, kind, rid, did, evs)
                if msg and channels:
                    _deliver(db, channels, kind, did, msg)
            except Exception:
                log.exception("notify build")
            # ทำเครื่องหมายด้วยคำสั่งเดียวตามเลขที่: ถ้าผู้ดูแลเพิ่งลบรอบหรือล้างข้อมูลทดสอบระหว่างที่กำลังส่ง
            # เหตุการณ์ที่ถูกลบไปแล้วจะถูกข้ามเฉย ๆ ไม่ทำให้รอบการส่งนี้ล้ม
            ids = [e.id for e in evs]
            db.query(NotifyEvent).filter(NotifyEvent.id.in_(ids)).update({"done": True}, synchronize_session=False)
            db.commit()
        # เก็บกวาด: เหตุการณ์ที่ส่งแล้วเกิน 3 วัน และบันทึกการส่งเกิน 30 วัน
        db.query(NotifyEvent).filter(NotifyEvent.done.is_(True),
                                     NotifyEvent.created_at < t - timedelta(days=3)).delete(synchronize_session=False)
        db.query(NotifyLog).filter(NotifyLog.at < t - timedelta(days=30)).delete(synchronize_session=False)
        db.commit()
        return remaining


def _loop():
    global _dirty
    while not _stop.is_set():
        if _dirty:
            _stop.wait(3)                 # รอให้ผู้เรียก commit เสร็จก่อน
            _dirty = False                # เหตุการณ์ที่เข้ามาระหว่างส่ง จะตั้งค่านี้กลับเป็น True เอง
            try:
                if flush() > 0:
                    _dirty = True
            except Exception:
                log.exception("notify loop")
        _wake.wait(timeout=15 if _dirty else 3600)
        _wake.clear()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="fives-notify", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    _wake.set()

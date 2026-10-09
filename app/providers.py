"""สุขภาพของผู้ให้บริการ AI: เบรกเกอร์ จังหวะการเรียก และตัวนับต่อวัน

หลักคิดของไฟล์นี้: ปัญหาของผู้ให้บริการ (โควตาเต็ม, เรียกถี่เกิน, ขัดข้องชั่วคราว, ตั้งค่าผิด) ไม่ใช่ความผิดของภาพ
จึงเก็บสถานะไว้ที่ผู้ให้บริการ ไม่ใช่ที่ภาพทีละใบ เมื่อ AI หลักมีปัญหา คิวจะใช้ AI สำรองทันที
ถ้าไม่มีสำรอง ทั้งคิวจะหยุดรอพร้อมกันและบอกเหตุผล แล้วเดินต่อเองทันทีที่ผู้ให้บริการกลับมา
(รุ่นก่อนหน้านี้ให้แต่ละภาพพักรอของตัวเองได้นานถึง 1 ชั่วโมงต่อครั้ง ภาพจึงค้างได้หลายชั่วโมง)

สถานะทั้งหมดอยู่ในหน่วยความจำ: ระบบเริ่มใหม่ = เริ่มนับใหม่ ซึ่งปลอดภัย เพราะการเรียกครั้งแรกจะบอกสภาพจริงเอง
ส่วนตัวนับต่อวันเก็บในฐานข้อมูล
"""
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import ai
from .db import AiUsage, SessionLocal

SLOTS = ("ai1", "ai2", "ai3")            # ลำดับที่คิวลองเรียก: หลัก แล้วสำรองตามลำดับ
SLOT_NAMES = {"ai1": "AI หลัก", "ai2": "AI สำรอง 1", "ai3": "AI สำรอง 2"}
USAGE_COLUMNS = {"ai1": "count", "ai2": "count2", "ai3": "count3"}
RPM_KEYS = {"ai1": "ai_rpm", "ai2": "ai2_rpm", "ai3": "ai3_rpm"}
CAP_KEYS = {"ai1": "ai_daily", "ai2": "ai2_daily", "ai3": "ai3_daily"}
KIND_NAMES = {"rate": "เรียกถี่เกินโควตาต่อนาที", "quota": "โควตาของวันนี้หมด", "transient": "ขัดข้องชั่วคราว",
              "config": "การตั้งค่าไม่ถูกต้อง", "cap": "ครบเพดานต่อวันที่ตั้งไว้"}

RATE_MIN, RATE_MAX = 5.0, 120.0          # ช่วงเวลาพักเมื่อเรียกถี่เกิน (วินาที) ในสามครั้งแรก
RATE_ESCALATE_MAX = 1800.0               # ถ้ายังเกินซ้ำ ๆ ถือว่าโควตาน่าจะหมดจริง พักยาวขึ้นได้ถึงครึ่งชั่วโมง
# ผู้ให้บริการแจ้งว่าโควตาต่อวันหมด: ลองเรียกใหม่หนึ่งครั้งที่ 15 วินาที, 1, 3, 10 นาที แล้วทุกครึ่งชั่วโมง
# ครั้งแรก ๆ ถามเร็ว เพราะผู้ให้บริการแจ้งแบบนี้เป็นครั้งคราวได้ทั้งที่โควตายังเหลือ การหยุดทั้งคิวนานจากสัญญาณครั้งเดียวจึงไม่คุ้ม
# (การถามตอนโควตาหมดจริงได้ 429 กลับมา ไม่เสียโควตา) และถามทันทีเมื่อถึงเวลาที่โควตารอบใหม่เริ่ม
QUOTA_STEPS = (15.0, 60.0, 180.0, 600.0, 1800.0)
QUOTA_PROBE = QUOTA_STEPS[-1]
TRANSIENT_STEPS = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)   # ขัดข้องชั่วคราว: ลองเรียกใหม่หนึ่งครั้งตามช่วงนี้ จนกว่าจะกลับมา
TRANSIENT_OPEN_AT = 2                    # ขัดข้องติดกันกี่ครั้งจึงพักผู้ให้บริการ
CONFIG_RETRY = 900.0                     # ตั้งค่าผิด: ลองใหม่ทุก 15 นาที หรือทันทีที่ผู้ดูแลบันทึกการตั้งค่า
BAD_REQUEST_STRIKES = 3                  # 400 ติดกันกี่ภาพจึงถือว่าเป็นปัญหาของการตั้งค่า ไม่ใช่ของภาพ
PACE_MAX = 8.0                           # ชะลอจังหวะได้มากสุดกี่เท่าของที่ตั้งไว้
MAX_SLOT_WAIT = 150.0                    # รอจังหวะนานสุดต่อการเรียกหนึ่งครั้ง
TRIAL_MAX = 420.0                        # การลองเรียกหลังพักถือสิทธิ์ได้นานสุดเท่านี้ (เผื่อเธรดที่ลองหายไปกลางทาง)

_lock = threading.RLock()
_health = {}
_usage = {"day": "", "ai1": 0, "ai2": 0, "ai3": 0}
_clock = time.time                       # ชุดทดสอบเปลี่ยนนาฬิกาได้


class Health:
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        self.until = 0.0                 # พักถึงเวลานี้ (epoch)
        self.kind = ""                   # เหตุที่พัก
        self.reason = ""
        self.strikes = 0                 # ล้มเหลวระดับผู้ให้บริการติดกันกี่ครั้ง
        self.bad_requests = 0
        self.quota_strikes = 0           # ถูกแจ้งว่าโควตาต่อวันหมดติดกันกี่ครั้ง
        self.trial_until = 0.0           # มีเธรดหนึ่งกำลังลองเรียกหลังพัก เธรดอื่นรอผลก่อน
        self.pace = 1.0                  # ตัวคูณช่วงห่างระหว่างการเรียก (ปรับเองเมื่อโดน 429)
        self.next_slot = 0.0             # เวลาเร็วสุดที่เรียกครั้งถัดไปได้
        self.last_ok = None
        self.last_error = ""
        self.last_error_at = None
        self.calls = 0
        self.failures = 0


def now() -> float:
    return _clock()


def _fingerprint(cfg: dict) -> str:
    raw = "|".join([cfg.get("type", ""), cfg.get("base", ""), cfg.get("model", ""), cfg.get("key", "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def health(cfg: dict) -> Health:
    """สถานะของผู้ให้บริการในช่องนี้ ถ้าผู้ดูแลเปลี่ยน key โมเดล หรือที่อยู่ จะเริ่มนับใหม่ทันที"""
    fp = _fingerprint(cfg)
    with _lock:
        h = _health.get(cfg["slot"])
        if h is None or h.fingerprint != fp:
            h = _health[cfg["slot"]] = Health(fp)
        return h


def reset(slot: str = None):
    """ล้างสถานะพักของผู้ให้บริการ (ผู้ดูแลกด เดินคิวเดี๋ยวนี้ หรือชุดทดสอบเริ่มกรณีใหม่)"""
    with _lock:
        if slot is None:
            _health.clear()
        else:
            _health.pop(slot, None)


# --------------------------------------------------------------------------- วันของโควตา
def _pacific(dt_utc: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:                    # เครื่องที่ไม่มีฐานข้อมูลเขตเวลา: ใช้ UTC-8 คงที่ (คลาดได้ 1 ชั่วโมงช่วงฤดูร้อน)
        return dt_utc.astimezone(timezone(timedelta(hours=-8)))


def quota_day(at: float = None) -> str:
    """วันของโควตา: Gemini ตัดรอบเที่ยงคืนเวลาแปซิฟิก (บ่ายสองหรือบ่ายสามเวลาไทย แล้วแต่ฤดู)"""
    return _pacific(datetime.fromtimestamp(now() if at is None else at, timezone.utc)).strftime("%Y-%m-%d")


def quota_reset_at(at: float = None) -> float:
    """เวลา (epoch) ที่โควตารอบถัดไปเริ่ม"""
    t = now() if at is None else at
    local = _pacific(datetime.fromtimestamp(t, timezone.utc))
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return nxt.timestamp()


def thai_clock(epoch: float) -> str:
    """เวลาไทยแบบ 14:05 น. (ถ้าไม่ใช่วันนี้จะบอกวันที่ด้วย)"""
    th = timezone(timedelta(hours=7))
    d, today = datetime.fromtimestamp(epoch, th), datetime.fromtimestamp(now(), th)
    text = d.strftime("%H:%M น.")
    return text if d.date() == today.date() else d.strftime("%d/%m ") + text


# --------------------------------------------------------------------------- ตัวนับต่อวัน
def _read(session, day: str) -> dict:
    row = session.get(AiUsage, day)
    return {slot: (getattr(row, col) or 0) if row else 0 for slot, col in USAGE_COLUMNS.items()}


def load_usage(db=None) -> dict:
    """อ่านตัวนับของวันนี้จากฐานข้อมูล (เรียกตอนคิวหยิบภาพ จึงตรงกันแม้มีหลายโปรเซส)"""
    day = quota_day()
    if db is not None:
        got = _read(db, day)
    else:
        with SessionLocal() as session:
            got = _read(session, day)
    with _lock:
        _usage.update(day=day, **got)
        return dict(_usage)


def usage() -> dict:
    with _lock:
        if _usage["day"] != quota_day():
            _usage.update(day=quota_day(), **{slot: 0 for slot in SLOTS})
        return dict(_usage)


def counts(err) -> bool:
    """การเรียกครั้งนี้นับเข้าเพดานต่อวันหรือไม่: นับเฉพาะครั้งที่ AI รับงานไปทำจริง

    ครั้งที่ถูกปฏิเสธเพราะโควตา (429) ครั้งที่ขัดข้อง และครั้งที่การตั้งค่าผิด ไม่ได้ใช้โควตา จึงไม่นับ
    """
    return err is None or getattr(err, "kind", "") in ("parse", "content")


def _bump(db, day: str, slot: str) -> None:
    """เพิ่มตัวนับ 1 ครั้งด้วยคำสั่งเดียวในฐานข้อมูล: หลายเธรดหรือหลายโปรเซสนับพร้อมกันได้โดยตัวเลขไม่หาย"""
    name = USAGE_COLUMNS.get(slot, "count")
    col = getattr(AiUsage, name)
    done = db.query(AiUsage).filter(AiUsage.day == day).update({col: func.coalesce(col, 0) + 1}, synchronize_session=False)
    if not done:
        try:
            db.add(AiUsage(day=day, **{c: (1 if c == name else 0) for c in USAGE_COLUMNS.values()}))
            db.commit()
            return
        except IntegrityError:           # เธรดอื่นสร้างแถวของวันนี้ไปก่อนเสี้ยววินาที
            db.rollback()
            db.query(AiUsage).filter(AiUsage.day == day).update({col: func.coalesce(col, 0) + 1}, synchronize_session=False)
    db.commit()


def record(cfg: dict, err=None):
    """ตัวรับผลของการเรียก AI ทุกครั้ง (ส่งเป็น on_call ให้ชั้น AI)"""
    if cfg.get("type") == "demo" or not counts(err):
        return
    slot = cfg.get("slot") if cfg.get("slot") in USAGE_COLUMNS else "ai1"
    day = quota_day()
    try:
        with SessionLocal() as db:
            _bump(db, day, slot)
            got = _read(db, day)
    except Exception:                    # ตัวนับเป็นกันชน ไม่ใช่งานหลัก: พลาดได้โดยไม่ทำให้การวิเคราะห์ล้ม
        with _lock:
            if _usage["day"] == day:
                _usage[slot] += 1
        return
    with _lock:
        _usage.update(day=day, **got)


def cap_of(cfg: dict, s: dict) -> int:
    """เพดานต่อวันของช่องนี้ (0 = ไม่จำกัด)"""
    try:
        return max(0, int(s.get(CAP_KEYS.get(cfg["slot"], "ai_daily"), 0) or 0))
    except (TypeError, ValueError):
        return 0


def rpm_of(cfg: dict, s: dict) -> int:
    try:
        return max(1, min(int(s.get(RPM_KEYS.get(cfg["slot"], "ai_rpm"), 6) or 6), 120))
    except (TypeError, ValueError):
        return 6


# --------------------------------------------------------------------------- ใช้ได้ตอนนี้ไหม
def blocked(cfg: dict, s: dict):
    """คืน None ถ้าเรียกผู้ให้บริการนี้ได้ตอนนี้ ไม่เช่นนั้นคืน (เหตุ, ใช้ได้อีกทีเมื่อไร, คำอธิบาย)"""
    if cfg["type"] == "demo":
        return None
    t = now()
    cap = cap_of(cfg, s)
    name = SLOT_NAMES.get(cfg["slot"], cfg["slot"])
    if cap and usage().get(cfg["slot"], 0) >= cap:
        reset_at = quota_reset_at(t)
        return ("cap", reset_at, f"{name}ใช้ครบเพดานต่อวันที่ตั้งไว้แล้ว ({cap} ครั้ง) "
                                 f"คิวจะเดินต่อเมื่อขึ้นรอบโควตาใหม่ประมาณ {thai_clock(reset_at)} หรือเพิ่มเพดานในหน้าตั้งค่า")
    h = health(cfg)
    with _lock:
        if h.until > t:
            return (h.kind, h.until, h.reason)
        if h.trial_until > t:
            return ("trial", h.trial_until, h.reason or f"กำลังลองเรียก{name}หนึ่งครั้งหลังพัก คิวจะเดินต่อทันทีที่ได้ผล")
    return None


def begin(cfg: dict) -> bool:
    """ขอเรียกผู้ให้บริการ คืน False ถ้ามีเธรดอื่นกำลังลองเรียกหลังพักอยู่

    ผู้ให้บริการที่เพิ่งมีปัญหา ให้ลองทีละหนึ่งครั้ง: ถ้ายังไม่กลับมา จะได้ไม่ยิงซ้ำหลายครั้งพร้อมกันจากทุกเธรด
    """
    if cfg["type"] == "demo":
        return True
    h = health(cfg)
    with _lock:
        t = now()
        if h.until > t or h.trial_until > t:
            return False
        if h.strikes or h.quota_strikes:
            h.trial_until = t + TRIAL_MAX
        return True


def end(cfg: dict):
    """คืนสิทธิ์ลองเรียกโดยไม่มีผล (ระบบกำลังปิด หรือเกิดข้อผิดพลาดที่ไม่ได้มาจากผู้ให้บริการ)"""
    if cfg["type"] == "demo":
        return
    h = health(cfg)
    with _lock:
        h.trial_until = 0.0


def plan(s: dict) -> dict:
    """ภาพรวมว่าตอนนี้คิวเดินได้หรือไม่ และถ้าไม่ได้ จะเดินได้อีกทีเมื่อไร เพราะอะไร"""
    profs = ai.profiles(s)
    ready, waits = [], []
    for cfg in profs:
        b = blocked(cfg, s)
        if b is None:
            ready.append(cfg)
        else:
            waits.append((cfg, b))
    out = dict(profiles=profs, ready=ready, waits=waits, reason="", kind="", resume_at=None)
    if not profs:
        out.update(reason="ยังไม่ได้ตั้งค่า AI", kind="config")
    elif not ready:
        cfg, (kind, until, reason) = min(waits, key=lambda w: w[1][1])
        out.update(reason=reason, kind=kind, resume_at=until)
    return out


def troubled(cfg: dict) -> bool:
    """ผู้ให้บริการนี้เพิ่งมีปัญหาและยังไม่กลับมาสำเร็จ: การเรียกครั้งถัดไปเป็นแค่การลองเชิง ไม่นับเป็นความผิดของภาพ"""
    if cfg["type"] == "demo":
        return False
    with _lock:
        return health(cfg).strikes >= TRANSIENT_OPEN_AT


# --------------------------------------------------------------------------- จังหวะการเรียก
def reserve(cfg: dict, s: dict) -> float:
    """จองจังหวะเรียกครั้งถัดไป คืนจำนวนวินาทีที่ต้องรอก่อนเรียก (หลายเธรดเรียกพร้อมกันได้ ไม่เกินอัตราที่ตั้ง)"""
    if cfg["type"] == "demo":
        return 0.0
    h = health(cfg)
    with _lock:
        t = now()
        gap = 60.0 / rpm_of(cfg, s) * h.pace
        start = max(t, h.next_slot)
        h.next_slot = start + gap
        return min(start - t, MAX_SLOT_WAIT)


def success(cfg: dict):
    if cfg["type"] == "demo":
        return
    h = health(cfg)
    with _lock:
        h.calls += 1
        h.strikes = h.bad_requests = h.quota_strikes = 0
        h.trial_until = 0.0
        h.until, h.kind, h.reason = 0.0, "", ""
        h.pace = max(1.0, h.pace * 0.92)          # กลับสู่จังหวะที่ตั้งไว้ทีละน้อย
        h.last_ok = now()


def failure(cfg: dict, err: "ai.AIError") -> str:
    """บันทึกความล้มเหลว และตัดสินว่าจะพักผู้ให้บริการนี้นานเท่าไร คืนชนิดของปัญหาตามที่ตัดสินจริง

    (ชนิดอาจต่างจากที่ชั้น AI บอก: คำขอที่ถูกปฏิเสธด้วย 400 ติดกันหลายภาพ ถือเป็นปัญหาของการตั้งค่า ไม่ใช่ของภาพ)
    """
    if cfg["type"] == "demo":
        return err.kind
    h = health(cfg)
    name = SLOT_NAMES.get(cfg["slot"], cfg["slot"])
    text = str(err)[:400]
    with _lock:
        t = now()
        h.calls += 1
        h.failures += 1
        h.last_error, h.last_error_at = text, t
        h.trial_until = 0.0
        kind = err.kind
        if kind in ("rate", "quota", "config") and h.until > t and h.kind == kind:
            return kind                  # เหตุเดียวกับที่กำลังพักอยู่ (สองเธรดเรียกพร้อมกัน): ไม่นับซ้ำ ไม่ยืดเวลาพัก
        if kind == "content" and err.status == 400:
            h.bad_requests += 1
            if h.bad_requests >= BAD_REQUEST_STRIKES:      # หลายภาพติดกันถูกปฏิเสธแบบเดียวกัน = ปัญหาอยู่ที่การตั้งค่า
                kind = "config"
        if kind == "rate":
            h.strikes += 1
            h.pace = min(h.pace * 1.5, PACE_MAX)
            hint = err.retry_after if err.retry_after else 20.0
            if h.strikes <= 3:
                cool = min(max(hint + 1.0, RATE_MIN), RATE_MAX)
            else:
                cool = min(RATE_MAX * (2 ** (h.strikes - 3)), RATE_ESCALATE_MAX)
            h.until, h.kind = t + cool, "rate"
            h.reason = (f"{name}แจ้งว่าเรียกถี่เกินโควตาต่อนาที ระบบพักการเรียกและจะเดินคิวต่อเองเวลา {thai_clock(h.until)}")
        elif kind == "quota":
            h.strikes += 1
            reset_at = quota_reset_at(t)
            step = QUOTA_STEPS[min(h.quota_strikes, len(QUOTA_STEPS) - 1)]
            h.quota_strikes += 1
            cool = min(step, max(QUOTA_STEPS[0], reset_at - t))
            h.until, h.kind = t + cool, "quota"
            h.reason = (f"{name}แจ้งว่าโควตาของวันนี้หมดแล้ว โควตารอบใหม่เริ่มประมาณ {thai_clock(reset_at)} "
                        f"ระบบจะลองเรียกซ้ำให้เองเวลา {thai_clock(h.until)} และเดินคิวต่อทันทีที่เรียกได้")
        elif kind == "transient":
            h.strikes += 1
            if h.strikes >= TRANSIENT_OPEN_AT:
                cool = TRANSIENT_STEPS[min(h.strikes - TRANSIENT_OPEN_AT, len(TRANSIENT_STEPS) - 1)]
                h.until, h.kind = t + cool, "transient"
                h.reason = f"{name}ขัดข้องชั่วคราว ({text[:160]}) ระบบจะลองใหม่เองเวลา {thai_clock(h.until)}"
        elif kind == "config":
            h.strikes += 1
            h.until, h.kind = t + CONFIG_RETRY, "config"
            h.reason = (f"{name}ใช้ไม่ได้เพราะการตั้งค่า: {text[:300]} "
                        "แก้ในหน้า ตั้งค่าและ AI แล้วบันทึก คิวจะเดินต่อเองโดยไม่ต้องสั่งวิเคราะห์ใหม่")
        # parse และ content เป็นเรื่องของภาพ ไม่กระทบสถานะของผู้ให้บริการ
        return kind


# --------------------------------------------------------------------------- สำหรับหน้าจอ
def snapshot(s: dict) -> list:
    """สถานะของผู้ให้บริการแต่ละช่อง ใช้แสดงในหน้า คิววิเคราะห์"""
    out, used, t = [], usage(), now()
    for cfg in ai.profiles(s):
        h = health(cfg)
        b = blocked(cfg, s)
        with _lock:
            rpm = rpm_of(cfg, s)
            out.append(dict(
                slot=cfg["slot"], name=SLOT_NAMES.get(cfg["slot"], cfg["slot"]), type=cfg["type"], model=cfg["model"],
                ok=b is None, kind=b[0] if b else "", kind_name=KIND_NAMES.get(b[0], "") if b else "",
                until=datetime.fromtimestamp(b[1], timezone.utc).replace(tzinfo=None) if b else None,
                wait_s=max(0, int(b[1] - t)) if b else 0, reason=b[2] if b else "",
                used=used.get(cfg["slot"], 0), cap=cap_of(cfg, s), rpm=rpm,
                rpm_now=round(rpm / h.pace, 1), slowed=h.pace > 1.05,
                last_ok=datetime.fromtimestamp(h.last_ok, timezone.utc).replace(tzinfo=None) if h.last_ok else None,
                last_error=h.last_error,
                last_error_at=(datetime.fromtimestamp(h.last_error_at, timezone.utc).replace(tzinfo=None)
                               if h.last_error_at else None),
                calls=h.calls, failures=h.failures))
    return out

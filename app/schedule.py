"""ตารางเวลาถ่ายภาพอัตโนมัติของกล้อง IP

ตารางมี 3 ชั้น: ค่ากลางของทุกกล้อง < ตารางของแผนก < ตารางของกล้องเอง (หรือปิดการถ่ายอัตโนมัติเฉพาะกล้อง)
หนึ่งตาราง = เวลาคงที่ + จำนวนครั้งที่สุ่มในช่วงเวลา + วันในสัปดาห์ที่ถ่าย เวลาเป็นเวลาประเทศไทย

เวลาสุ่มของแต่ละวันคำนวณจากรหัสลับของระบบ วันที่ และเลขกล้อง จึงได้ค่าเดิมทุกครั้งที่คำนวณในวันเดียวกัน
(โปรแกรมกล้องปิดแล้วเปิดใหม่ก็ไม่ถ่ายซ้ำ) แต่คนที่ไม่รู้รหัสลับเดาเวลาไม่ได้
"""
import hashlib
import random
import re
from datetime import date, timedelta

from . import settings_store
from .db import now

DAY_NAMES = ["จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา."]
_HHMM = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def thai_now():
    return now() + timedelta(hours=7)


def _hhmm(text: str):
    m = _HHMM.match(text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def normalize(times="", n_random=0, between="", days=None) -> dict:
    """รับค่าจากฟอร์มหรือค่าที่เก็บไว้ คืนตารางที่ตรวจแล้ว"""
    if isinstance(times, str):
        times = re.split(r"[,\s]+", times.strip())
    fixed = sorted({t for t in (_hhmm(x) for x in times or []) if t})
    a, _, b = (between or "").partition("-")
    lo, hi = _hhmm(a) or "08:30", _hhmm(b) or "16:30"
    if _minutes(hi) <= _minutes(lo):
        lo, hi = "08:30", "16:30"
    try:
        n = max(0, min(24, int(n_random or 0)))
    except (TypeError, ValueError):
        n = 0
    n = min(n, max(0, (_minutes(hi) - _minutes(lo)) // 5))          # สุ่มห่างกันอย่างน้อย 5 นาที
    try:
        keep = sorted({int(d) for d in (days if days is not None else range(7)) if 0 <= int(d) <= 6})
    except (TypeError, ValueError):
        keep = [0, 1, 2, 3, 4]
    return dict(times=fixed[:24], random=n, between=f"{lo}-{hi}", days=keep)


def from_form(form, prefix: str = "") -> dict:
    return normalize(form.get(prefix + "times") or "", form.get(prefix + "random") or 0, form.get(prefix + "between") or "",
                     form.getlist(prefix + "days"))


def is_empty(sched) -> bool:
    return not sched or (not sched.get("times") and not sched.get("random")) or not sched.get("days")


def effective(mode: str, own, dept, default):
    """ตารางที่ใช้จริงของกล้อง คืน (ตาราง หรือ None ถ้าไม่ถ่ายอัตโนมัติ, ที่มา)"""
    if mode == "off":
        return None, "off"
    if mode == "own":
        return (None if is_empty(own) else normalize(**_kw(own))), "camera"
    if dept is not None:
        return (None if is_empty(dept) else normalize(**_kw(dept))), "department"
    return (None if is_empty(default) else normalize(**_kw(default))), "default"


def _kw(s: dict) -> dict:
    return dict(times=s.get("times") or [], n_random=s.get("random") or 0, between=s.get("between") or "", days=s.get("days"))


def secret() -> str:
    return str(settings_store.load().get("_sched_secret") or "5s-vision")


def holidays() -> set:
    return {str(x).strip() for x in (settings_store.load().get("cam_holidays") or []) if str(x).strip()}


def times_for(cam_id: int, sched, day: date) -> list:
    """เวลาถ่ายของกล้องในวันนั้น (HH:MM เรียงแล้ว) วันหยุดและวันที่ไม่อยู่ในตารางคืนรายการว่าง"""
    if not sched or day.weekday() not in (sched.get("days") or []) or day.isoformat() in holidays():
        return []
    out = set(sched.get("times") or [])
    n = int(sched.get("random") or 0)
    if n > 0:
        lo, hi = (_minutes(x) for x in sched["between"].split("-"))
        slots = list(range(lo, hi, 5))
        rnd = random.Random(hashlib.sha256(f"{secret()}|{cam_id}|{day.isoformat()}".encode()).digest())
        for m in rnd.sample(slots, min(n, len(slots))):
            m += rnd.randrange(5)                                    # ไม่ให้ลงท้ายด้วย 0 หรือ 5 เสมอ
            out.add(f"{m // 60:02d}:{m % 60:02d}")
    return sorted(out)


def describe(sched) -> str:
    """ข้อความสั้น ๆ ของตาราง สำหรับหน้าจัดการ (ไม่บอกเวลาสุ่มจริง)"""
    if is_empty(sched):
        return "ไม่ถ่ายอัตโนมัติ"
    parts = []
    if sched.get("times"):
        parts.append("เวลา " + ", ".join(sched["times"]))
    if sched.get("random"):
        parts.append(f"สุ่ม {sched['random']} ครั้งระหว่าง {sched['between']}")
    days = sched.get("days") or []
    which = "ทุกวัน" if len(days) == 7 else "จ. ถึง ศ." if days == [0, 1, 2, 3, 4] else " ".join(DAY_NAMES[d] for d in days)
    return " และ".join(parts) + f" ({which})"


def due(times: list, now_hhmm: str, done: set, key: tuple, grace_min: int) -> list:
    """เวลาที่ถึงกำหนดแล้วและยังไม่ได้ถ่าย: คืน [(เวลา, ถ่ายหรือข้ามเพราะเลยมานานเกิน)]"""
    out, cur = [], _minutes(now_hhmm)
    for t in times:
        if key + (t,) in done or _minutes(t) > cur:
            continue
        out.append((t, cur - _minutes(t) <= grace_min))
    return out

"""รอบการตรวจอัตโนมัติ: เปิดตามวันเริ่ม ปิดเมื่อพ้นวันสิ้นสุด และสร้างรอบถัดไปเองตามรอบสัปดาห์หรือรอบเดือน

งานนี้ทำได้ 2 ทาง: ตัวตั้งเวลาเบื้องหลัง (เมื่อบริการตื่นอยู่) และตอนมีคนเข้าใช้ (ensure) บริการบน cloud ฟรีที่หลับอยู่
ตอนเที่ยงคืนจึงยังได้สถานะที่ถูกต้องทันทีที่มีคนเปิดใช้ครั้งถัดไป การตรวจว่าถึงเวลาหรือยังใช้ค่าในหน่วยความจำ ไม่แตะฐานข้อมูล
"""
import calendar
from datetime import date, timedelta

from . import cameras, notify, scoring, settings_store
from .db import DATA_VERSION, Round, checklist_snapshot, log, now, rubric_snapshot

THAI_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน",
               "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
_seen = {"day": None, "version": -1}


def today() -> date:
    return (now() + timedelta(hours=7)).date()


def _period(start: date, repeat: str) -> date:
    """วันสิ้นสุดของรอบที่เริ่มวันนั้น: รายสัปดาห์ = 7 วัน, รายเดือน = ถึงสิ้นเดือน (หรือครบ 1 เดือนถ้าไม่ได้เริ่มวันที่ 1)"""
    if repeat == "weekly":
        return start + timedelta(days=6)
    if start.day == 1:
        return start.replace(day=calendar.monthrange(start.year, start.month)[1])
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return date(y, m, min(start.day, calendar.monthrange(y, m)[1])) - timedelta(days=1)


def round_name(prefix: str, start: date, end: date, repeat: str) -> str:
    if repeat == "monthly" and start.day == 1:
        return f"{prefix} {THAI_MONTHS[start.month]} {start.year + 543}"
    return f"{prefix} {start.strftime('%d/%m')}-{end.strftime('%d/%m/%Y')}"


def _close(db, rnd: Round):
    rnd.status, rnd.closed_at = "closed", now()
    top = scoring.round_ranking(db, rnd, with_prev=False)["ranked"][:3]
    text = "\n".join(f"อันดับ {r['rank']}: {r['dept'].name} {notify._num(r['avg'])}%" for r in top)
    notify.emit(db, "round", round_id=rnd.id, payload={"action": "close", "top": text})
    log(db, "system", "auto_close_round", f"{rnd.name} พ้นวันสิ้นสุด {rnd.end_date}")


def apply(db, day: date = None) -> dict:
    """ทำให้สถานะของรอบตรงกับวันที่ คืนสิ่งที่เปลี่ยน"""
    day = day or today()
    s = settings_store.load()
    out = dict(opened=[], closed=[], created=[])
    for rnd in db.query(Round).filter(Round.status == "open", Round.auto_close.is_(True), Round.end_date.isnot(None),
                                      Round.end_date < day).order_by(Round.id).all():
        _close(db, rnd)
        out["closed"].append(rnd.name)
    for rnd in db.query(Round).filter(Round.status == "planned", Round.start_date.isnot(None),
                                      Round.start_date <= day).order_by(Round.id).all():
        if rnd.end_date is not None and rnd.end_date < day and rnd.auto_close:
            rnd.status, rnd.closed_at = "closed", now()          # ช่วงของรอบผ่านไปแล้วทั้งช่วงขณะที่ระบบไม่ได้ทำงาน
            log(db, "system", "skip_round", f"{rnd.name}: พ้นช่วงของรอบไปแล้ว จึงไม่เปิด")
            continue
        rnd.status, rnd.closed_at = "open", None
        notify.emit(db, "round", round_id=rnd.id, payload={"action": "open"})
        log(db, "system", "auto_open_round", f"{rnd.name} ถึงวันเริ่ม {rnd.start_date}")
        out["opened"].append(rnd.name)
    db.flush()                                                     # ให้การเปิดปิดข้างบนมีผลกับการนับด้านล่าง
    repeat = s.get("rounds_repeat", "off")
    if repeat in ("weekly", "monthly") and not db.query(Round).filter(Round.status.in_(["open", "planned"])).count():
        base = db.query(Round).filter(Round.end_date.isnot(None)).order_by(Round.end_date.desc(), Round.id.desc()).first()
        if base is not None and base.end_date < day:
            start = base.end_date + timedelta(days=1)
            end = _period(start, repeat)
            while end < day:                                       # ข้ามช่วงที่ผ่านไปแล้วทั้งช่วง ไม่สร้างรอบว่าง
                start = end + timedelta(days=1)
                end = _period(start, repeat)
            rubric = rubric_snapshot(db)
            mode = base.mode or "level"
            checklist = checklist_snapshot(db) if mode == "checklist" else None
            if mode == "checklist" and not checklist:
                mode = "level"
            if rubric:
                name = round_name(s.get("rounds_repeat_prefix") or "ตรวจ 5ส", start, end, repeat)
                if db.query(Round).filter(Round.name == name).count():
                    name += f" ({start.isoformat()})"
                rnd = Round(name=name[:160], note="สร้างโดยระบบตามรอบอัตโนมัติ", start_date=start, end_date=end,
                            status="open" if start <= day else "planned", min_photos=base.min_photos, rubric=rubric,
                            mode=mode, checklist=checklist, rule_rev=int(s.get("_rule_rev", 0) or 0),
                            auto_open=True, auto_close=True)
                db.add(rnd)
                db.flush()
                if rnd.status == "open":
                    notify.emit(db, "round", round_id=rnd.id, payload={"action": "open"})
                log(db, "system", "auto_create_round", f"{name} ({start} ถึง {end})")
                out["created"].append(name)
    if any(out.values()):
        db.commit()
        cameras.invalidate()
    return out


def ensure(db) -> None:
    """เรียกได้บ่อย: ทำงานจริงเมื่อขึ้นวันใหม่ หรือเมื่อข้อมูลในระบบเปลี่ยนตั้งแต่ครั้งก่อน (เช่น มีการแก้วันของรอบ)"""
    day, version = today(), DATA_VERSION["n"]
    if _seen["day"] == day and _seen["version"] == version:
        return
    apply(db, day)
    _seen["day"], _seen["version"] = day, DATA_VERSION["n"]

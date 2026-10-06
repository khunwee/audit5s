"""งานแก้ไข (corrective actions): สร้างจากข้อที่ไม่ผ่านซึ่งยืนยันแล้ว ติดตามผู้รับผิดชอบและกำหนดเสร็จ และปิดเองเมื่อภาพหลังแก้ไขผ่าน"""
from datetime import timedelta

from . import notify, settings_store
from .db import Action, Photo, log, now


def today():
    return (now() + timedelta(hours=7)).date()


def is_overdue(a: Action) -> bool:
    return a.status == "open" and a.due_date is not None and a.due_date < today()


def create_for_photo(db, photo: Photo, user, s: dict) -> int:
    """ภาพที่ยืนยันผลแล้ว: สร้างงานแก้ไข 1 งานต่อข้อที่ไม่ผ่าน (ไม่สร้างซ้ำ) ใช้ได้กับโหมดรายการตรวจ"""
    a = photo.analysis or {}
    if a.get("mode") != "checklist" or photo.status != "done":
        return 0
    have = {x.check_code for x in db.query(Action).filter(Action.photo_id == photo.id).all()}
    who = user if isinstance(user, str) else (user.full_name or user.username)
    made = []
    for c in a.get("checks", []):
        if c.get("status") not in ("minor", "major") or c["code"] in have:
            continue
        days = int(s.get("action_due_days_major", 3) if c["status"] == "major" else s.get("action_due_days", 7))
        act = Action(round_id=photo.round_id, department_id=photo.department_id, area_name=photo.area_name,
                     photo_id=photo.id, check_code=c["code"], severity=c["status"],
                     title=(c.get("action") or f"แก้ไขให้เป็นไปตามข้อ: {c['text']}")[:400],
                     detail=f"ข้อ {c['code']} {c['text']}\nสิ่งที่พบ: {c.get('evidence', '')}"[:2000],
                     due_date=today() + timedelta(days=max(0, days)), created_by=who)
        db.add(act)
        made.append(act)
    if made:
        lines = [f"- {x.title[:160]} (กำหนดเสร็จ {x.due_date.strftime('%d/%m/%Y')})" for x in made[:8]]
        link = (settings_store.load().get("public_url") or "").rstrip("/")
        text = f"จุดตรวจ {photo.area_name}: มีงานแก้ไขใหม่ {len(made)} งาน\n" + "\n".join(lines)
        if link:
            text += f"\n{link}/actions?dept={photo.department_id}"
        notify.emit(db, "action", round_id=photo.round_id, department_id=photo.department_id, payload={"text": text})
        log(db, user, "create_actions", f"ภาพ {photo.id} {photo.area_name}: {len(made)} งาน")
    return len(made)


def close_fixed(db, after: Photo) -> int:
    """ภาพหลังแก้ไขได้คะแนนแล้ว: ปิดงานแก้ไขของภาพเดิมสำหรับข้อที่ตอนนี้ผ่าน"""
    status = {c["code"]: c["status"] for c in (after.analysis or {}).get("checks", [])}
    n = 0
    for act in db.query(Action).filter(Action.photo_id == after.after_of, Action.status == "open").all():
        if status.get(act.check_code) == "ok":
            act.status, act.closed_at, act.closed_by = "done", now(), "ระบบ"
            act.close_note = f"ภาพหลังแก้ไขเลขที่ {after.id} ผ่านข้อนี้แล้ว"
            act.after_photo_id = after.id
            n += 1
    return n


def overdue_reminders(db) -> int:
    """แจ้งแผนกวันละครั้งเมื่อมีงานแก้ไขเกินกำหนด (เวลาที่แจ้งล่าสุดเก็บในฐานข้อมูล)"""
    s = settings_store.load()
    day, sent, by = today(), 0, {}
    for act in db.query(Action).filter(Action.status == "open", Action.due_date.isnot(None), Action.due_date < day).all():
        by.setdefault(act.department_id, []).append(act)
    link = (s.get("public_url") or "").rstrip("/")
    for dept_id, acts in by.items():
        key = f"_stamp_actions_{dept_id}"
        if s.get(key) == day.isoformat():
            continue
        lines = [f"- {a.area_name}: {a.title[:140]} (เกินกำหนด {(day - a.due_date).days} วัน)" for a in acts[:8]]
        text = f"งานแก้ไขเกินกำหนด {len(acts)} งาน\n" + "\n".join(lines)
        if link:
            text += f"\n{link}/actions?dept={dept_id}&status=overdue"
        notify.emit(db, "action", department_id=dept_id, payload={"text": text})
        settings_store.save(db, {key: day.isoformat()})
        sent += 1
    return sent

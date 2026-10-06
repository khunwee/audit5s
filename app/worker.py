"""คิววิเคราะห์ภาพ: เธรดเดียว เรียก AI ทีละภาพตามอัตราที่ตั้งไว้ คิวอยู่ในฐานข้อมูลจึงไม่หายเมื่อ host หลับ"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from . import ai, notify, settings_store, storage
from .db import AiUsage, Photo, PhotoImage, Round, SessionLocal, now

log = logging.getLogger("fives.worker")
_wake = threading.Event()
_stop = threading.Event()
_thread = None
state = {"last_error": "", "last_ok": None, "paused_reason": "", "idle": 3600.0}


def quota_day() -> str:
    """วันของโควตา: Gemini ตัดรอบเที่ยงคืนเวลาแปซิฟิก (ประมาณบ่ายสองถึงบ่ายสามเวลาไทย)"""
    return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def usage_today(db) -> int:
    row = db.get(AiUsage, quota_day())
    return row.count if row else 0


def count_call():
    with SessionLocal() as db:
        row = db.get(AiUsage, quota_day())
        if row is None:
            db.add(AiUsage(day=quota_day(), count=1))
        else:
            row.count += 1
        db.commit()


def wake():
    _wake.set()


def apply_result(photo: Photo, result: dict, provider: str, model: str):
    """บันทึกผลที่ตรวจแล้วลงในภาพ (ใช้ทั้งจาก AI และจากการปรับคะแนนด้วยมือ)"""
    photo.analysis = result
    photo.provider, photo.model = provider, model
    photo.analyzed_at = now()
    photo.error = ""
    photo.next_try_at = None
    if not result.get("image_ok", True):
        photo.status = "rejected"
        photo.score = photo.max_score = photo.percent = None
        return
    photo.score, photo.max_score, photo.percent = ai.totals(result["criteria"])
    photo.status = "done" if photo.percent is not None else "rejected"


def process_one() -> bool:
    """วิเคราะห์ภาพถัดไปในคิว 1 ภาพ คืน True เมื่อได้เรียก AI (ต้องเว้นจังหวะก่อนภาพถัดไป)

    เมื่อไม่มีงาน จะบอกผ่าน state["idle"] ว่าควรตรวจคิวอีกทีเมื่อไร เธรดจึงไม่ต้องถามฐานข้อมูลถี่ ๆ
    (สำคัญกับ Postgres ฟรีแบบ Neon ที่นับชั่วโมงการทำงานของฐานข้อมูล)
    """
    s = settings_store.load()
    state["idle"] = 3600.0
    if not ai.is_configured(s):
        state["paused_reason"] = "ยังไม่ได้ตั้งค่า AI"
        return False
    with SessionLocal() as db:
        if usage_today(db) >= int(s["ai_daily"]):
            state["paused_reason"] = "ใช้ครบเพดานต่อวันที่ตั้งไว้แล้ว คิวจะเดินต่อเมื่อขึ้นรอบโควตาใหม่"
            state["idle"] = 900.0
            waiting = db.query(func.count(Photo.id)).filter(Photo.status == "pending").scalar()
            if waiting:
                notify.alert_admin(db, "ai_cap", f"AI ใช้ครบเพดาน {s['ai_daily']} ครั้งของวันนี้แล้ว มี {waiting} ภาพรอในคิว "
                                                 "คิวจะเดินต่อเองเมื่อขึ้นรอบโควตาใหม่ หรือเพิ่มเพดานในหน้าตั้งค่าถ้าโควตาของ key ยังเหลือ", 20)
            return False
        state["paused_reason"] = ""
        t = now()
        photo = (db.query(Photo).filter(Photo.status == "pending")
                 .filter((Photo.next_try_at.is_(None)) | (Photo.next_try_at <= t))
                 .order_by(Photo.id).first())
        if photo is None:
            due = db.query(func.min(Photo.next_try_at)).filter(Photo.status == "pending").scalar()
            if due is not None:
                state["idle"] = max(5.0, (due - t).total_seconds() + 1)
            return False
        photo.status = "processing"
        db.commit()
        try:
            blob = db.get(PhotoImage, photo.id)
            rnd = db.get(Round, photo.round_id)
            if blob is None:
                raise ai.AIError("ภาพเต็มถูกลบไปแล้ว จึงวิเคราะห์ใหม่ไม่ได้", retryable=False)
            meta = dict(area_type=photo.area_type, area_name=photo.area_name, note=photo.note)
            if photo.area_id:
                from .db import AuditArea
                area = db.get(AuditArea, photo.area_id)
                meta["standard"] = area.standard if area is not None else ""
            result, provider, model = ai.analyze(blob.data, list(rnd.rubric or []), meta, s, on_call=count_call)
            review = False
            if int(s.get("ai_passes", 1)) >= 2 and provider != "demo" and result.get("image_ok", True):
                try:      # รอบที่สองล้มเหลวไม่เป็นไร ใช้ผลรอบแรก
                    second, _, _ = ai.analyze(blob.data, list(rnd.rubric or []), meta, s, on_call=count_call)
                    result, review = ai.merge_passes(result, second)
                except ai.AIError:
                    pass
            apply_result(photo, result, provider, model)
            photo.review_flag = review
            photo.verified_by, photo.verified_at = "", None       # ผลใหม่ต้องให้คนยืนยันใหม่
            photo.overridden = False
            photo.override_by = photo.override_note = ""
            photo.override_at = None
            state["last_ok"], state["last_error"] = now(), ""
        except ai.AIError as e:
            photo.attempts += 1
            state["last_error"] = str(e)
            if e.retryable and photo.attempts < int(s["ai_max_attempts"]):
                delay = e.retry_after or min(30 * (2 ** photo.attempts), 900)
                photo.status = "pending"
                photo.next_try_at = now() + timedelta(seconds=delay)
                photo.error = f"จะลองใหม่อัตโนมัติ: {e}"
            else:
                photo.status = "error"
                photo.error = str(e)
        except Exception as e:                       # ไม่ให้คิวค้างเพราะข้อผิดพลาดที่ไม่คาดคิด
            log.exception("analyze failed")
            photo.status = "error"
            photo.error = f"ข้อผิดพลาดภายในระบบ: {type(e).__name__}"
            state["last_error"] = photo.error
        if photo.status in ("done", "rejected", "error"):
            notify.emit(db, "result", round_id=photo.round_id, department_id=photo.department_id, photo_id=photo.id)
            if photo.status == "error":
                db.commit()
                notify.alert_admin(db, "ai_error", f"AI วิเคราะห์ภาพไม่สำเร็จ: {photo.error[:300]} "
                                                   "ตรวจการตั้งค่า AI และโควตาในหน้าจัดการระบบ", 1)
        db.commit()
        return True


def housekeeping() -> dict:
    """งานดูแลประจำ (ทุก 6 ชั่วโมงขณะระบบทำงาน): ลบภาพตามนโยบาย ตรวจพื้นที่ เตือนให้สำรอง เตือนรอบใกล้ปิด"""
    from datetime import timedelta as _td
    from . import scoring
    out = {}
    with SessionLocal() as db:
        s = settings_store.load()
        out["cleanup"] = storage.auto_cleanup(db, s)
        out["alerts"] = storage.check_alerts(db, settings_store.load())
        out["backup"] = storage.backup_reminders(db, settings_store.load())
        today = (now() + _td(hours=7)).date()
        out["closing"] = 0
        for rnd in db.query(Round).filter(Round.status == "open").all():
            if rnd.end_date is None or not 0 <= (rnd.end_date - today).days <= 2:
                continue
            rk = scoring.round_ranking(db, rnd, with_prev=False)
            late = [r["dept"].name for r in rk["unranked"] + rk["idle"]]
            if late:
                left = (rnd.end_date - today).days
                when = "วันนี้" if left == 0 else f"อีก {left} วัน"
                if notify.alert_admin(db, f"closing_{rnd.id}", f"รอบ \"{rnd.name}\" จะสิ้นสุด{when} "
                                      f"แผนกที่ภาพยังไม่ครบ: {', '.join(late[:20])}", 20):
                    out["closing"] += 1
        db.commit()
    return out


def recover_stuck():
    with SessionLocal() as db:
        db.query(Photo).filter(Photo.status == "processing").update({"status": "pending"})
        db.commit()


def _loop():
    recover_stuck()
    last_cleanup = 0.0
    while not _stop.is_set():
        worked = False
        try:
            if time.time() - last_cleanup > 6 * 3600:
                last_cleanup = time.time()
                housekeeping()
            worked = process_one()
        except Exception:
            log.exception("worker loop")
        if worked:
            rpm = max(1, min(int(settings_store.load().get("ai_rpm", 6)), 60))
            _stop.wait(60.0 / rpm)
        else:
            _wake.wait(timeout=state["idle"])
            _wake.clear()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="fives-worker", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    _wake.set()

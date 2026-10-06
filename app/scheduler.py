"""ตัวตั้งเวลาเบื้องหลัง: ถ่ายภาพจากกล้องแบบ direct ตามตาราง และเปิดหรือปิดรอบตามวันที่

กล้องแบบ agent ไม่ได้ถ่ายที่นี่: โปรแกรมกล้องบนเครื่องในโรงงานรับตารางจากระบบแล้วถ่ายเอง
รายการกล้องและตารางเก็บในหน่วยความจำ ตัวตั้งเวลาจึงแตะฐานข้อมูลเฉพาะตอนถึงเวลาถ่ายหรือขึ้นวันใหม่
"""
import logging
import threading

from . import cameras, notify, photos as intake, rounds_auto, schedule, settings_store
from .db import Camera, Department, Round, SessionLocal, now

log = logging.getLogger("fives.scheduler")
_stop = threading.Event()
_thread = None
done = set()                 # (วันที่, เลขกล้อง, เวลา) ที่ถ่ายหรือข้ามไปแล้ว


def effective_map(db) -> dict:
    """ตารางที่ใช้จริงของทุกกล้องที่เปิดใช้: {เลขกล้อง: (กล้อง, ตารางหรือ None, ที่มา)}"""
    default = settings_store.load().get("cam_schedule")
    depts = {d.id: d.cam_schedule for d in db.query(Department).filter(Department.active.is_(True)).all()}
    out = {}
    for cam in db.query(Camera).filter(Camera.active.is_(True)).order_by(Camera.id).all():
        if cam.department_id not in depts:               # แผนกถูกปิดใช้: กล้องยังอยู่ แต่ไม่ถ่ายอัตโนมัติ
            out[cam.id] = (cam, None, "off")
            continue
        sched, source = schedule.effective(cam.sched_mode or "inherit", cam.schedule, depts.get(cam.department_id), default)
        out[cam.id] = (cam, sched, source)
    return out


def _direct_cache(db_factory) -> list:
    if cameras.direct["cache"] is None:
        with db_factory() as db:
            cameras.direct["cache"] = [(cid, sched) for cid, (cam, sched, _) in effective_map(db).items()
                                       if cam.mode == "direct" and sched]
    return cameras.direct["cache"]


def capture_direct(db, cam: Camera, rnd: Round, note: str, uploader_id=None, uploader_name=None):
    """ดึงภาพจากกล้องแบบ direct แล้วบันทึกเข้ารอบ คืนภาพ หรือโยน CameraError"""
    try:
        raw = cameras.grab(cam)
    except cameras.CameraError as e:
        cam.last_error = str(e)[:500]
        db.commit()
        raise
    p = intake.create_photo(db, settings_store.load(), rnd=rnd, department_id=cam.department_id, raw=raw,
                            area_name=cam.area_name or cam.name, area_type=cam.area_type, note=note,
                            uploader_id=uploader_id, uploader_name=uploader_name or f"กล้อง {cam.name}", source="ipcam",
                            camera_id=cam.id)
    cam.last_capture_at, cam.last_error = now(), ""
    db.commit()
    return p


def camera_failed(db, cam: Camera, reason: str):
    """แจ้งผู้ดูแลเมื่อกล้องถ่ายตามตารางไม่สำเร็จ (ไม่ถี่กว่า 6 ชั่วโมงต่อกล้อง)"""
    notify.alert_admin(db, f"camera_{cam.id}", f"กล้อง {cam.name} ({cam.department.name}) ถ่ายตามตารางไม่สำเร็จ: {reason[:300]} "
                                               "ตรวจกล้อง เครือข่าย และการตั้งค่าที่หน้า จัดการ > กล้อง", 6)


def stale_cameras(db, when=None) -> int:
    """กล้องที่มีตารางเวลาแต่ไม่ได้ส่งภาพตั้งแต่เวลาตามตารางครั้งล่าสุด (เช่น โปรแกรมกล้องถูกปิด เครื่องดับ กล้องเสีย)"""
    from datetime import datetime, timedelta
    when = when or schedule.thai_now()
    rnd = db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).first()
    if rnd is None:
        return 0
    opened = (rnd.created_at or now()) + timedelta(hours=7)
    n = 0
    for cid, (cam, sched, _) in effective_map(db).items():
        if not sched:
            continue
        expected = []
        for day in (when.date() - timedelta(days=1), when.date()):
            for t in schedule.times_for(cid, sched, day):
                at = datetime.combine(day, datetime.strptime(t, "%H:%M").time())
                if opened <= at <= when - timedelta(hours=1):
                    expected.append(at)
        if not expected:
            continue
        last = cam.last_capture_at + timedelta(hours=7) if cam.last_capture_at else None
        if last is None or last < max(expected) - timedelta(minutes=5):
            who = "โปรแกรมกล้องบนเครื่องในโรงงาน" if cam.mode == "agent" else "เครื่องที่รันระบบ"
            if notify.alert_admin(db, f"camera_stale_{cid}", f"กล้อง {cam.name} ({cam.department.name}) ไม่ได้ส่งภาพตามตารางเวลา "
                                  f"{max(expected).strftime('%d/%m %H:%M')} ตรวจว่า{who}เปิดอยู่และต่อเครือข่ายได้"
                                  + (f" ข้อผิดพลาดล่าสุด: {cam.last_error[:200]}" if cam.last_error else ""), 24):
                n += 1
    return n


def tick(when=None, db_factory=SessionLocal) -> dict:
    """หนึ่งรอบของตัวตั้งเวลา (ชุดทดสอบเรียกตรงพร้อมเวลาที่กำหนด) คืนสิ่งที่ทำ"""
    when = when or schedule.thai_now()
    day, hhmm = when.date(), when.strftime("%H:%M")
    out = dict(captured=[], failed=[], skipped=[])
    if rounds_auto._seen["day"] != day or rounds_auto._seen["version"] != rounds_auto.DATA_VERSION["n"]:
        with db_factory() as db:
            rounds_auto.ensure(db)
    grace = int(settings_store.load().get("cam_grace_min", 20) or 20)
    todo = []
    for cid, sched in _direct_cache(db_factory):
        for t, fresh in schedule.due(schedule.times_for(cid, sched, day), hhmm, done, (day.isoformat(), cid), grace):
            done.add((day.isoformat(), cid, t))
            (todo if fresh else out["skipped"]).append((cid, t))
    if not todo:
        return out
    with db_factory() as db:
        rnd = db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).first()
        for cid, t in todo:
            cam = db.get(Camera, cid)
            if cam is None or not cam.active or rnd is None:
                out["skipped"].append((cid, t))
                continue
            try:
                p = capture_direct(db, cam, rnd, f"กล้อง {cam.name} ถ่ายตามตาราง {t}")
                out["captured"].append((cid, t, p.id))
            except cameras.CameraError as e:
                camera_failed(db, cam, str(e))
                out["failed"].append((cid, t))
            except Exception as e:                    # เช่น พื้นที่เต็ม ภาพซ้ำ: ไม่ให้กล้องตัวเดียวหยุดทั้งรอบ
                db.rollback()
                detail = getattr(e, "detail", None) or str(e)
                log.warning("scheduled capture failed for camera %s: %s", cid, detail)
                out["failed"].append((cid, t))
    if len(done) > 5000:
        today = day.isoformat()
        for key in [k for k in done if k[0] != today]:
            done.discard(key)
    return out


def _loop():
    while not _stop.wait(20):
        try:
            tick()
        except Exception:
            log.exception("scheduler tick failed")


def start():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="fives-scheduler", daemon=True)
        _thread.start()


def stop():
    _stop.set()

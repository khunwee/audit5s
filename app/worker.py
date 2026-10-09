"""คิววิเคราะห์ภาพ

คิวอยู่ในฐานข้อมูล (คอลัมน์ status ของภาพ) จึงไม่หายเมื่อ host หลับหรือเริ่มใหม่ ส่วนที่อยู่ในไฟล์นี้คือผู้ทำงานของคิว

หลักการออกแบบ (รุ่น 1.8)
1. หยิบภาพแบบ atomic: UPDATE ... WHERE status = 'pending' ภาพหนึ่งใบจึงถูกวิเคราะห์โดยเธรดเดียวเสมอ
2. ไม่ถือการเชื่อมต่อฐานข้อมูลระหว่างรอ AI ตอบ: หยิบภาพแล้วปิด session เรียก AI แล้วเปิด session ใหม่มาบันทึกผล
   ฐานข้อมูลบนคลาวด์ที่ตัดการเชื่อมต่อที่ว่างนาน ๆ จึงไม่ทำให้ภาพค้างกลางทาง
3. ปัญหาของผู้ให้บริการ AI (โควตา, เรียกถี่, ขัดข้อง, ตั้งค่าผิด) เก็บที่ผู้ให้บริการ (ดู providers.py)
   ภาพไม่ถูกลงโทษด้วยการพักรอนาน ๆ ทีละใบ และไม่เสียจำนวนครั้งที่ลองได้
4. ภาพที่ล้มเหลวเพราะตัวมันเองเท่านั้นจึงนับครั้ง และรอสั้น ๆ (10 วินาที ถึง 5 นาที) ก่อนลองใหม่
5. มีผู้เฝ้าคิว: คืนภาพที่ค้างสถานะกำลังวิเคราะห์ ปลุกเธรดที่ตาย เตือนผู้ดูแลเมื่อภาพรอนาน
   และกัน host ฟรีไม่ให้หลับขณะคิวยังไม่หมด (ดู keepalive.py)
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from . import ai, imaging, keepalive, notify, providers, rules, settings_store, storage
from .db import DATA_VERSION, AiUsage, AuditArea, Photo, PhotoImage, Round, SessionLocal, now

log = logging.getLogger("fives.worker")

IDLE_MAX = 3600.0                 # คิวว่าง: ไม่ถามฐานข้อมูลเลยจนกว่าจะมีคนปลุก (Postgres ฟรีนับชั่วโมงทำงานของฐานข้อมูล)
BLOCKED_MAX = 900.0               # คิวหยุดรอผู้ให้บริการ: ตื่นมาดูอย่างช้าทุก 15 นาที
BACKOFF = (10.0, 30.0, 60.0, 120.0, 300.0)   # ภาพที่ล้มเหลวเพราะตัวมันเอง: รอเท่านี้ก่อนลองครั้งถัดไป
STALE_AFTER = 20 * 60             # ภาพที่อยู่ในสถานะกำลังวิเคราะห์นานเกินนี้ถือว่าค้าง คืนเข้าคิว
ORPHAN_AFTER = 3 * 60             # ตอนผู้ดูแลกด เดินคิวเดี๋ยวนี้: ภาพที่ไม่มีเธรดใดถืออยู่และนานเกินนี้ คืนเข้าคิวทันที
PROBE_LIMIT = 12                  # ลองเชิงกับภาพเดียวกันได้กี่ครั้งขณะผู้ให้บริการขัดข้อง ก่อนถือว่าภาพนี้มีปัญหาเอง
SUPERVISE_EVERY = 15.0
MAX_WORKERS = 4

_stop = threading.Event()
_cond = threading.Condition()
_gen = 0                          # เพิ่มทุกครั้งที่มีคนปลุกคิว: เธรดที่กำลังจะพักเห็นว่าเลขเปลี่ยนก็ไม่พัก
_threads = {}
_supervisor = None
_lock = threading.RLock()
_inflight = {}                    # photo_id -> เวลาที่เริ่ม (monotonic) ของภาพที่โปรเซสนี้กำลังวิเคราะห์
_probes = {}                      # photo_id -> จำนวนครั้งที่ถูกใช้ลองเชิง
_dirty = True                     # มีการเปลี่ยนแปลงในคิวที่ผู้เฝ้าคิวยังไม่ได้ดู

state = {"last_error": "", "last_ok": None, "paused_reason": "", "paused_kind": "", "resume_at": None, "paused_since": None,
         "idle": IDLE_MAX, "pending": 0, "processing": 0, "oldest": None, "avg_s": 20.0, "jobs": 0, "checked_at": None,
         "workers": 0, "started_at": None}


# --------------------------------------------------------------------------- ตัวนับ (ชื่อเดิมยังใช้ได้)
def quota_day() -> str:
    return providers.quota_day()


def usage_today(db) -> int:
    """จำนวนครั้งที่ AI หลักรับงานไปทำในรอบโควตาของวันนี้"""
    row = db.get(AiUsage, providers.quota_day())
    return (row.count or 0) if row else 0


def usage_backup_today(db) -> int:
    row = db.get(AiUsage, providers.quota_day())
    return (row.count2 or 0) if row else 0


def usage_all(db) -> dict:
    """จำนวนครั้งที่แต่ละช่องรับงานไปทำในรอบโควตาของวันนี้: {"ai1": n, "ai2": n, "ai3": n}"""
    row = db.get(AiUsage, providers.quota_day())
    return {slot: (getattr(row, col) or 0) if row else 0 for slot, col in providers.USAGE_COLUMNS.items()}


def count_call(cfg: dict = None, err=None):
    providers.record(cfg or {"slot": "ai1", "type": "manual"}, err)


# --------------------------------------------------------------------------- ปลุกและพัก
def wake():
    """บอกคิวว่ามีงานใหม่หรือการตั้งค่าเปลี่ยน (เรียกหลัง commit เสมอ)"""
    global _gen, _dirty
    with _cond:
        _gen += 1
        _dirty = True
        _cond.notify_all()


def _wait(seen: int, timeout: float) -> None:
    """พักจนกว่าจะมีคนปลุกหลังจากจุดที่เธรดนี้อ่านเลขรุ่นไว้ หรือครบเวลา"""
    end = time.monotonic() + max(0.0, timeout)
    with _cond:
        while _gen == seen and not _stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return
            _cond.wait(min(left, 30.0))


def _pause(seconds: float) -> bool:
    """รอจังหวะก่อนเรียก AI คืน False ถ้าระบบกำลังปิด"""
    if seconds <= 0:
        return not _stop.is_set()
    return not _stop.wait(seconds)


# --------------------------------------------------------------------------- บันทึกผล
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


def requeue(db, query) -> int:
    """ส่งภาพตามเงื่อนไขของ query กลับเข้าคิวแบบเริ่มนับใหม่ (ผู้เรียกเป็นคน commit แล้วเรียก wake)"""
    return query.update({"status": "pending", "attempts": 0, "next_try_at": None, "error": "",
                         "queued_at": now(), "started_at": None}, synchronize_session=False)


class Job:
    """ข้อมูลทั้งหมดที่ต้องใช้วิเคราะห์ภาพหนึ่งใบ อ่านจากฐานข้อมูลครั้งเดียวตอนหยิบ"""
    __slots__ = ("id", "token", "photo", "image", "mode", "rubric", "checklist", "meta", "fatal", "began")

    def __init__(self, photo: Photo):
        self.id, self.token, self.photo = photo.id, photo.started_at, photo
        self.image, self.mode, self.rubric, self.checklist, self.meta, self.fatal = None, "level", [], [], {}, ""
        self.began = time.monotonic()


class Outcome:
    __slots__ = ("kind", "result", "cfg", "review", "text", "counted", "probe", "seconds")

    def __init__(self, kind: str, **kw):
        self.kind = kind                 # done | defer | fail
        self.result, self.cfg, self.review = kw.get("result"), kw.get("cfg"), kw.get("review", False)
        self.text, self.counted, self.probe = kw.get("text", ""), kw.get("counted", False), kw.get("probe", False)
        self.seconds = kw.get("seconds", 0.0)


def _due(q, t):
    return q.filter(Photo.status == "pending").filter((Photo.next_try_at.is_(None)) | (Photo.next_try_at <= t))


def _claim(db):
    """หยิบภาพถัดไปแบบ atomic: ภาพที่ยังไม่เคยล้มเหลวก่อน แล้วจึงตามลำดับที่ส่ง"""
    for _ in range(6):
        t = now()
        row = _due(db.query(Photo.id), t).order_by(Photo.attempts, Photo.id).first()
        if row is None:
            return None
        got = (db.query(Photo).filter(Photo.id == row[0], Photo.status == "pending")
               .update({"status": "processing", "started_at": t}, synchronize_session=False))
        db.commit()
        if got == 1:
            db.expire_all()
            return db.get(Photo, row[0])
    return None


def _load(db, photo: Photo) -> Job:
    job = Job(photo)
    blob = db.get(PhotoImage, photo.id)
    rnd = db.get(Round, photo.round_id)
    if blob is None:
        job.fatal = "ภาพเต็มถูกลบไปแล้ว จึงวิเคราะห์ใหม่ไม่ได้"
        return job
    if rnd is None:
        job.fatal = "ไม่พบรอบการตรวจของภาพนี้"
        return job
    job.image = bytes(blob.data)
    job.mode = rnd.mode or "level"
    job.rubric, job.checklist = list(rnd.rubric or []), list(rnd.checklist or [])
    job.meta = dict(area_type=photo.area_type, area_name=photo.area_name, note=photo.note)
    if photo.area_id:
        area = db.get(AuditArea, photo.area_id)
        job.meta["standard"] = area.standard if area is not None else ""
    return job


def _decide(errors: list, s: dict) -> Outcome:
    """ผู้ให้บริการทุกเจ้าที่เรียกได้ล้มเหลว: ตัดสินว่าภาพนี้ควรรอ ลองใหม่ หรือถือว่าวิเคราะห์ไม่สำเร็จ"""
    if not errors:                        # ถูกพักไปก่อนโดยเธรดอื่น ยังไม่ได้เรียกใครเลย
        return Outcome("defer", text=providers.plan(s)["reason"] or "รอผู้ให้บริการ AI")
    retryable = [x for x in errors if x[1].kind in ("parse", "transient", "rate", "quota")]
    content = [x for x in errors if x[1].kind == "content"]
    if content and not retryable:         # ปัญหาเฉพาะภาพนี้ ลองใหม่ไปก็ไม่ผ่าน
        return Outcome("fail", text=str(content[0][1]))
    if not retryable:                     # การตั้งค่าผิดล้วน ๆ: ทุกภาพจะเจอเหมือนกัน ให้รอผู้ดูแลแก้
        return Outcome("defer", text=str(errors[0][1]))
    counted = any(e.kind == "parse" or (e.kind == "transient" and not probing) for _cfg, e, probing in errors)
    probe = any(e.kind == "transient" and probing for _cfg, e, probing in errors)
    return Outcome("defer", text=str(retryable[0][1]), counted=counted, probe=probe)


def _analyse(job: Job, s: dict, pace: bool) -> Outcome:
    if job.fatal:
        return Outcome("fail", text=job.fatal)
    twice = int(s.get("ai_passes", 1) or 1) >= 2
    if job.mode == "checklist":
        # AI รายงานสภาพทีละข้อ แล้ว rule engine เป็นผู้คิดคะแนน
        checks = rules.applicable(job.checklist, job.rubric, job.photo)
        picture = imaging.draw_zones(job.image, rules.zones_of(checks)) if checks else job.image

        def run(cfg):
            return ai.check_with(cfg, picture, checks, job.meta, s, on_call=providers.record)
        merge = ai.merge_check_passes
    else:
        def run(cfg):
            return ai.analyze_with(cfg, job.image, job.rubric, job.meta, s, on_call=providers.record)
        merge = ai.merge_passes
    errors = []
    for cfg in ai.profiles(s):
        if providers.blocked(cfg, s) is not None or not providers.begin(cfg):
            continue
        probing = providers.troubled(cfg)
        t0 = time.monotonic()
        try:
            if pace and not _pause(providers.reserve(cfg, s)):
                providers.end(cfg)
                return Outcome("defer", text="ระบบกำลังปิด ภาพจะถูกวิเคราะห์เมื่อเริ่มใหม่")
            t0 = time.monotonic()
            result = run(cfg)
        except ai.AIError as e:
            e.kind = providers.failure(cfg, e)
            errors.append((cfg, e, probing))
            log.warning("photo %s: %s (%s) failed after %.1fs: [%s] %s", job.id, cfg["slot"], cfg["model"] or cfg["type"],
                        time.monotonic() - t0, e.kind, e)
            wake()                        # สถานะของผู้ให้บริการเปลี่ยน: ให้เธรดอื่นดูใหม่
            continue
        except BaseException:
            providers.end(cfg)
            raise
        providers.success(cfg)
        if probing:
            wake()                        # ผู้ให้บริการกลับมาแล้ว: เธรดที่รอผลการลองเรียกทำงานต่อได้ทันที
        review = False
        if twice and cfg["type"] != "demo" and result.get("image_ok", True) and providers.blocked(cfg, s) is None:
            if not pace or _pause(providers.reserve(cfg, s)):
                try:                      # รอบที่สองล้มเหลวไม่เป็นไร ใช้ผลรอบแรก
                    second = run(cfg)
                    providers.success(cfg)
                    result, review = merge(result, second)
                except ai.AIError as e:
                    providers.failure(cfg, e)
        return Outcome("done", result=result, cfg=cfg, review=review, seconds=time.monotonic() - t0)
    return _decide(errors, s)


def _store(job: Job, change) -> bool:
    """เปิด session ใหม่มาบันทึกผลของภาพ ลองซ้ำเมื่อฐานข้อมูลสะดุด และทิ้งผลถ้าภาพถูกลบหรือถูกส่งเข้าคิวใหม่ไปแล้ว"""
    for delay in (0.0, 1.0, 3.0, 8.0):
        if delay and _stop.wait(delay):
            break
        try:
            with SessionLocal() as db:
                photo = db.get(Photo, job.id)
                if photo is None or photo.status != "processing" or photo.started_at != job.token:
                    log.info("photo %s: result discarded (deleted or re-queued while analysing)", job.id)
                    return False
                after = change(db, photo)
                photo.started_at = None
                db.commit()
                if after:
                    try:
                        after(db)
                    except Exception:
                        log.exception("photo %s: follow-up after saving failed", job.id)
                return True
        except SQLAlchemyError as e:
            log.warning("photo %s: could not save result (%s), retrying", job.id, type(e).__name__)
    log.error("photo %s: result could not be saved; the watchdog will put the photo back in the queue", job.id)
    return False


def _finish(job: Job, out: Outcome, s: dict) -> None:
    limit = max(1, int(s.get("ai_max_attempts", 4) or 4))

    def alert(text: str):
        return lambda db: notify.alert_admin(db, "ai_error", f"AI วิเคราะห์ภาพไม่สำเร็จ: {text[:300]} "
                                                             "ตรวจการตั้งค่า AI และโควตาในหน้าจัดการระบบ", 1)

    def emit(db, photo):
        notify.emit(db, "result", round_id=photo.round_id, department_id=photo.department_id, photo_id=photo.id)

    def failed(db, photo, text: str):
        photo.status, photo.error, photo.next_try_at = "error", text, None
        emit(db, photo)
        state["last_error"] = text
        return alert(text)

    def change(db, photo):
        if out.kind == "done":
            try:
                result = out.result
                if job.mode == "checklist":
                    result = rules.finalize(db, photo, job.rubric, result)
                apply_result(photo, result, out.cfg["type"], out.cfg["model"] if out.cfg["type"] != "demo" else "demo")
                photo.ai_slot = out.cfg["slot"]
                if photo.status == "done" and photo.after_of:
                    from . import actions
                    actions.close_fixed(db, photo)
            except SQLAlchemyError:
                raise
            except Exception as e:                   # ไม่ให้คิวค้างเพราะข้อผิดพลาดที่ไม่คาดคิด
                log.exception("photo %s: scoring failed", job.id)
                db.rollback()
                photo = db.get(Photo, job.id)
                return failed(db, photo, f"ข้อผิดพลาดภายในระบบ: {type(e).__name__}")
            photo.review_flag = out.review
            photo.verified_by, photo.verified_at = "", None       # ผลใหม่ต้องให้คนยืนยันใหม่
            photo.overridden = False
            photo.override_by = photo.override_note = ""
            photo.override_at = None
            emit(db, photo)
            state["last_ok"], state["last_error"] = now(), ""
            return None
        if out.kind == "fail":
            return failed(db, photo, out.text)
        # defer
        state["last_error"] = out.text
        if out.probe:
            _probes[job.id] = _probes.get(job.id, 0) + 1
            if _probes[job.id] >= PROBE_LIMIT:
                return failed(db, photo, f"ลองหลายครั้งแล้วไม่สำเร็จ: {out.text}")
        if out.counted:
            photo.attempts = (photo.attempts or 0) + 1
            if photo.attempts >= limit:
                return failed(db, photo, out.text)
            photo.next_try_at = now() + timedelta(seconds=BACKOFF[min(photo.attempts - 1, len(BACKOFF) - 1)])
        else:
            photo.next_try_at = None
        photo.status, photo.error = "pending", f"จะลองใหม่อัตโนมัติ: {out.text}"
        return None

    saved = _store(job, change)
    took = time.monotonic() - job.began
    if out.kind != "defer":
        _probes.pop(job.id, None)
    if saved and out.kind == "done":
        state["jobs"] += 1
        state["avg_s"] = round(0.7 * state["avg_s"] + 0.3 * took, 1) if state["jobs"] > 1 else round(took, 1)
        if state["jobs"] % 10 == 1:       # จำเวลาเฉลี่ยไว้ข้ามการเริ่มระบบใหม่ เวลาที่บอกผู้ใช้จึงแม่นตั้งแต่ภาพแรก
            try:
                with SessionLocal() as db:
                    settings_store.save(db, {"_queue_avg_s": state["avg_s"]})
            except Exception:
                log.warning("could not remember the average analysis time")
        log.info("photo %s: analysed in %.1fs by %s (%s)", job.id, took, out.cfg["slot"], out.cfg["model"] or out.cfg["type"])
    elif saved:
        log.info("photo %s: %s after %.1fs: %s", job.id, out.kind, took, out.text[:200])


def _blocked(db, gate: dict) -> float:
    """ไม่มีผู้ให้บริการที่เรียกได้ตอนนี้: จดเหตุผล เตือนผู้ดูแลถ้ามีภาพรออยู่ แล้วบอกว่าควรตื่นมาดูอีกทีเมื่อไร"""
    waiting = db.query(func.count(Photo.id)).filter(Photo.status == "pending").scalar() or 0
    state["pending"] = waiting
    if state["paused_kind"] != gate["kind"] or not state["paused_since"]:
        state["paused_since"] = now()
    state["paused_reason"], state["paused_kind"] = gate["reason"], gate["kind"]
    state["resume_at"] = (datetime.fromtimestamp(gate["resume_at"], timezone.utc).replace(tzinfo=None)
                          if gate["resume_at"] else None)
    if waiting and gate["kind"] in ("cap", "quota", "config"):
        hours = 6 if gate["kind"] == "config" else 20
        try:
            notify.alert_admin(db, f"ai_{gate['kind']}", f"คิววิเคราะห์ภาพหยุดรอ มี {waiting} ภาพในคิว: {gate['reason']}", hours)
        except SQLAlchemyError:          # การแจ้งเตือนพลาดต้องไม่ทำให้คิวสะดุด รอบถัดไปจะแจ้งใหม่
            db.rollback()
            log.warning("could not record the queue alert; will try again")
    if not gate["resume_at"]:
        return IDLE_MAX
    return min(max(gate["resume_at"] - providers.now() + 0.5, 1.0), BLOCKED_MAX)


def run_one(pace: bool = False) -> tuple:
    """วิเคราะห์ภาพถัดไปในคิว 1 ภาพ คืน (ผล, ควรพักกี่วินาทีก่อนดูคิวอีกที)

    ผล: worked = ได้ผลของภาพแล้ว (สำเร็จหรือผิดพลาด) | deferred = เรียก AI แล้วไม่ได้ผล ภาพกลับไปรอ
        empty = ไม่มีภาพที่ถึงคิว | blocked = ไม่มีผู้ให้บริการที่เรียกได้ตอนนี้
    """
    s = settings_store.load()
    if not ai.is_configured(s):
        state.update(paused_reason="ยังไม่ได้ตั้งค่า AI", paused_kind="config", resume_at=None)
        return "blocked", IDLE_MAX
    with SessionLocal() as db:
        providers.load_usage(db)
        gate = providers.plan(s)
        if not gate["ready"]:
            return "blocked", _blocked(db, gate)
        state.update(paused_reason="", paused_kind="", resume_at=None, paused_since=None)
        photo = _claim(db)
        if photo is None:
            t = now()
            due = db.query(func.min(Photo.next_try_at)).filter(Photo.status == "pending").scalar()
            state["pending"] = db.query(func.count(Photo.id)).filter(Photo.status == "pending").scalar() or 0
            return "empty", (max(1.0, (due - t).total_seconds() + 0.5) if due is not None else IDLE_MAX)
        job = _load(db, photo)
    with _lock:
        _inflight[job.id] = time.monotonic()
    try:
        try:
            out = _analyse(job, s, pace)
        except Exception as e:                       # ไม่ให้คิวค้างเพราะข้อผิดพลาดที่ไม่คาดคิด
            log.exception("photo %s: analyse failed", job.id)
            out = Outcome("fail", text=f"ข้อผิดพลาดภายในระบบ: {type(e).__name__}")
        _finish(job, out, s)
    finally:
        with _lock:
            _inflight.pop(job.id, None)
    return ("deferred", 1.0) if out.kind == "defer" else ("worked", 0.0)


def process_one() -> bool:
    """วิเคราะห์ภาพถัดไป 1 ภาพทันทีโดยไม่เว้นจังหวะ คืน True เมื่อได้หยิบภาพมาทำ (ใช้ในชุดทดสอบและงานดูแล)"""
    result, idle = run_one(pace=False)
    state["idle"] = idle
    return result in ("worked", "deferred")


def remind_departments(db) -> int:
    """ก่อนวันสิ้นสุดรอบ: แจ้งแผนกที่ภาพยังไม่ครบ หรือยังขาดจุดตรวจบังคับ วันละครั้งต่อแผนก"""
    from . import scoring
    s = settings_store.load()
    days = int(s.get("dept_remind_days", 3) or 0)
    if days <= 0:
        return 0
    today = (now() + timedelta(hours=7)).date()
    link = (s.get("public_url") or "").rstrip("/")
    sent = 0
    for rnd in db.query(Round).filter(Round.status == "open", Round.end_date.isnot(None)).all():
        left = (rnd.end_date - today).days
        if left < 0 or left > days:
            continue
        rk = scoring.round_ranking(db, rnd, with_prev=False)
        for r in rk["unranked"] + rk["idle"]:
            key = f"_stamp_remind_{rnd.id}_{r['dept'].id}"
            if s.get(key) == today.isoformat():
                continue
            need = max(1, rnd.min_photos)
            lines = [f"รอบ {rnd.name} จะปิดใน {left} วัน" if left else f"รอบ {rnd.name} ปิดวันนี้",
                     f"แผนก {r['dept'].name} ยังไม่ถูกจัดอันดับ: นับคะแนนแล้ว {r['scored']} จากขั้นต่ำ {need} ภาพ"]
            if r.get("unverified"):
                lines.append(f"มี {r['unverified']} ภาพที่รอหัวหน้าหรือกรรมการยืนยัน")
            if r.get("missing"):
                lines.append("จุดตรวจบังคับที่ยังไม่มีภาพ: " + ", ".join(r["missing"][:8]))
            if link:
                lines.append(f"{link}/capture")
            notify.emit(db, "round", round_id=rnd.id, department_id=r["dept"].id, payload={"action": "remind", "text": "\n".join(lines)})
            settings_store.save(db, {key: today.isoformat()})
            sent += 1
    if sent:
        db.commit()
    return sent


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
        from . import actions
        out["actions"] = actions.overdue_reminders(db)
        from . import rounds_auto, scheduler
        rounds_auto.ensure(db)
        out["cameras"] = scheduler.stale_cameras(db)
        out["reminders"] = remind_departments(db)
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



# --------------------------------------------------------------------------- ผู้เฝ้าคิว
def recover_stuck(older_than: float = None) -> int:
    """คืนภาพที่ค้างสถานะกำลังวิเคราะห์กลับเข้าคิว

    ไม่ระบุเวลา = ทุกภาพ (ใช้ตอนระบบเริ่ม เพราะยังไม่มีเธรดใดทำงาน)
    ระบุเวลา = เฉพาะภาพที่ถูกหยิบไปนานเกินกี่วินาที และไม่ใช่ภาพที่โปรเซสนี้กำลังวิเคราะห์อยู่
    """
    with SessionLocal() as db:
        q = db.query(Photo).filter(Photo.status == "processing")
        if older_than is not None:
            with _lock:
                mine = list(_inflight)
            edge = now() - timedelta(seconds=older_than)
            q = q.filter((Photo.started_at.is_(None)) | (Photo.started_at < edge))
            if mine:
                q = q.filter(Photo.id.notin_(mine))
        n = q.update({"status": "pending", "started_at": None}, synchronize_session=False)
        db.commit()
    if n:
        log.warning("put %s stuck photo(s) back in the queue", n)
        wake()
    return n


def release_long_waits() -> int:
    """ยกเลิกเวลารอที่ยาวเกินกว่าที่คิวรุ่นนี้จะตั้งเอง (เกิน 5 นาที)

    ภาพที่รุ่นก่อนหน้าพักไว้ครั้งละ 15 ถึง 60 นาทีจึงถูกทำต่อทันทีหลังอัปเดต ไม่ต้องรอให้ครบเวลาเดิม
    """
    edge = now() + timedelta(seconds=max(BACKOFF) + 5)
    with SessionLocal() as db:
        n = (db.query(Photo).filter(Photo.status == "pending", Photo.next_try_at.isnot(None), Photo.next_try_at > edge)
             .update({"next_try_at": None}, synchronize_session=False))
        db.commit()
    if n:
        log.warning("released %s photo(s) from a long retry wait", n)
    return n


def refresh(db=None) -> dict:
    """อ่านขนาดของคิวจากฐานข้อมูลมาเก็บในหน่วยความจำ (หน้าเว็บใช้ค่านี้ จึงไม่ต้องถามฐานข้อมูลทุกครั้งที่มีคนเปิดหน้า)"""
    def read(session):
        counts = dict(session.query(Photo.status, func.count(Photo.id))
                      .filter(Photo.status.in_(["pending", "processing"])).group_by(Photo.status).all())
        oldest = (session.query(func.min(func.coalesce(Photo.queued_at, Photo.created_at)))
                  .filter(Photo.status.in_(["pending", "processing"])).scalar())
        return counts, oldest
    if db is not None:
        counts, oldest = read(db)
    else:
        with SessionLocal() as session:
            counts, oldest = read(session)
    state.update(pending=counts.get("pending", 0), processing=counts.get("processing", 0), oldest=oldest,
                 checked_at=now(), version=DATA_VERSION["n"])
    return state


def watch() -> dict:
    """งานของผู้เฝ้าคิวหนึ่งรอบ: คืนภาพที่ค้าง นับคิว และเตือนผู้ดูแลถ้ามีภาพรอนานเกินที่ตั้งไว้"""
    global _dirty
    _dirty = False
    out = {"recovered": recover_stuck(STALE_AFTER), "alert": False}
    s = settings_store.load()
    with SessionLocal() as db:
        refresh(db)
        limit = int(s.get("queue_alert_min", 30) or 0)
        waited = (now() - state["oldest"]).total_seconds() / 60 if state["oldest"] else 0
        if limit > 0 and state["pending"] and waited >= limit and ai.is_configured(s):
            why = state["paused_reason"] or state["last_error"] or "คิวยาว หรือ AI ตอบช้า"
            link = (s.get("public_url") or "").rstrip("/")
            out["alert"] = notify.alert_admin(
                db, "queue_slow", f"มี {state['pending']} ภาพรอ AI วิเคราะห์ ภาพที่รอนานสุดรอมาแล้ว {int(waited)} นาที "
                                  f"สาเหตุล่าสุด: {why[:300]}" + (f" ดูรายละเอียดที่ {link}/admin/queue" if link else ""), 6)
    return out


BACKUP_SLOTS = tuple(s for s in providers.SLOTS if s != "ai1")


def backup_scored(db):
    """ภาพในรอบที่ยังเปิดอยู่ ซึ่งได้คะแนนจาก AI สำรอง และยังไม่มีคนยืนยันหรือปรับผล (จึงวิเคราะห์ใหม่ได้โดยไม่ทับงานของคน)"""
    open_rounds = [r[0] for r in db.query(Round.id).filter(Round.status == "open").all()]
    return (db.query(Photo).filter(Photo.round_id.in_(open_rounds or [-1]), Photo.status == "done",
                                   Photo.ai_slot.in_(BACKUP_SLOTS), Photo.has_image.is_(True),
                                   Photo.verified_at.is_(None), Photo.overridden.is_(False)))


def rescore_with_main(db) -> dict:
    """ส่งภาพที่ได้คะแนนจาก AI สำรองกลับเข้าคิว ให้ AI หลักวิเคราะห์ใหม่ เพื่อให้ทุกแผนกถูกวัดด้วยโมเดลเดียวกัน

    ทำเฉพาะเมื่อ AI หลักเรียกได้ในตอนนี้ และไม่เกินจำนวนครั้งที่เพดานต่อวันของ AI หลักยังเหลือ
    ภาพที่ยังไม่ได้ส่ง ยังคงคะแนนเดิมไว้ ไม่มีช่วงที่ภาพหายจากการจัดอันดับโดยไม่จำเป็น
    """
    s = settings_store.load()
    main = [c for c in ai.profiles(s) if c["slot"] == "ai1"]
    total = backup_scored(db).count()
    if not main:
        return dict(sent=0, total=total, why="ยังไม่ได้ตั้งค่า AI หลัก")
    providers.load_usage(db)
    stop = providers.blocked(main[0], s)
    if stop is not None:
        return dict(sent=0, total=total, why=stop[2])
    cap = providers.cap_of(main[0], s)
    passes = 2 if int(s.get("ai_passes", 1) or 1) >= 2 else 1
    room = total if not cap else max(0, (cap - providers.usage().get("ai1", 0)) // passes)
    ids = [r[0] for r in backup_scored(db).with_entities(Photo.id).order_by(Photo.id).limit(room).all()]
    sent = requeue(db, db.query(Photo).filter(Photo.id.in_(ids))) if ids else 0
    db.commit()
    if sent:
        wake()
    return dict(sent=sent, total=total, why="" if sent == total else "เพดานต่อวันของ AI หลักเหลือไม่พอสำหรับทุกภาพ ส่งเท่าที่เหลือ")


def kick(db) -> dict:
    """ผู้ดูแลกด เดินคิวเดี๋ยวนี้: ล้างการพักของผู้ให้บริการ ยกเลิกเวลารอของทุกภาพ คืนภาพที่ค้าง แล้วปลุกคิว"""
    providers.reset()
    _probes.clear()
    released = (db.query(Photo).filter(Photo.status == "pending", Photo.next_try_at.isnot(None))
                .update({"next_try_at": None}, synchronize_session=False))
    db.commit()
    stuck = recover_stuck(ORPHAN_AFTER)
    state.update(paused_reason="", paused_kind="", resume_at=None, paused_since=None)
    wake()
    return {"released": released, "stuck": stuck}


def _order(rows: list, t) -> list:
    """เรียงภาพที่รออยู่ตามลำดับที่คิวจะหยิบจริง: ภาพที่กำลังวิเคราะห์ ภาพที่ถึงคิว แล้วจึงภาพที่รอลองใหม่"""
    def key(r):
        if r.status == "processing":
            return (0, 0, 0, r.id)
        waiting = r.next_try_at is not None and r.next_try_at > t
        return (2 if waiting else 1, r.next_try_at.timestamp() if waiting else 0, r.attempts or 0, r.id)
    return sorted(rows, key=key)


def per_photo_seconds(s: dict) -> float:
    """เวลาเฉลี่ยต่อภาพของคิวตอนนี้ คิดจากเวลาที่วัดได้จริง จำนวนเธรด และอัตราที่ตั้งไว้"""
    n = max(1, min(int(s.get("ai_workers", 2) or 2), MAX_WORKERS))
    passes = 2 if int(s.get("ai_passes", 1) or 1) >= 2 else 1
    snaps = [p for p in providers.snapshot(s) if p["ok"]]
    pace = 60.0 / max(0.5, snaps[0]["rpm_now"]) * passes if snaps and snaps[0]["type"] != "demo" else 0.0
    return max(state["avg_s"] / n, pace, 1.0)


def _hold(t) -> tuple:
    """คิวหยุดรออยู่หรือไม่ คืน (หยุดอยู่, วินาทีที่ต้องรอจนเดินต่อ หรือ None ถ้าบอกไม่ได้, วินาทีจนถึงการลองเรียกครั้งถัดไป)

    บอกเวลาเดินต่อได้เฉพาะเหตุที่รู้กำหนดแน่: พักเพราะเรียกถี่ กำลังลองเรียก และครบเพดานที่ตั้งเอง
    โควตาของผู้ให้บริการหมด ขัดข้อง หรือตั้งค่าผิด ระบบรู้แค่เวลาที่จะลองเรียกใหม่ ไม่รู้ว่าจะกลับมาเมื่อไร
    จึงบอกผู้ใช้เป็นเวลาที่จะลองใหม่แทน ไม่เดาเวลาที่จะได้ผล
    """
    if not state["paused_reason"]:
        return False, 0.0, None
    left = max(0.0, (state["resume_at"] - t).total_seconds()) if state["resume_at"] else None
    if state["paused_kind"] in ("rate", "trial", "cap") and left is not None:
        return True, left, left
    return True, None, left


def waits(db, ids: list = None) -> dict:
    """ลำดับในคิวและเวลาที่คาดว่าจะรอของภาพที่ยังไม่ได้ผล

    คืน {photo_id: {position, total, status, eta_s, retry_s, reason}}
    eta_s = อีกกี่วินาทีจึงคาดว่าได้ผล (None = ยังบอกไม่ได้) | retry_s = อีกกี่วินาทีระบบจะลองเรียก AI ใหม่ (เมื่อคิวหยุดรอ)
    """
    s = settings_store.load()
    t = now()
    rows = (db.query(Photo.id, Photo.status, Photo.attempts, Photo.next_try_at, Photo.error, Photo.started_at)
            .filter(Photo.status.in_(["pending", "processing"])).all())
    each = per_photo_seconds(s)
    one = max(2.0, float(state["avg_s"]))                 # เวลาที่ภาพหนึ่งใบใช้ตั้งแต่ถูกหยิบจนได้ผล
    paused, hold, retry = _hold(t)
    ready = ai.is_configured(s)
    # ภาพที่กำลังวิเคราะห์อยู่ใช้เวลาไปแล้วเท่าไร: ภาพถัดไปเริ่มได้เมื่อครบช่วงห่างของคิวนับจากภาพที่เริ่มล่าสุด
    started = [(t - r.started_at).total_seconds() for r in rows if r.status == "processing" and r.started_at]
    free_in = max(0.0, each - min(started)) if started else 0.0
    out, ahead = {}, 0
    for i, r in enumerate(_order(rows, t)):
        own = max(0.0, (r.next_try_at - t).total_seconds()) if r.next_try_at and r.status == "pending" else 0.0
        if r.status == "processing":
            spent = (t - r.started_at).total_seconds() if r.started_at else 0.0
            eta, reason = max(2.0, one - spent), ""
        else:
            reason = ""
            if not ready:
                reason = "ยังไม่ได้ตั้งค่า AI"
            elif paused:
                reason = state["paused_reason"]
            elif r.error:
                reason = r.error
            eta = None if (not ready or hold is None) else max(hold, own, free_in) + ahead * each + one
            ahead += 1
        if ids is None or r.id in ids:
            out[r.id] = dict(position=i + 1, total=len(rows), status=r.status, eta_s=None if eta is None else int(round(eta)),
                             retry_s=None if (retry is None or r.status != "pending" or not ready) else int(round(retry)),
                             reason=reason)
    return out


def duration_text(seconds) -> str:
    """ระยะเวลาเป็นคำ: 45 วินาที, 2 นาที 5 วินาที, 1 ชั่วโมง 10 นาที"""
    if seconds is None:
        return ""
    n = max(0, int(round(seconds)))
    if n < 60:
        return f"{n} วินาที"
    if n < 3600:
        m, sec = divmod(n, 60)
        return f"{m} นาที {sec} วินาที" if sec else f"{m} นาที"
    h, m = divmod(n // 60, 60)
    return f"{h} ชั่วโมง {m} นาที" if m else f"{h} ชั่วโมง"


def eta_text(seconds) -> str:
    """เวลาโดยประมาณ ขึ้นต้นด้วยคำว่า ประมาณ (ใช้ในหน้าของผู้ดูแล)"""
    return "" if seconds is None else "ประมาณ " + duration_text(seconds)


def wait_parts(w: dict) -> dict:
    """ข้อความบอกการรอของภาพหนึ่งใบ แยกเป็นส่วน เพื่อให้หน้าเว็บนับเวลาถอยหลังได้

    head = ลำดับในคิว | lead + เวลา = เวลาที่คาดว่าจะได้ผล หรือเวลาที่ระบบจะลองเรียก AI ใหม่ | tail = เหตุผลถ้าคิวหยุดรอ
    """
    if not w:
        return {}
    if w["status"] == "processing":
        head, lead, secs = "AI กำลังวิเคราะห์ภาพนี้", "คาดว่าได้ผลในอีกประมาณ", w["eta_s"]
    else:
        head = f"รอคิวลำดับที่ {w['position']} จาก {w['total']} ภาพ"
        if w["eta_s"] is not None:
            lead, secs = "คาดว่าได้ผลในอีกประมาณ", w["eta_s"]
        elif w["retry_s"] is not None:
            lead, secs = "คิวหยุดรอ ระบบจะลองเรียก AI อีกครั้งในอีก", w["retry_s"]
        else:
            lead, secs = "", None
    tail = (w["reason"] or "")[:260]
    text = head + (f" {lead} {duration_text(secs)}" if lead else "") + (f" ({tail})" if tail else "")
    return dict(head=head, lead=lead, secs=secs, time=duration_text(secs), tail=tail, text=text, known=w["eta_s"] is not None)


def wait_text(w: dict) -> str:
    """ข้อความเดียวสำหรับผู้ส่งภาพ: ลำดับในคิว เวลาที่คาดว่าจะรอเป็นนาทีและวินาที และเหตุผลถ้าคิวหยุดรอ"""
    return wait_parts(w).get("text", "")


def queue_info(db) -> dict:
    """ข้อมูลครบของคิวสำหรับหน้า คิววิเคราะห์ ของผู้ดูแล"""
    s = settings_store.load()
    refresh(db)
    providers.load_usage(db)
    t = now()
    each = per_photo_seconds(s)
    _paused, hold, retry = _hold(t)
    total = state["pending"] + state["processing"]
    return dict(
        pending=state["pending"], processing=state["processing"],
        errors=db.query(func.count(Photo.id)).filter(Photo.status == "error").scalar() or 0,
        oldest=state["oldest"], waited_min=int((t - state["oldest"]).total_seconds() // 60) if state["oldest"] else 0,
        each_s=round(each, 1), avg_s=state["avg_s"], jobs=state["jobs"],
        eta_s=int(hold + total * each) if total and hold is not None and ai.is_configured(s) else None,
        retry_s=None if retry is None else int(round(retry)),
        paused=state["paused_reason"], paused_kind=state["paused_kind"], resume_at=state["resume_at"],
        last_ok=state["last_ok"], last_error=state["last_error"], providers=providers.snapshot(s),
        ai_ready=ai.is_configured(s), workers=alive(), workers_want=_wanted(s), running=bool(_supervisor and _supervisor.is_alive()),
        keepalive=keepalive.status(s), started_at=state["started_at"], backup_scored=backup_scored(db).count())


def brief() -> dict:
    """สรุปคิวจากหน่วยความจำ (ไม่ถามฐานข้อมูล) สำหรับแถบแจ้งบนหน้าเว็บ"""
    return dict(pending=state["pending"], processing=state["processing"], paused=state["paused_reason"],
                resume_at=state["resume_at"])


# --------------------------------------------------------------------------- เธรด
def _wanted(s: dict = None) -> int:
    s = s or settings_store.load()
    try:
        return max(1, min(int(s.get("ai_workers", 2) or 2), MAX_WORKERS))
    except (TypeError, ValueError):
        return 2


def alive() -> int:
    return sum(1 for t in _threads.values() if t.is_alive())


def _work(index: int):
    while not _stop.is_set():
        if index >= _wanted():           # ผู้ดูแลลดจำนวนเธรด: เธรดส่วนเกินเลิกทำงานเอง
            with _lock:
                if _threads.get(index) is threading.current_thread():
                    del _threads[index]  # เลิกโดยตั้งใจ ไม่ใช่เธรดตาย
            return
        seen = _gen
        try:
            result, idle = run_one(pace=True)
        except Exception:
            log.exception("worker %s", index)
            result, idle = "fault", 30.0  # ฐานข้อมูลสะดุดหรือข้อผิดพลาดอื่น: พักสั้น ๆ แล้วลองใหม่ ไม่พักเป็นชั่วโมง
        if result == "worked":
            continue
        state["idle"] = idle
        _wait(seen, idle)


def _ensure_workers():
    want = _wanted()
    for i in range(want):
        t = _threads.get(i)
        if t is None or not t.is_alive():
            if t is not None:
                log.error("worker %s died; starting a new one", i)
            t = _threads[i] = threading.Thread(target=_work, args=(i,), name=f"fives-worker-{i}", daemon=True)
            t.start()
    state["workers"] = alive()


def _supervise():
    try:
        recover_stuck()
        release_long_waits()
    except Exception:
        log.exception("recover at start")
    last_house = last_watch = 0.0
    while not _stop.is_set():
        try:
            _ensure_workers()
            t = time.time()
            if t - last_house > 6 * 3600:
                last_house = t
                housekeeping()
            busy = bool(state["pending"] or state["processing"] or _dirty)
            with _lock:
                busy = busy or bool(_inflight)
            every = 600.0 if state["paused_reason"] else 60.0
            if busy and (_dirty or t - last_watch >= every):
                last_watch = t
                watch()
            keepalive.tick(settings_store.load(), busy=bool(state["pending"] or state["processing"]),
                           paused_kind=state["paused_kind"], paused_since=state["paused_since"])
        except Exception:
            log.exception("queue supervisor")
        _stop.wait(SUPERVISE_EVERY)


def start():
    global _supervisor
    if _supervisor and _supervisor.is_alive():
        return
    _stop.clear()
    state["started_at"] = now()
    try:
        remembered = float(settings_store.load().get("_queue_avg_s") or 0)
        if 1.0 <= remembered <= 600.0 and not state["jobs"]:
            state["avg_s"] = remembered
    except (TypeError, ValueError):
        pass
    _supervisor = threading.Thread(target=_supervise, name="fives-queue-supervisor", daemon=True)
    _supervisor.start()


def stop():
    """ปิดคิว: ภาพที่กำลังวิเคราะห์อยู่ถูกคืนเข้าคิว ระบบเริ่มใหม่แล้วจะทำต่อเอง"""
    _stop.set()
    with _cond:
        _cond.notify_all()
    with _lock:
        mine = list(_inflight)
    if mine:
        try:
            with SessionLocal() as db:
                (db.query(Photo).filter(Photo.id.in_(mine), Photo.status == "processing")
                 .update({"status": "pending", "started_at": None}, synchronize_session=False))
                db.commit()
        except Exception:
            log.exception("release photos at shutdown")

"""รับภาพเข้าระบบ: ทุกแหล่ง (มือถือ เว็บแคม กล้อง IP โปรแกรมกล้อง) ผ่านจุดเดียวกัน จึงใช้กติกาชุดเดียวกัน"""
from datetime import datetime, timedelta

from fastapi import HTTPException

from . import config, imaging, notify, storage, worker
from sqlalchemy import func

from .db import AuditArea, Photo, PhotoImage, PhotoThumb, Round, now

SOURCES = {"mobile": "กล้องมือถือ", "gallery": "คลังภาพ", "webcam": "เว็บแคม", "ipcam": "กล้อง IP",
           "agent": "กล้อง IP (โปรแกรมในโรงงาน)"}


def area_types(s: dict) -> list:
    items = [str(x).strip() for x in (s.get("area_types") or []) if str(x).strip()]
    return items or ["อื่น ๆ"]


def area_label(value: str) -> str:
    return config.AREA_TYPES.get(value, value or "-")


def parse_shot_at(text: str):
    """เวลาที่ถ่ายภาพที่เครื่องของผู้ส่งอ่านได้จากไฟล์ (เวลาไทย) คืน None ถ้าไม่มีหรือไม่สมเหตุผล"""
    try:
        taken = datetime.strptime((text or "").strip()[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    local = now() + timedelta(hours=7)
    if taken > local + timedelta(hours=14) or taken.year < 2005:        # นาฬิกาของกล้องผิดชัดเจน: ถือว่าไม่ทราบ
        return None
    return taken


def create_photo(db, s: dict, *, rnd: Round, department_id: int, raw: bytes, area_name: str, area_type: str = "",
                 note: str = "", uploader_id=None, uploader_name: str = "", source: str = "mobile",
                 camera_id=None, after_of=None, area_id=None, enforce_area: bool = False, shot_at: str = "") -> Photo:
    if rnd is None or rnd.status != "open":
        raise HTTPException(400, "รอบการตรวจนี้ปิดรับภาพแล้ว")
    # จุดตรวจ: ใช้จุดที่โรงงานกำหนดก่อนเสมอ (เลือกจากรายการ หรือชื่อตรงกัน) จุดที่พิมพ์เองรับเมื่อผู้ดูแลอนุญาต
    area = None
    if area_id:
        area = db.get(AuditArea, int(area_id))
        if area is None or not area.active or area.department_id != department_id:
            raise HTTPException(400, "จุดตรวจที่เลือกไม่ใช่ของแผนกนี้ หรือถูกปิดใช้แล้ว")
    area_name = (area.name if area else (area_name or "")).strip()[:160]
    if not area_name:
        raise HTTPException(400, "ใส่ชื่อจุดตรวจก่อนส่งภาพ")
    defined = db.query(AuditArea).filter(AuditArea.department_id == department_id, AuditArea.active.is_(True))
    if area is None:
        area = defined.filter(AuditArea.name == area_name).first()
    if area is None and enforce_area and not s.get("allow_free_area", True) and defined.count():
        raise HTTPException(400, "เลือกจุดตรวจจากรายการที่โรงงานกำหนดไว้ให้แผนกนี้")
    if area is not None and area.area_type:
        area_type = area.area_type
    u = storage.usage(db, s)
    if u["level"] == "full":
        storage.check_alerts(db, s, u)
        raise HTTPException(507, "พื้นที่จัดเก็บเต็ม แจ้งผู้ดูแลระบบให้สำรองข้อมูลและลบภาพเก่าก่อน")
    cap = int(s.get("max_photos_per_dept", 0) or 0)
    if cap and db.query(func.count(Photo.id)).filter(Photo.round_id == rnd.id,
                                                    Photo.department_id == department_id).scalar() >= cap:
        raise HTTPException(400, f"แผนกนี้ส่งครบ {cap} ภาพของรอบนี้แล้ว ลบภาพที่ไม่ใช้ก่อนส่งเพิ่ม")
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "ไฟล์ภาพใหญ่เกิน 15 MB")
    try:
        img = imaging.process(raw, s["img_max_side"], s["img_quality"])
    except imaging.ImageError as e:
        raise HTTPException(400, str(e))
    dup = db.query(Photo.id).filter(Photo.round_id == rnd.id, Photo.sha256 == img["sha256"]).first()
    if dup:
        raise HTTPException(409, f"ภาพนี้ส่งแล้วในรอบนี้ (ภาพเลขที่ {dup[0]})")
    if after_of:
        before = db.get(Photo, int(after_of))
        if before is None or before.department_id != department_id:
            raise HTTPException(400, "ภาพก่อนแก้ไขที่อ้างถึงไม่ใช่ของแผนกนี้")
    taken, stale = parse_shot_at(shot_at), False
    limit = int(s.get("gallery_max_age_h", 0) or 0)
    if source == "gallery" and limit > 0 and taken is not None:
        age_h = (now() + timedelta(hours=7) - taken).total_seconds() / 3600
        if age_h > limit:
            if s.get("gallery_stale", "flag") == "reject":
                raise HTTPException(400, f"ภาพนี้ถ่ายไว้เมื่อ {taken.strftime('%d/%m/%Y %H:%M')} ซึ่งเกิน {limit} ชั่วโมง "
                                         "ถ่ายภาพใหม่ด้วยปุ่ม ถ่ายภาพ")
            stale = True
    allowed = set(area_types(s)) | set(config.AREA_TYPES)
    p = Photo(shot_at=taken, stale=stale,round_id=rnd.id, department_id=department_id, uploader_id=uploader_id, uploader_name=uploader_name[:120],
              area_name=area_name, area_type=area_type if area_type in allowed else area_types(s)[-1],
              note=(note or "").strip()[:1000], sha256=img["sha256"], width=img["width"], height=img["height"],
              image_bytes=len(img["image"]), thumb_bytes=len(img["thumb"]), status="pending",
              source=source if source in SOURCES else "mobile", camera_id=camera_id,
              after_of=int(after_of) if after_of else None, review_flag=False,
              area_id=area.id if area is not None else None, verified_by="")
    db.add(p)
    db.flush()
    db.add(PhotoImage(photo_id=p.id, data=img["image"]))
    db.add(PhotoThumb(photo_id=p.id, data=img["thumb"]))
    notify.emit(db, "upload", round_id=rnd.id, department_id=department_id, photo_id=p.id)
    db.commit()
    worker.wake()
    if u["level"] != "ok" or u["host_level"] != "ok":
        storage.check_alerts(db, s)
    return p

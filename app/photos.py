"""รับภาพเข้าระบบ: ทุกแหล่ง (มือถือ เว็บแคม กล้อง IP โปรแกรมกล้อง) ผ่านจุดเดียวกัน จึงใช้กติกาชุดเดียวกัน"""
from fastapi import HTTPException

from . import config, imaging, notify, storage, worker
from .db import Photo, PhotoImage, PhotoThumb, Round

SOURCES = {"mobile": "กล้องมือถือ", "gallery": "คลังภาพ", "webcam": "เว็บแคม", "ipcam": "กล้อง IP",
           "agent": "กล้อง IP (โปรแกรมในโรงงาน)"}


def area_types(s: dict) -> list:
    items = [str(x).strip() for x in (s.get("area_types") or []) if str(x).strip()]
    return items or ["อื่น ๆ"]


def area_label(value: str) -> str:
    return config.AREA_TYPES.get(value, value or "-")


def create_photo(db, s: dict, *, rnd: Round, department_id: int, raw: bytes, area_name: str, area_type: str = "",
                 note: str = "", uploader_id=None, uploader_name: str = "", source: str = "mobile",
                 camera_id=None, after_of=None) -> Photo:
    if rnd is None or rnd.status != "open":
        raise HTTPException(400, "รอบการตรวจนี้ปิดรับภาพแล้ว")
    area_name = (area_name or "").strip()[:160]
    if not area_name:
        raise HTTPException(400, "ใส่ชื่อจุดตรวจก่อนส่งภาพ")
    if storage.usage(db, s)["level"] == "full":
        raise HTTPException(507, "พื้นที่จัดเก็บเต็ม แจ้งผู้ดูแลระบบให้สำรองข้อมูลและลบภาพเก่าก่อน")
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
    allowed = set(area_types(s)) | set(config.AREA_TYPES)
    p = Photo(round_id=rnd.id, department_id=department_id, uploader_id=uploader_id, uploader_name=uploader_name[:120],
              area_name=area_name, area_type=area_type if area_type in allowed else area_types(s)[-1],
              note=(note or "").strip()[:1000], sha256=img["sha256"], width=img["width"], height=img["height"],
              image_bytes=len(img["image"]), thumb_bytes=len(img["thumb"]), status="pending",
              source=source if source in SOURCES else "mobile", camera_id=camera_id,
              after_of=int(after_of) if after_of else None, review_flag=False)
    db.add(p)
    db.flush()
    db.add(PhotoImage(photo_id=p.id, data=img["image"]))
    db.add(PhotoThumb(photo_id=p.id, data=img["thumb"]))
    notify.emit(db, "upload", round_id=rnd.id, department_id=department_id, photo_id=p.id)
    db.commit()
    worker.wake()
    return p

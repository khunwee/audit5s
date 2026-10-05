"""พื้นที่จัดเก็บ: วัดการใช้ ลบภาพเต็มตามนโยบาย (คะแนน คำอธิบาย และภาพย่อยังอยู่)"""
import logging
import os
from datetime import timedelta

from sqlalchemy import func, text

from .db import IS_SQLITE, Photo, PhotoImage, Round, engine, log, now

logger = logging.getLogger("fives.storage")
MB = 1024 * 1024
ROW_ESTIMATE = 6000      # ขนาดโดยประมาณของข้อมูลข้อความต่อ 1 ภาพ (ผลวิเคราะห์ JSON)


def physical_bytes(db):
    try:
        if IS_SQLITE:
            path = engine.url.database
            return sum(os.path.getsize(path + ext) for ext in ("", "-wal") if os.path.exists(path + ext))
        return int(db.execute(text("select pg_database_size(current_database())")).scalar())
    except Exception:
        return None


def usage(db, s: dict) -> dict:
    img_bytes, n_img = db.query(func.coalesce(func.sum(Photo.image_bytes), 0), func.count(Photo.id)) \
        .filter(Photo.has_image.is_(True)).one()
    thumb_bytes, n_all = db.query(func.coalesce(func.sum(Photo.thumb_bytes), 0), func.count(Photo.id)).one()
    logical = int(img_bytes) + int(thumb_bytes) + int(n_all) * ROW_ESTIMATE
    budget = max(20, int(s.get("storage_budget_mb", 350))) * MB
    pct = round(logical / budget * 100, 1)
    return dict(images=int(n_img), photos=int(n_all), image_bytes=int(img_bytes), logical=logical,
                budget=budget, pct=pct, physical=physical_bytes(db),
                level="full" if pct >= 100 else "warn" if pct >= 80 else "ok",
                avg_image=int(img_bytes / n_img) if n_img else 0)


def purge_images(db, photo_ids: list) -> int:
    """ลบเฉพาะไฟล์ภาพเต็มของภาพที่ระบุ"""
    done = 0
    for i in range(0, len(photo_ids), 200):
        chunk = photo_ids[i:i + 200]
        db.query(PhotoImage).filter(PhotoImage.photo_id.in_(chunk)).delete(synchronize_session=False)
        done += db.query(Photo).filter(Photo.id.in_(chunk), Photo.has_image.is_(True)) \
            .update({"has_image": False, "purged_at": now()}, synchronize_session=False)
    db.commit()
    return done


def _eligible(db, s: dict):
    q = db.query(Photo.id).join(Round, Round.id == Photo.round_id).filter(Photo.has_image.is_(True))
    if s.get("purge_requires_backup", True):
        q = q.filter(Round.last_backup_at.isnot(None), Photo.created_at <= Round.last_backup_at)
    return q


def auto_cleanup(db, s: dict) -> dict:
    """ลบภาพเต็มอัตโนมัติ 2 กรณี: เก่ากว่าอายุที่ตั้งไว้ และพื้นที่ใกล้เต็ม (เริ่มจากภาพเก่าสุด)"""
    by_age = by_space = 0
    days = int(s.get("retention_days", 0) or 0)
    if days > 0:
        ids = [r[0] for r in _eligible(db, s).filter(Photo.created_at < now() - timedelta(days=days)).all()]
        by_age = purge_images(db, ids) if ids else 0
    u = usage(db, s)
    guard = 0
    while u["logical"] > 0.9 * u["budget"] and guard < 200:
        guard += 1
        ids = [r[0] for r in _eligible(db, s).order_by(Photo.created_at).limit(50).all()]
        if not ids:
            break
        by_space += purge_images(db, ids)
        u = usage(db, s)
        if u["logical"] <= 0.75 * u["budget"]:
            break
    if by_age or by_space:
        log(db, "system", "auto_cleanup", f"ลบภาพเต็มตามอายุ {by_age} ภาพ, เพราะพื้นที่ใกล้เต็ม {by_space} ภาพ")
        db.commit()
        logger.info("auto cleanup: age=%s space=%s", by_age, by_space)
    return dict(by_age=by_age, by_space=by_space)


def compact(db) -> bool:
    """คืนพื้นที่ไฟล์หลังลบภาพ (SQLite เท่านั้น — Postgres นำพื้นที่ที่ลบแล้วกลับมาใช้เองอัตโนมัติ)"""
    if not IS_SQLITE:
        return False
    db.commit()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))
    return True

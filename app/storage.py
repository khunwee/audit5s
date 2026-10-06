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
    """การใช้พื้นที่ 2 มุม

    logical   ขนาดข้อมูลที่ระบบเก็บอยู่จริง เทียบกับ "งบพื้นที่" ที่ผู้ดูแลตั้ง ใช้ตัดสินว่ารับภาพต่อได้หรือไม่
    physical  ขนาดไฟล์ฐานข้อมูลที่ host นับ เทียบกับความจุของ host (Postgres ไม่คืนพื้นที่เองหลังลบ
              แต่นำกลับมาใช้กับภาพใหม่) ใช้แจ้งเตือนผู้ดูแลก่อนชนเพดานของ host
    """
    img_bytes, n_img = db.query(func.coalesce(func.sum(Photo.image_bytes), 0), func.count(Photo.id)) \
        .filter(Photo.has_image.is_(True)).one()
    thumb_bytes, n_all = db.query(func.coalesce(func.sum(Photo.thumb_bytes), 0), func.count(Photo.id)).one()
    logical = int(img_bytes) + int(thumb_bytes) + int(n_all) * ROW_ESTIMATE
    budget = max(20, int(s.get("storage_budget_mb", 350))) * MB
    pct = round(logical / budget * 100, 1)
    warn = max(10, min(int(s.get("storage_warn_pct", 80)), 99))
    physical = physical_bytes(db)
    host_limit = max(20, int(s.get("host_limit_mb", 500))) * MB
    host_pct = round(physical / host_limit * 100, 1) if (physical is not None and not IS_SQLITE) else None
    level = "full" if pct >= 100 else "warn" if pct >= warn else "ok"
    host_level = "ok" if host_pct is None else "critical" if host_pct >= 95 else "warn" if host_pct >= warn else "ok"
    return dict(images=int(n_img), photos=int(n_all), image_bytes=int(img_bytes), logical=logical,
                budget=budget, pct=pct, physical=physical, host_limit=host_limit, host_pct=host_pct,
                level=level, host_level=host_level, warn_pct=warn,
                avg_image=int(img_bytes / n_img) if n_img else 0)


def check_alerts(db, s: dict, u: dict = None) -> list:
    """แจ้งผู้ดูแลเมื่อพื้นที่ถึงเกณฑ์ (เรียกตอนรับภาพและในรอบตรวจทุก 6 ชั่วโมง) คืนรายการเรื่องที่แจ้ง"""
    from . import notify
    u = u or usage(db, s)
    sent = []
    how = "สำรองรอบเก่าแล้วลบภาพเต็มที่หน้า จัดการระบบ > พื้นที่และสำรองข้อมูล"
    if u["level"] == "full":
        if notify.alert_admin(db, "storage_full", f"พื้นที่จัดเก็บเต็ม ({u['pct']}% ของงบ) ระบบหยุดรับภาพใหม่แล้ว {how}", 3):
            sent.append("storage_full")
    elif u["level"] == "warn":
        if notify.alert_admin(db, "storage_warn", f"พื้นที่จัดเก็บใช้ไป {u['pct']}% ของงบ เก็บภาพเต็มอยู่ {u['images']} ภาพ "
                                                  f"ระบบจะหยุดรับภาพเมื่อถึง 100% {how}", 20):
            sent.append("storage_warn")
    if u["host_level"] != "ok":
        text = (f"ฐานข้อมูลบน host มีขนาด {u['physical'] / MB:.0f} MB จากความจุ {u['host_limit'] / MB:.0f} MB "
                f"({u['host_pct']}%) ลบภาพเต็มของรอบที่สำรองแล้ว จากนั้นกด คืนพื้นที่ที่ลบแล้ว ในหน้า พื้นที่และสำรองข้อมูล")
        if notify.alert_admin(db, "host_" + u["host_level"], text, 6 if u["host_level"] == "critical" else 20):
            sent.append("host_" + u["host_level"])
    return sent


def backup_reminders(db, s: dict) -> int:
    """แจ้งล่วงหน้าเมื่อมีภาพที่ยังไม่ได้สำรองกำลังจะครบอายุการเก็บ"""
    from . import notify
    days, ahead = int(s.get("retention_days", 0) or 0), int(s.get("backup_remind_days", 7) or 0)
    if days <= 0 or ahead <= 0:
        return 0
    edge = now() - timedelta(days=max(days - ahead, 0))
    n = 0
    for rnd in db.query(Round).all():
        q = db.query(func.count(Photo.id)).filter(Photo.round_id == rnd.id, Photo.has_image.is_(True),
                                                  Photo.created_at < edge)
        if rnd.last_backup_at is not None:
            q = q.filter(Photo.created_at > rnd.last_backup_at)
        count = q.scalar()
        if count:
            keep = "ระบบจะยังไม่ลบภาพที่ยังไม่สำรอง พื้นที่จึงจะไม่ถูกคืน" if s.get("purge_requires_backup", True) \
                else "ภาพจะถูกลบโดยไม่มีสำเนา"
            if notify.alert_admin(db, f"backup_{rnd.id}", f"รอบ \"{rnd.name}\" มีภาพ {count} ภาพที่ยังไม่ได้สำรอง "
                                  f"และจะครบอายุการเก็บ {days} วันภายใน {ahead} วัน {keep} "
                                  "ดาวน์โหลดไฟล์สำรองที่หน้า พื้นที่และสำรองข้อมูล", 24):
                n += 1
    return n


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
    # ภาพที่ยังรอ AI วิเคราะห์ต้องเก็บภาพเต็มไว้ก่อน ไม่ลบอัตโนมัติ
    q = (db.query(Photo.id).join(Round, Round.id == Photo.round_id)
         .filter(Photo.has_image.is_(True), Photo.status.notin_(["pending", "processing"])))
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
        from . import notify
        log(db, "system", "auto_cleanup", f"ลบภาพเต็มตามอายุ {by_age} ภาพ, เพราะพื้นที่ใกล้เต็ม {by_space} ภาพ")
        notify.alert_admin(db, "cleanup", f"ระบบลบภาพเต็มอัตโนมัติ {by_age + by_space} ภาพ (ครบอายุ {by_age}, พื้นที่ใกล้เต็ม {by_space}) "
                                          f"คะแนน คำอธิบาย และภาพย่อยังอยู่ ตอนนี้ใช้พื้นที่ {usage(db, s)['pct']}% ของงบ", 0)
        db.commit()
        logger.info("auto cleanup: age=%s space=%s", by_age, by_space)
    return dict(by_age=by_age, by_space=by_space)


def compact(db, full: bool = False) -> bool:
    """คืนพื้นที่ไฟล์หลังลบภาพ

    SQLite: VACUUM ทุกครั้ง (เร็ว) / Postgres: ทำเมื่อผู้ดูแลสั่งเท่านั้น (full=True) เพราะ VACUUM FULL ล็อกตารางภาพชั่วครู่
    """
    if not IS_SQLITE and not full:
        return False
    db.commit()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if IS_SQLITE:
            conn.execute(text("VACUUM"))
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        else:
            for table in ("photo_images", "photo_thumbs", "photos", "notify_events"):
                conn.execute(text(f"VACUUM FULL {table}"))
    return True

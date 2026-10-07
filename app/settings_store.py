"""ค่าตั้งของระบบที่ admin แก้ได้จากหน้าเว็บ (เก็บในตาราง settings)"""
import json
import threading

from .db import SessionLocal, Setting

DEFAULTS = {
    "org_name": "โรงงานของเรา",
    # AI หลัก / AI สำรอง : none | gemini | openai | demo
    "ai1_type": "none", "ai1_base": "", "ai1_key": "", "ai1_model": "",
    "ai2_type": "none", "ai2_base": "", "ai2_key": "", "ai2_model": "",
    "ai_rpm": 6,              # เรียก AI ได้กี่ครั้งต่อนาที
    "ai_daily": 200,          # เพดานต่อวัน (กันชนโควตาฟรี)
    "ai_max_attempts": 4,
    "img_max_side": 1280,     # ด้านยาวสุดของภาพที่เก็บ (px)
    "img_quality": 78,
    "allow_gallery": True,    # ให้เลือกรูปจากคลังภาพได้ (ปิด = ปุ่มถ่ายจากกล้องเท่านั้น)
    "storage_budget_mb": 350,
    "retention_days": 90,     # 0 = ไม่ลบตามอายุ
    "purge_requires_backup": True,
    "ranking_visibility": "always",   # always | closed
    "member_see_all": True,
    # ---- รุ่น 1.1
    "public_url": "",                 # ที่อยู่เว็บของระบบ ใช้ทำลิงก์ในข้อความแจ้งเตือน
    "area_types": ["สายการผลิต", "คลังสินค้า / สโตร์", "ซ่อมบำรุง / ห้องเครื่อง", "สำนักงาน",
                   "ห้อง QC / ห้องแล็บ", "พื้นที่ส่วนกลาง / ทางเดิน", "อื่น ๆ"],
    "band_good": 80,                  # คะแนนตั้งแต่ค่านี้ขึ้นไปแสดงสีเขียว
    "band_mid": 60,                   # ตั้งแต่ค่านี้แสดงสีเหลือง ต่ำกว่านี้สีแดง
    "ai_extra": "",                   # มาตรฐานเฉพาะของโรงงานที่ต้องการให้ AI ยึด
    "ai_passes": 1,                   # 2 = ให้ AI ประเมินสองรอบแล้วเทียบกัน (ใช้โควตาสองเท่า)
    "after_replaces": True,           # จุดที่มีภาพหลังแก้ไข นับเฉพาะภาพล่าสุด
    "agent_token": "",                # รหัสของโปรแกรมกล้องบนเครื่องในโรงงาน
    "webcam_width": 1920,
    # ---- รุ่น 1.2
    "allow_free_area": True,          # ให้พิมพ์ชื่อจุดตรวจเองได้ นอกเหนือจากจุดที่โรงงานกำหนด
    "require_coverage": False,        # จัดอันดับเฉพาะแผนกที่ส่งภาพครบทุกจุดตรวจบังคับ
    "verify_required": False,         # นับคะแนนเฉพาะภาพที่หัวหน้าหรือกรรมการยืนยันแล้ว
    "host_limit_mb": 500,             # ความจุฐานข้อมูลที่ host ให้ (Neon ฟรี = 500 MB)
    "storage_warn_pct": 80,           # เริ่มแจ้งผู้ดูแลเมื่อใช้พื้นที่ถึงกี่ %
    "max_photos_per_dept": 0,         # จำนวนภาพสูงสุดต่อแผนกต่อรอบ (0 = ไม่จำกัด)
    "backup_remind_days": 7,          # แจ้งล่วงหน้ากี่วันก่อนภาพที่ยังไม่สำรองจะครบอายุ
    # ---- รุ่น 1.3
    "scoring_mode": "level",          # วิธีให้คะแนนของรอบที่สร้างใหม่: level | checklist
    "auto_actions": True,             # ยืนยันผลแล้วสร้างงานแก้ไขจากข้อที่ไม่ผ่านให้อัตโนมัติ
    "action_due_days": 7,             # กำหนดเสร็จของข้อบกพร่องเล็กน้อย (วัน)
    "action_due_days_major": 3,       # กำหนดเสร็จของข้อบกพร่องมาก (วัน)
    # ---- รุ่น 1.4: จอแสดงผล (TV)
    "tv_title": "",                   # หัวเรื่องบนจอ ว่าง = ใช้ชื่อโรงงาน
    "tv_title_en": "",
    "tv_lang": "th",                  # ภาษาตั้งต้นของจอ: th | en
    "tv_theme": "light",              # light = พื้นสว่าง เหมาะกับโรงงานที่แสงมาก | dark = พื้นเข้ม
    "tv_slides": ["ranking", "trend", "ranks", "history", "category", "actions"],
    "tv_seconds": 15,                 # เวลาต่อหนึ่งหน้า (วินาที)
    "tv_rows": 8,                     # จำนวนแผนกต่อหน้า
    "tv_rounds": 6,                   # จำนวนรอบย้อนหลังในกราฟ
    "tv_round": "auto",               # auto = ตามการตั้งค่าการเปิดเผยอันดับ | open = รอบที่เปิดอยู่ | closed = รอบที่ปิดล่าสุด
    "tv_refresh_min": 5,              # จอถามข้อมูลใหม่ทุกกี่นาที
    "tv_hours": "",                   # ช่วงเวลาที่จออัปเดตข้อมูล เช่น 07:00-19:00 ว่าง = ทั้งวัน
    "tv_clock": True,
    "tv_unranked": True,              # แสดงแผนกที่ยังไม่ถูกจัดอันดับ
    # ---- รุ่น 1.5: ตารางเวลาของกล้อง และรอบอัตโนมัติ
    "cam_schedule": {"times": [], "random": 0, "between": "08:30-16:30", "days": [0, 1, 2, 3, 4]},   # ค่ากลางของทุกกล้อง
    "cam_holidays": [],               # วันที่ไม่ถ่ายอัตโนมัติ (ปปปป-ดด-วว)
    "cam_grace_min": 20,              # เลยเวลาถ่ายไม่เกินกี่นาทียังถ่ายชดเชย
    "rounds_repeat": "off",           # สร้างรอบถัดไปเอง: off | weekly | monthly
    "rounds_repeat_prefix": "ตรวจ 5ส",
    # ---- รุ่น 1.6
    "preset": "",                     # ชุดตั้งค่าเริ่มต้นที่เลือกใช้: factory | office | ว่าง = ยังไม่ได้เลือก
    # ---- รุ่น 1.7
    "gallery_max_age_h": 0,           # ภาพจากคลังภาพต้องถ่ายไว้ไม่เกินกี่ชั่วโมง (0 = ไม่ตรวจ)
    "gallery_stale": "flag",          # ภาพที่เก่าเกิน: flag = รับแต่ติดป้ายให้กรรมการเห็น | reject = ไม่รับ
    "dept_remind_days": 3,            # เตือนแผนกที่ยังส่งไม่ครบ ก่อนวันสิ้นสุดรอบกี่วัน (0 = ไม่เตือน)
    "tv_group": "",                   # จอแสดงผล: แสดงเฉพาะกลุ่มนี้ (ว่าง = ทุกแผนก)
}

_lock = threading.Lock()
_cache = None


def load(force: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)
        data = dict(DEFAULTS)
        with SessionLocal() as db:
            for row in db.query(Setting).all():
                try:
                    data[row.key] = json.loads(row.value)
                except Exception:
                    data[row.key] = row.value
        _cache = data
        return dict(data)


def save(db, values: dict):
    global _cache
    for k, v in values.items():
        row = db.get(Setting, k)
        if row is None:
            db.add(Setting(key=k, value=json.dumps(v, ensure_ascii=False)))
        else:
            row.value = json.dumps(v, ensure_ascii=False)
    db.commit()
    with _lock:
        _cache = None


def get_or_create_secret() -> str:
    import secrets
    s = load()
    if s.get("_secret"):
        return s["_secret"]
    value = secrets.token_hex(32)
    with SessionLocal() as db:
        save(db, {"_secret": value})
    return value

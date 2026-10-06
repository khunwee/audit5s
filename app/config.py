"""ค่าตั้งต้นของระบบที่อ่านจาก environment (ที่เหลือตั้งในหน้า ตั้งค่า ของแอป)"""
import os
from pathlib import Path

APP_NAME = "5ส Vision"
APP_VERSION = "1.5.0"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))

# เว้นว่าง = ใช้ SQLite ในโฟลเดอร์ data/ (เหมาะกับเครื่องตัวเอง / เครื่องในโรงงาน)
# ใส่ postgresql://... = ใช้ Postgres (เช่น Neon) สำหรับ host ฟรีที่ดิสก์ไม่ถาวร
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0").strip() in ("1", "true", "True")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
DISABLE_WORKER = os.getenv("DISABLE_WORKER", "0") == "1"   # ใช้ตอนรันชุดทดสอบ

MAX_UPLOAD_BYTES = 15 * 1024 * 1024      # ต่อ 1 ภาพ (ก่อนบีบอัด)
MAX_RESTORE_BYTES = 600 * 1024 * 1024    # ไฟล์ ZIP ที่นำกลับเข้าระบบ

# รหัสประเภทพื้นที่ของรุ่น 1.0 (รุ่น 1.1 ผู้ดูแลตั้งรายการเองในหน้าตั้งค่า และเก็บเป็นข้อความ)
AREA_TYPES = {
    "production": "สายการผลิต",
    "warehouse": "คลังสินค้า / สโตร์",
    "maintenance": "ซ่อมบำรุง / ห้องเครื่อง",
    "office": "สำนักงาน",
    "qc": "ห้อง QC / ห้องแล็บ",
    "common": "พื้นที่ส่วนกลาง / ทางเดิน",
    "other": "อื่น ๆ",
}

ROLES = {
    "admin": "ผู้ดูแลระบบ",
    "auditor": "กรรมการ 5ส",
    "member": "ตัวแทนแผนก",
}


def db_url() -> str:
    url = DATABASE_URL
    if not url:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(DATA_DIR / 'fives.db').as_posix()}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url

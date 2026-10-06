"""ฐานข้อมูล: ตาราง, การเชื่อมต่อ, การอัปเกรดคอลัมน์อัตโนมัติ และข้อมูลตั้งต้น"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey,
                        Index, Integer, LargeBinary, String, Text, create_engine,
                        event, inspect, text)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from . import config

URL = config.db_url()
IS_SQLITE = URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_engine(URL, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _rec):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
else:
    # prepare_threshold=None: ปลอดภัยกับ connection pooler (Neon -pooler / PgBouncer)
    engine = create_engine(URL, pool_pre_ping=True, pool_recycle=280, pool_size=3,
                           max_overflow=5, connect_args={"prepare_threshold": None})

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
Base = declarative_base()


def now() -> datetime:
    """เวลาปัจจุบันแบบ UTC (ไม่มี tzinfo) — แปลงเป็นเวลาไทยตอนแสดงผล"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True)
    code = Column(String(20), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    zone = Column(String(120), default="")
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    full_name = Column(String(120), default="")
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="member", nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    must_change = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=now)
    last_login = Column(DateTime, nullable=True)
    perms = Column(JSON, nullable=True)          # สิทธิ์ที่ตั้งต่างจากบทบาท {"export": true, ...}
    extra_depts = Column(JSON, nullable=True)    # แผนกเพิ่มเติมที่ส่งภาพและดูผลได้
    department = relationship("Department", lazy="joined")


class Criterion(Base):
    """เกณฑ์ 5ส ชุดปัจจุบัน (แต่ละรอบการตรวจจะคัดลอกไปเก็บเป็นของตัวเอง)"""
    __tablename__ = "criteria"
    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    focus = Column(Text, default="")
    max_score = Column(Float, default=20, nullable=False)
    levels = Column(JSON, default=list)          # ข้อความอธิบายระดับ 0..4
    allow_na = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    active = Column(Boolean, default=True, nullable=False)


class Round(Base):
    __tablename__ = "rounds"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    note = Column(Text, default="")
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    status = Column(String(12), default="open", nullable=False)   # open | closed
    min_photos = Column(Integer, default=3, nullable=False)
    rubric = Column(JSON, default=list)
    created_at = Column(DateTime, default=now)
    closed_at = Column(DateTime, nullable=True)
    last_backup_at = Column(DateTime, nullable=True)


class Photo(Base):
    __tablename__ = "photos"
    id = Column(Integer, primary_key=True)
    round_id = Column(Integer, ForeignKey("rounds.id"), nullable=False, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False, index=True)
    uploader_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploader_name = Column(String(120), default="")
    area_name = Column(String(160), default="")
    area_type = Column(String(60), default="other")
    note = Column(Text, default="")
    created_at = Column(DateTime, default=now, index=True)
    sha256 = Column(String(64), default="")
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    image_bytes = Column(Integer, default=0)
    thumb_bytes = Column(Integer, default=0)
    has_image = Column(Boolean, default=True, nullable=False)
    purged_at = Column(DateTime, nullable=True)
    # pending | processing | done | rejected | error
    status = Column(String(12), default="pending", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    next_try_at = Column(DateTime, nullable=True)
    error = Column(Text, default="")
    provider = Column(String(30), default="")
    model = Column(String(120), default="")
    analyzed_at = Column(DateTime, nullable=True)
    score = Column(Float, nullable=True)
    max_score = Column(Float, nullable=True)
    percent = Column(Float, nullable=True)
    analysis = Column(JSON, nullable=True)
    overridden = Column(Boolean, default=False, nullable=False)
    override_by = Column(String(120), default="")
    override_note = Column(Text, default="")
    override_at = Column(DateTime, nullable=True)
    source = Column(String(20), default="mobile")       # mobile | gallery | webcam | ipcam | agent
    camera_id = Column(Integer, nullable=True)
    after_of = Column(Integer, nullable=True)           # ภาพนี้คือภาพหลังแก้ไขของภาพเลขที่...
    review_flag = Column(Boolean, default=False)        # AI สองรอบให้ผลต่างกันมาก ควรให้กรรมการดู
    area_id = Column(Integer, nullable=True)            # จุดตรวจที่โรงงานกำหนด (ว่าง = จุดที่พิมพ์ชื่อเอง)
    verified_by = Column(String(120), default="")       # หัวหน้าหรือกรรมการที่ดูภาพแล้วยืนยันผล
    verified_at = Column(DateTime, nullable=True)
    department = relationship("Department", lazy="joined")

    __table_args__ = (Index("ix_photos_round_sha", "round_id", "sha256"),)


class PhotoImage(Base):
    """ภาพเต็ม (บีบอัดแล้ว) — แยกตารางเพื่อให้ลบภาพได้โดยคะแนนและคำอธิบายยังอยู่"""
    __tablename__ = "photo_images"
    photo_id = Column(Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True)
    data = Column(LargeBinary, nullable=False)


class PhotoThumb(Base):
    __tablename__ = "photo_thumbs"
    photo_id = Column(Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True)
    data = Column(LargeBinary, nullable=False)


class DeptSummary(Base):
    __tablename__ = "dept_summaries"
    id = Column(Integer, primary_key=True)
    round_id = Column(Integer, ForeignKey("rounds.id"), nullable=False, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    content = Column(JSON, nullable=True)
    model = Column(String(120), default="")
    created_at = Column(DateTime, default=now)


class AuditArea(Base):
    """จุดตรวจที่โรงงานกำหนดให้แต่ละแผนก พร้อมมาตรฐานของจุดนั้น (ส่งให้ AI ใช้ประกอบการประเมิน)"""
    __tablename__ = "audit_areas"
    id = Column(Integer, primary_key=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    area_type = Column(String(60), default="")
    standard = Column(Text, default="")
    required = Column(Boolean, default=True, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    department = relationship("Department", lazy="joined")


class Channel(Base):
    """ช่องทางแจ้งเตือน: ผูกกับแผนกเดียว หรือรับของทุกแผนก"""
    __tablename__ = "notify_channels"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    kind = Column(String(20), nullable=False)
    config = Column(JSON, default=dict)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    events = Column(JSON, default=list)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now)
    last_ok_at = Column(DateTime, nullable=True)
    last_error = Column(Text, default="")
    department = relationship("Department", lazy="joined")


class NotifyEvent(Base):
    __tablename__ = "notify_events"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False)
    round_id = Column(Integer, nullable=True)
    department_id = Column(Integer, nullable=True)
    photo_id = Column(Integer, nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=now)
    done = Column(Boolean, default=False, nullable=False, index=True)


class NotifyLog(Base):
    __tablename__ = "notify_logs"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now, index=True)
    channel = Column(String(120), default="")
    kind = Column(String(20), default="")
    ok = Column(Boolean, default=True, nullable=False)
    detail = Column(Text, default="")


class Camera(Base):
    """กล้อง IP: direct = เซิร์ฟเวอร์ดึงภาพเอง, agent = โปรแกรมบนเครื่องในโรงงานดึงแล้วส่งขึ้นมา"""
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    area_name = Column(String(160), default="")
    area_type = Column(String(60), default="")
    mode = Column(String(10), default="direct")        # direct | agent
    source = Column(String(10), default="snapshot")    # snapshot | rtsp
    url = Column(Text, default="")
    username = Column(String(120), default="")
    password = Column(String(200), default="")
    auth = Column(String(10), default="basic")         # basic | digest
    active = Column(Boolean, default=True, nullable=False)
    last_capture_at = Column(DateTime, nullable=True)
    last_error = Column(Text, default="")
    department = relationship("Department", lazy="joined")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=now, index=True)
    username = Column(String(60), default="")
    action = Column(String(60), default="")
    detail = Column(Text, default="")


class AiUsage(Base):
    __tablename__ = "ai_usage"
    day = Column(String(10), primary_key=True)
    count = Column(Integer, default=0, nullable=False)


def log(db, user, action: str, detail: str = ""):
    name = user if isinstance(user, str) else (user.username if user else "system")
    db.add(AuditLog(username=name, action=action, detail=(detail or "")[:2000]))


DEFAULT_CRITERIA = [
    dict(code="S1", name="สะสาง (Seiri)", max_score=20, allow_na=False,
         focus="ของที่ไม่จำเป็น ของเสีย ของชำรุด ของส่วนเกิน ของส่วนตัว ที่ปะปนอยู่บนพื้น โต๊ะ ชั้นวาง หรือรอบเครื่องจักร",
         levels=["พื้นที่เต็มไปด้วยของที่ไม่จำเป็นหรือของเสีย ไม่มีการคัดแยกเลย",
                 "มีของไม่จำเป็นจำนวนมาก ปะปนกับของใช้งาน หรือกีดขวางพื้นที่ทำงานและทางเดิน",
                 "พบของไม่จำเป็นหลายจุด (ประมาณ 3-5 จุด) หรือมีของส่วนเกินกองอยู่บางส่วน",
                 "พบของไม่จำเป็นเล็กน้อย 1-2 จุด และไม่กีดขวางการทำงาน",
                 "ไม่พบของที่ไม่จำเป็นในภาพ มีเฉพาะของที่ใช้งานในปริมาณเหมาะสม"]),
    dict(code="S2", name="สะดวก (Seiton)", max_score=20, allow_na=False,
         focus="ทุกอย่างมีที่วางประจำ มีป้ายชี้บ่ง มีเส้นแบ่งพื้นที่ จัดวางเป็นแนวเป็นระเบียบ หยิบใช้และเก็บคืนได้ง่าย",
         levels=["วางของปะปนกันไม่มีที่ประจำ ไม่มีป้ายหรือเส้นแบ่งใด ๆ",
                 "ของส่วนใหญ่ไม่อยู่ในตำแหน่ง วางซ้อนหรือวางบนพื้นโดยไม่มีการกำหนดที่วาง ป้ายชี้บ่งแทบไม่มี",
                 "มีการกำหนดที่วางบางส่วน แต่พบของวางผิดที่หรือไม่มีป้ายชี้บ่งหลายจุด",
                 "ของส่วนใหญ่อยู่ในที่ประจำและมีป้ายชี้บ่ง พบจุดที่วางไม่ตรงตำแหน่งหรือป้ายขาดเพียง 1-2 จุด",
                 "ทุกอย่างอยู่ในที่ประจำ มีป้ายชี้บ่งและเส้นแบ่งชัดเจน จัดวางเป็นแนวเดียวกัน"]),
    dict(code="S3", name="สะอาด (Seiso)", max_score=20, allow_na=False,
         focus="ฝุ่น คราบน้ำมัน คราบสกปรก เศษวัสดุ ขยะ บนพื้น เครื่องจักร โต๊ะ ชั้นวาง และอุปกรณ์",
         levels=["สกปรกมาก มีขยะ เศษวัสดุ หรือคราบน้ำมันทั่วพื้นที่",
                 "พบคราบสกปรก ฝุ่นสะสม หรือเศษวัสดุเห็นได้ชัดหลายบริเวณ",
                 "สะอาดพอใช้ แต่พบฝุ่น คราบ หรือเศษวัสดุหลายจุด",
                 "สะอาดโดยรวม พบฝุ่นหรือคราบเล็กน้อย 1-2 จุด",
                 "พื้น เครื่องจักร และอุปกรณ์สะอาด ไม่พบฝุ่น คราบ หรือเศษวัสดุ"]),
    dict(code="S4", name="สุขลักษณะ / สร้างมาตรฐาน (Seiketsu)", max_score=20, allow_na=False,
         focus="มาตรฐานที่มองเห็นได้ เช่น สีและเส้นพื้น ป้ายมาตรฐาน การควบคุมด้วยสายตา ถังขยะแยกประเภท ที่เก็บอุปกรณ์ทำความสะอาด แสงสว่าง และทางหนีไฟหรือถังดับเพลิงไม่ถูกกีดขวาง",
         levels=["ไม่เห็นมาตรฐานใด ๆ และพบสภาพที่ไม่ปลอดภัยหรือไม่ถูกสุขลักษณะชัดเจน",
                 "แทบไม่มีมาตรฐานที่มองเห็นได้ เส้นพื้นหรือป้ายลบเลือนเสียหายเป็นส่วนใหญ่",
                 "มีมาตรฐานบางอย่าง แต่ไม่ครบหรือไม่เป็นแบบเดียวกัน เส้นหรือป้ายบางส่วนชำรุด",
                 "มีมาตรฐานที่มองเห็นได้เกือบครบ พบจุดที่ชำรุดหรือไม่เป็นไปตามมาตรฐาน 1-2 จุด",
                 "มาตรฐานชัดเจนและเป็นแบบเดียวกันทั้งพื้นที่ เส้นและป้ายอยู่ในสภาพดี ไม่มีสิ่งกีดขวางอุปกรณ์ความปลอดภัย"]),
    dict(code="S5", name="สร้างนิสัย (Shitsuke)", max_score=20, allow_na=True,
         focus="หลักฐานการรักษามาตรฐานอย่างต่อเนื่อง เช่น บอร์ด 5ส ตารางเวรทำความสะอาด ใบตรวจเช็กที่เป็นปัจจุบัน การแต่งกายและอุปกรณ์ป้องกันของพนักงาน และไม่มีร่องรอยการปล่อยปละละเลยสะสม",
         levels=["เห็นร่องรอยการปล่อยปละละเลยสะสมเป็นเวลานานทั่วพื้นที่",
                 "ไม่มีหลักฐานการดูแลต่อเนื่อง หรือพบการไม่ปฏิบัติตามกฎที่เห็นได้ชัด (เช่น ไม่สวมอุปกรณ์ป้องกัน)",
                 "มีหลักฐานการดูแลบางส่วน แต่เอกสารหรือบอร์ดไม่เป็นปัจจุบัน หรือปฏิบัติไม่สม่ำเสมอ",
                 "มีหลักฐานการดูแลต่อเนื่องและปฏิบัติตามกฎเป็นส่วนใหญ่ พบจุดบกพร่องเล็กน้อย",
                 "เห็นหลักฐานการดูแลต่อเนื่องชัดเจน เอกสารหรือบอร์ดเป็นปัจจุบัน และทุกคนในภาพปฏิบัติตามกฎ"]),
]


def rubric_snapshot(db) -> list:
    rows = (db.query(Criterion).filter(Criterion.active.is_(True))
            .order_by(Criterion.sort_order, Criterion.id).all())
    out = []
    for c in rows:
        levels = list(c.levels or [])
        levels = (levels + [""] * 5)[:5]
        out.append(dict(code=c.code, name=c.name, focus=c.focus or "", max=float(c.max_score),
                        levels=levels, allow_na=bool(c.allow_na)))
    return out


def _migrate_columns():
    """เพิ่มคอลัมน์ที่มีในโค้ดแต่ยังไม่มีในฐานข้อมูล (อัปเกรดเวอร์ชันโดยข้อมูลเดิมยังอยู่)"""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    coltype = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
                    default = col.default.arg if col.default is not None and col.default.is_scalar else None
                    if default is not None:      # แถวเดิมได้ค่าตั้งต้นของคอลัมน์ใหม่
                        conn.execute(text(f'UPDATE "{table.name}" SET "{col.name}" = :v'), {"v": default})


def init_db():
    from .security import hash_password
    Base.metadata.create_all(engine)
    _migrate_columns()
    with SessionLocal() as db:
        if db.query(User).count() == 0:
            db.add(User(username="admin", full_name="ผู้ดูแลระบบ", role="admin",
                        password_hash=hash_password(config.ADMIN_PASSWORD), must_change=True))
        if db.query(Criterion).count() == 0:
            for i, c in enumerate(DEFAULT_CRITERIA):
                db.add(Criterion(sort_order=(i + 1) * 10, **c))
        db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

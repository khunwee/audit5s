"""ตัวช่วยของหน้าเว็บ: เทมเพลต ตัวกรองการแสดงผล ข้อความแจ้ง"""
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import config, security, settings_store, storage
from .photos import SOURCES, area_label, area_types

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
BKK = timedelta(hours=7)

STATUS = {
    "pending": "รอ AI วิเคราะห์",
    "processing": "กำลังวิเคราะห์",
    "done": "ให้คะแนนแล้ว",
    "rejected": "ภาพใช้ประเมินไม่ได้",
    "error": "วิเคราะห์ไม่สำเร็จ",
}


def to_bkk(v: datetime):
    return v + BKK if v else None


def f_dt(v):
    return (v + BKK).strftime("%d/%m/%Y %H:%M") if v else "-"


def f_date(v):
    if not v:
        return "-"
    if isinstance(v, datetime):
        v = (v + BKK).date()
    return v.strftime("%d/%m/%Y") if isinstance(v, date) else str(v)


def f_num(v, nd=1):
    if v is None:
        return "-"
    return f"{float(v):.{nd}f}".rstrip("0").rstrip(".")


def f_mb(v):
    if v is None:
        return "-"
    v = float(v)
    return f"{v / 1024:.0f} KB" if v < 1024 * 1024 else f"{v / 1024 / 1024:.1f} MB"


def band(pct):
    """สีของคะแนน: เกณฑ์เขียว/เหลืองผู้ดูแลตั้งได้ในหน้าตั้งค่า"""
    if pct is None:
        return "none"
    s = settings_store.load()
    return "good" if pct >= s.get("band_good", 80) else "mid" if pct >= s.get("band_mid", 60) else "low"


def f_short(name):
    """ชื่อเกณฑ์แบบสั้นสำหรับพื้นที่แคบ: 'สุขลักษณะ / สร้างมาตรฐาน (Seiketsu)' -> 'สุขลักษณะ'"""
    return str(name).split(" (")[0].split(" / ")[0].strip()


templates.env.filters.update(dt=f_dt, d=f_date, num=f_num, mb=f_mb, short=f_short)
templates.env.globals.update(AREA_TYPES=config.AREA_TYPES, ROLES=config.ROLES, STATUS=STATUS,
                             APP_NAME=config.APP_NAME, APP_VERSION=config.APP_VERSION, band=band,
                             can=security.can, manages=security.manages, PERMS=security.PERMS,
                             area_label=area_label, SOURCES=SOURCES)


def flash(request, message: str, kind: str = "ok"):
    items = request.session.get("flash") or []
    items.append([kind, message])
    request.session["flash"] = items[-5:]


def render(request, name: str, user=None, db=None, status_code: int = 200, **ctx):
    s = settings_store.load()
    ctx.update(user=user, org_name=s.get("org_name", ""), settings=s, area_types=area_types(s),
               flashes=request.session.pop("flash", []), path=request.url.path)
    if user is not None and security.can(user, "storage") and db is not None:
        ctx["storage_level"] = storage.usage(db, s)["level"]
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)

"""จอแสดงผล (TV) และการเปลี่ยนภาษา

จอ TV ไม่มีคนเข้าสู่ระบบ จึงเปิดด้วยลิงก์ที่มีรหัสของจอ (tv_key) ที่ผู้ดูแลสร้างให้ ผู้ใช้ที่เข้าสู่ระบบอยู่และมีสิทธิ์ดูผลของ
ทุกแผนกก็เปิดได้โดยไม่ต้องใช้รหัส
"""
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, security, settings_store, tvdata
from .db import User, get_db, log
from .security import admin_user
from .web import flash, render, templates

router = APIRouter()


def _session_user(request: Request, db):
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active or user.must_change or request.session.get("stamp") != security.session_stamp(user):
        return None
    return user


def tv_allowed(request: Request, db) -> str:
    """คืน 'key' เมื่อเปิดด้วยรหัสของจอ, 'user' เมื่อเป็นผู้ใช้ที่มีสิทธิ์, '' เมื่อไม่ได้รับอนุญาต"""
    s = settings_store.load()
    key = request.query_params.get("key") or request.cookies.get("tvkey") or ""
    if key and s.get("tv_key") and hmac.compare_digest(key.encode(), str(s["tv_key"]).encode()):
        return "key"
    user = _session_user(request, db)
    if user is not None:
        from .routes_main import can_view_dept
        if can_view_dept(user, -1, s) and (s.get("ranking_visibility", "always") == "always"
                                           or security.can(user, "rank_live") or s.get("tv_round") == "closed"):
            return "user"
    return ""


@router.get("/tv", response_class=HTMLResponse)
def tv_page(request: Request, db=Depends(get_db)):
    how = tv_allowed(request, db)
    if not how:
        if _session_user(request, db) is None and not request.query_params.get("key") and not request.cookies.get("tvkey"):
            return RedirectResponse("/login", 303)
        raise HTTPException(403, "ลิงก์ของจอแสดงผลไม่ถูกต้องหรือถูกเปลี่ยนแล้ว ขอลิงก์ใหม่จากผู้ดูแลระบบ")
    s = settings_store.load()
    resp = templates.TemplateResponse(request, "tv.html", dict(cfg=tvdata.display_config(s), version=config.APP_VERSION,
                                                               slides=tvdata.SLIDES, via=how))
    if how == "key" and request.query_params.get("key"):
        # เก็บรหัสไว้ในคุกกี้ของจอนี้ จอจึงขอข้อมูลต่อได้โดยไม่ต้องมีรหัสในทุกคำขอ
        resp.set_cookie("tvkey", request.query_params["key"], max_age=400 * 86400, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE)
    return resp


@router.get("/api/tv/data")
def tv_data(request: Request, rounds: int = 0, db=Depends(get_db)):
    if not tv_allowed(request, db):
        raise HTTPException(403, "ลิงก์ของจอแสดงผลไม่ถูกต้องหรือถูกเปลี่ยนแล้ว")
    return JSONResponse(tvdata.get(db, rounds), headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- ตั้งค่าจอ (ผู้ดูแลระบบ)
@router.get("/admin/tv", response_class=HTMLResponse)
def tv_admin(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    s = settings_store.load()
    if not s.get("tv_key"):
        settings_store.save(db, {"tv_key": secrets.token_urlsafe(18)})
        s = settings_store.load()
    base = (s.get("public_url") or str(request.base_url)).rstrip("/")
    return render(request, "admin/tv.html", user, db, s=s, slides=tvdata.SLIDES, link=f"{base}/tv?key={s['tv_key']}")


def _hours(v: str) -> str:
    import re
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", v or "")
    if not m:
        return ""
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
        return ""
    return f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"


@router.post("/admin/tv")
async def tv_admin_save(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    form = await request.form()

    def num(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(form.get(name) or default)))
        except ValueError:
            return default
    slides = [x for x in form.getlist("slides") if x in tvdata.SLIDES] or ["ranking"]
    settings_store.save(db, {
        "tv_title": (form.get("tv_title") or "").strip()[:80], "tv_title_en": (form.get("tv_title_en") or "").strip()[:80],
        "tv_lang": "en" if form.get("tv_lang") == "en" else "th",
        "tv_theme": "dark" if form.get("tv_theme") == "dark" else "light",
        "tv_slides": slides, "tv_seconds": num("tv_seconds", 15, 5, 300), "tv_rows": num("tv_rows", 8, 3, 20),
        "tv_rounds": num("tv_rounds", 6, 2, 12),
        "tv_round": form.get("tv_round") if form.get("tv_round") in ("auto", "open", "closed") else "auto",
        "tv_refresh_min": num("tv_refresh_min", 5, 1, 120), "tv_hours": _hours(form.get("tv_hours") or ""),
        "tv_clock": form.get("tv_clock") == "1", "tv_unranked": form.get("tv_unranked") == "1",
        "tv_group": (form.get("tv_group") or "").strip()[:60]})
    log(db, user, "save_tv_settings", f"หน้า: {', '.join(slides)}")
    db.commit()
    flash(request, "บันทึกการตั้งค่าจอแสดงผลแล้ว จอที่เปิดอยู่จะใช้ค่าใหม่เมื่อโหลดหน้าใหม่")
    return RedirectResponse("/admin/tv", 303)


@router.post("/admin/tv/key")
def tv_admin_key(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    settings_store.save(db, {"tv_key": secrets.token_urlsafe(18)})
    log(db, user, "rotate_tv_key", "สร้างลิงก์ของจอแสดงผลใหม่")
    db.commit()
    flash(request, "สร้างลิงก์ใหม่แล้ว ลิงก์เดิมใช้ไม่ได้ทันที เปิดลิงก์ใหม่บนจอทุกเครื่อง")
    return RedirectResponse("/admin/tv", 303)


# --------------------------------------------------------------------------- เปลี่ยนภาษาของหน้าจอ
@router.get("/lang/{code}")
def set_language(code: str, request: Request, next: str = "/"):
    target = next if next.startswith("/") and not next.startswith("//") and "\\" not in next else "/"
    resp = RedirectResponse(target, 303)
    resp.set_cookie("lang", "en" if code == "en" else "th", max_age=400 * 86400, samesite="lax", secure=config.COOKIE_SECURE)
    return resp

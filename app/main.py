"""5ส Vision — ระบบตรวจประเมิน 5ส ด้วยภาพถ่ายและ AI"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import config, notify, routes_admin, routes_main, routes_tv, settings_store, worker
from .db import SessionLocal, User, init_db
from .security import NeedLogin, NeedPasswordChange
from .web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
init_db()


@asynccontextmanager
async def lifespan(_app):
    if not config.DISABLE_WORKER:
        worker.start()
        notify.start()
    yield
    worker.stop()
    notify.stop()


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(routes_main.router)
app.include_router(routes_tv.router)
app.include_router(routes_admin.router)


@app.middleware("http")
async def guard(request: Request, call_next):
    # กันคำสั่งที่ส่งมาจากเว็บอื่น (CSRF): คำขอที่แก้ข้อมูลต้องมาจากโดเมนเดียวกับระบบ
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != "null":
            host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
            if urlparse(origin).netloc != host:
                return PlainTextResponse("คำขอนี้มาจากเว็บอื่น ระบบไม่รับ", status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/static") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


# เพิ่มทีหลังสุด = อยู่ชั้นนอกสุด จึงมี session ให้ทุกส่วนใช้
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY or settings_store.get_or_create_secret(),
                   session_cookie="fives_session", max_age=7 * 24 * 3600, same_site="lax",
                   https_only=config.COOKIE_SECURE)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/admin/ai/"))


@app.exception_handler(NeedLogin)
async def need_login(request: Request, _exc):
    if _wants_json(request):
        return JSONResponse({"detail": "หมดเวลาการใช้งาน เข้าสู่ระบบใหม่"}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse("/login" + (f"?next={quote(target)}" if target != "/" else ""), 303)


@app.exception_handler(NeedPasswordChange)
async def need_password(request: Request, _exc):
    if _wants_json(request):
        return JSONResponse({"detail": "ตั้งรหัสผ่านใหม่ก่อนใช้งาน"}, status_code=403)
    return RedirectResponse("/account/password", 303)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    user = None
    uid = request.session.get("uid") if "session" in request.scope else None
    if uid:
        with SessionLocal() as db:
            user = db.get(User, uid)
    title = {403: "ไม่มีสิทธิ์ใช้หน้านี้", 404: "ไม่พบหน้านี้"}.get(exc.status_code, "ทำรายการไม่สำเร็จ")
    detail = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "ลิงก์อาจถูกลบหรือพิมพ์ผิด"
    return render(request, "error.html", user, None, status_code=exc.status_code, title=title, detail=detail)

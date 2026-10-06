"""หน้าสำหรับผู้ใช้ทุกคน: เข้าสู่ระบบ ถ่ายและส่งภาพ ดูผล อันดับ รายงาน ส่งออก และจุดรับภาพจากกล้อง"""
import time

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import (actions as act_mod, ai, backup, cameras, config, notify, photos as intake, rounds_auto, rules, schedule,
               scheduler, scoring, security,
               settings_store, storage, worker)
from .db import (Action, AuditArea, Camera, Department, DeptSummary, Photo, PhotoImage, PhotoThumb, Round, User, get_db,
                 log, now)
from .security import can, current_user, dept_ids, need
from .web import flash, render

router = APIRouter()
PAGE = 24


# --------------------------------------------------------------------------- ตัวช่วย
def active_round(db):
    rounds_auto.ensure(db)          # เปิดหรือปิดรอบตามวันที่ก่อนตอบ (ทำงานจริงเฉพาะเมื่อขึ้นวันใหม่หรือข้อมูลเปลี่ยน)
    return (db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).first()
            or db.query(Round).filter(Round.status != "planned").order_by(Round.id.desc()).first())


def open_round(db):
    rounds_auto.ensure(db)
    return db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).first()


def can_rank(user: User, rnd: Round, s: dict) -> bool:
    return can(user, "rank_live") or s["ranking_visibility"] == "always" or rnd.status == "closed"


def can_view_dept(user: User, dept_id: int, s: dict) -> bool:
    return can(user, "view_all") or dept_id in dept_ids(user) or bool(s["member_see_all"])


def upload_depts(user: User, db) -> list:
    q = db.query(Department).filter(Department.active.is_(True)).order_by(Department.name)
    if not can(user, "upload_all"):
        q = q.filter(Department.id.in_(dept_ids(user) or {-1}))
    return q.all()


def get_photo(db, pid: int, user: User) -> Photo:
    p = db.get(Photo, pid)
    if p is None:
        raise HTTPException(404, "ไม่พบภาพนี้")
    if not can_view_dept(user, p.department_id, settings_store.load()):
        raise HTTPException(403, "ภาพนี้เป็นของแผนกอื่น")
    return p


def can_delete(user: User, p: Photo, rnd: Round) -> bool:
    return can(user, "delete_any") or (p.uploader_id == user.id and rnd.status == "open")


async def form_data(request: Request):
    return await request.form()


def download(name: str, fallback: str) -> dict:
    from urllib.parse import quote
    return {"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"}


# --------------------------------------------------------------------------- บัญชี
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.session.get("uid"):
        return RedirectResponse("/", 303)
    return render(request, "login.html", next=next)


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/"),
          db=Depends(get_db)):
    username = username.strip().lower()
    key = f"{request.client.host if request.client else '-'}:{username}"
    if security.login_locked(key):
        flash(request, "ใส่รหัสผ่านผิดหลายครั้ง รอ 10 นาทีแล้วลองใหม่", "err")
        return RedirectResponse("/login", 303)
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.active or not security.verify_password(password, user.password_hash):
        security.login_failed(key)
        flash(request, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "err")
        return RedirectResponse("/login", 303)
    security.login_ok(key)
    user.last_login = now()
    db.commit()
    # จำที่อยู่เว็บครั้งแรกที่ผู้ดูแลเข้าใช้ เพื่อใช้ทำลิงก์ในข้อความแจ้งเตือน (แก้ได้ในหน้าตั้งค่า)
    if user.role == "admin" and not settings_store.load().get("public_url"):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        if host and not host.startswith(("testserver", "127.0.0.1", "localhost")):
            settings_store.save(db, {"public_url": f"{proto.split(',')[0]}://{host.split(',')[0]}"})
    request.session.clear()
    request.session.update(uid=user.id, stamp=security.session_stamp(user))
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    return RedirectResponse(next, 303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, user=Depends(current_user), db=Depends(get_db)):
    return render(request, "password.html", user, db)


@router.post("/account/password")
def password_save(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                  user=Depends(current_user), db=Depends(get_db)):
    if not security.verify_password(current, user.password_hash):
        problem = "รหัสผ่านปัจจุบันไม่ถูกต้อง"
    elif new != confirm:
        problem = "รหัสผ่านใหม่สองช่องไม่ตรงกัน"
    elif new == current:
        problem = "รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสเดิม"
    else:
        problem = security.password_problem(new)
    if problem:
        flash(request, problem, "err")
        return RedirectResponse("/account/password", 303)
    user.password_hash = security.hash_password(new)
    user.must_change = False
    log(db, user, "change_password")
    db.commit()
    request.session.update(uid=user.id, stamp=security.session_stamp(user))
    flash(request, "เปลี่ยนรหัสผ่านแล้ว")
    return RedirectResponse("/", 303)


# --------------------------------------------------------------------------- หน้าแรก
@router.get("/", response_class=HTMLResponse)
def home(request: Request, user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    rnd = active_round(db)
    ranking = mine = None
    recent = []
    if rnd:
        ranking = scoring.round_ranking(db, rnd)
        if user.department_id:
            mine = scoring.find_row(ranking, user.department_id)
        q = db.query(Photo).filter(Photo.round_id == rnd.id)
        if not can_view_dept(user, -1, s):
            q = q.filter(Photo.department_id.in_(dept_ids(user) or {-1}))
        recent = q.order_by(Photo.id.desc()).limit(6).all()
    return render(request, "home.html", user, db, rnd=rnd, ranking=ranking, mine=mine, recent=recent,
                  show_rank=bool(rnd and can_rank(user, rnd, s)), ai_ready=ai.is_configured(s),
                  can_upload=bool(rnd and rnd.status == "open" and upload_depts(user, db)))


# --------------------------------------------------------------------------- ถ่ายและส่งภาพ
@router.get("/capture", response_class=HTMLResponse)
def capture(request: Request, after: int = 0, user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    rounds = db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).all()
    depts = upload_depts(user, db)
    q = db.query(Photo.area_name).filter(Photo.area_name != "")
    if depts:      # แนะนำเฉพาะชื่อจุดตรวจของแผนกที่ผู้ใช้ส่งภาพได้
        q = q.filter(Photo.department_id.in_([d.id for d in depts]))
    areas = [r[0] for r in q.distinct().limit(150)]
    before = None
    if after:
        before = db.get(Photo, after)
        if before is None or before.department_id not in [d.id for d in depts]:
            before = None
    cams = []
    if can(user, "cameras_use") and depts:
        cams = (db.query(Camera).filter(Camera.active.is_(True), Camera.department_id.in_([d.id for d in depts]))
                .order_by(Camera.name).all())
    defined = {}
    if depts:
        for a in (db.query(AuditArea).filter(AuditArea.active.is_(True), AuditArea.department_id.in_([d.id for d in depts]))
                  .order_by(AuditArea.sort_order, AuditArea.id).all()):
            defined.setdefault(str(a.department_id), []).append(dict(id=a.id, name=a.name, type=a.area_type, required=a.required))
    return render(request, "capture.html", user, db, rounds=rounds, depts=depts, areas=sorted(areas), before=before,
                  defined=defined,
                  usage=storage.usage(db, s), ai_ready=ai.is_configured(s), cams=cams,
                  agent_online=cameras.agent_online())


@router.post("/api/photos")
def upload_photo(file: UploadFile = File(...), round_id: int = Form(...), department_id: int = Form(...),
                 area_name: str = Form(""), area_type: str = Form(""), note: str = Form(""),
                 source: str = Form("mobile"), after_of: int = Form(0), area_id: int = Form(0),
                 user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    if department_id not in [d.id for d in upload_depts(user, db)]:
        raise HTTPException(403, "คุณไม่มีสิทธิ์ส่งภาพให้แผนกนี้")
    raw = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    p = intake.create_photo(db, s, rnd=db.get(Round, round_id), department_id=department_id, raw=raw,
                            area_name=area_name, area_type=area_type, note=note, uploader_id=user.id,
                            uploader_name=user.full_name or user.username,
                            source=source if source in ("mobile", "gallery", "webcam") else "mobile",
                            after_of=after_of or None, area_id=area_id or None, enforce_area=True)
    return {"id": p.id, "status": p.status, "ai_ready": ai.is_configured(s)}


@router.get("/api/photos/status")
def photo_status(ids: str = "", user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    want = [int(x) for x in ids.split(",") if x.strip().isdigit()][:60]
    out = []
    if want:
        for p in db.query(Photo).filter(Photo.id.in_(want)).all():
            if can_view_dept(user, p.department_id, s):
                out.append(dict(id=p.id, status=p.status, percent=p.percent, score=p.score, max=p.max_score,
                                error=p.error, issue=(p.analysis or {}).get("image_issue", "")))
    return {"photos": out, "paused": worker.state["paused_reason"]}


# --------------------------------------------------------------------------- กล้อง IP
def _camera_for(db, cid: int, user: User) -> Camera:
    cam = db.get(Camera, cid)
    if cam is None or not cam.active:
        raise HTTPException(404, "ไม่พบกล้องนี้ หรือกล้องถูกปิดใช้งาน")
    if cam.department_id not in [d.id for d in upload_depts(user, db)]:
        raise HTTPException(403, "กล้องนี้เป็นของแผนกที่คุณไม่มีสิทธิ์ส่งภาพ")
    return cam


@router.post("/api/cameras/{cid}/capture")
def camera_capture(cid: int, round_id: int = Form(...), user=Depends(need("cameras_use")), db=Depends(get_db)):
    cam = _camera_for(db, cid, user)
    rnd = db.get(Round, round_id)
    if rnd is None or rnd.status != "open":
        raise HTTPException(400, "รอบการตรวจนี้ปิดรับภาพแล้ว")
    if cam.mode == "agent":
        if not cameras.agent_online():
            raise HTTPException(503, "โปรแกรมกล้องบนเครื่องในโรงงานยังไม่ได้เชื่อมต่อ เปิด camera_agent ในโหมดรับคำสั่งก่อน")
        cameras.agent["requests"][cam.id] = dict(by=user.full_name or user.username, at=time.time())
        return {"queued": True, "message": "ส่งคำสั่งถ่ายไปยังเครื่องในโรงงานแล้ว ภาพจะเข้าระบบภายในไม่กี่วินาที"}
    try:
        p = scheduler.capture_direct(db, cam, rnd, f"กล้อง {cam.name}", user.id, user.full_name or user.username)
    except cameras.CameraError as e:
        raise HTTPException(502, str(e))
    return {"id": p.id, "status": p.status, "area": p.area_name}


def _agent_cache(db) -> dict:
    """รายการกล้องสำหรับ agent เก็บในหน่วยความจำ: agent ถามบ่อย จึงไม่ให้ทุกครั้งไปปลุกฐานข้อมูล"""
    if cameras.agent["cache"] is None:
        if not settings_store.load().get("_sched_secret"):
            import secrets as _secrets
            settings_store.save(db, {"_sched_secret": _secrets.token_hex(16)})
        eff = scheduler.effective_map(db)
        cams = [c for c, _, _ in eff.values() if c.mode == "agent"]
        cameras.agent["cache"] = dict(
            cameras=[dict(id=c.id, name=c.name, source=c.source, url=c.url, username=c.username,
                          password=c.password, auth=c.auth, department=c.department.name, area=c.area_name or c.name)
                     for c in cams],
            schedules={c.id: eff[c.id][1] for c in cams},
            open_round=open_round(db) is not None)
    return cameras.agent["cache"]


def _agent_plan(data: dict) -> dict:
    """เวลาถ่ายของวันนี้ของแต่ละกล้อง คำนวณจากตารางที่เก็บในหน่วยความจำ ไม่แตะฐานข้อมูล"""
    when = schedule.thai_now()
    return dict(date=when.date().isoformat(), now=when.strftime("%H:%M"),
                grace=int(settings_store.load().get("cam_grace_min", 20) or 20),
                plan={str(cid): schedule.times_for(cid, sched, when.date()) for cid, sched in data["schedules"].items()})


def _agent_auth(token: str):
    if not cameras.token_ok(token or "", settings_store.load().get("agent_token", "")):
        raise HTTPException(401, "รหัสของโปรแกรมกล้องไม่ถูกต้อง")
    cameras.agent["seen"] = time.time()


@router.get("/api/agent/poll")
def agent_poll(x_agent_token: str = Header(""), db=Depends(get_db)):
    _agent_auth(x_agent_token)
    data = _agent_cache(db)
    requests = list(cameras.agent["requests"].keys())
    return dict(cameras=data["cameras"], open_round=data["open_round"], requests=requests, **_agent_plan(data))


@router.post("/api/agent/upload")
def agent_upload(file: UploadFile = File(None), camera_id: int = Form(...), error: str = Form(""),
                 scheduled: str = Form(""), x_agent_token: str = Header(""), db=Depends(get_db)):
    _agent_auth(x_agent_token)
    cam = db.get(Camera, camera_id)
    if cam is None or not cam.active or cam.mode != "agent":
        raise HTTPException(404, "ไม่พบกล้องนี้")
    req = cameras.agent["requests"].pop(cam.id, None)
    if error or file is None:
        cam.last_error = (error or "agent ไม่ได้ส่งภาพ")[:500]
        db.commit()
        if not req:
            scheduler.camera_failed(db, cam, cam.last_error)
        return {"ok": False}
    rnd = open_round(db)
    if rnd is None:
        raise HTTPException(409, "ไม่มีรอบการตรวจที่เปิดรับภาพ")
    raw = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    p = intake.create_photo(db, settings_store.load(), rnd=rnd, department_id=cam.department_id, raw=raw,
                            area_name=cam.area_name or cam.name, area_type=cam.area_type,
                            note=f"กล้อง {cam.name}" + (f" สั่งถ่ายโดย {req['by']}" if req else f" ถ่ายตามตาราง {scheduled[:5]}" if scheduled else " ถ่ายตามเวลาที่ตั้งไว้"),
                            uploader_name=f"กล้อง {cam.name}", source="agent", camera_id=cam.id)
    cam.last_capture_at, cam.last_error = now(), ""
    db.commit()
    return {"ok": True, "id": p.id}


# --------------------------------------------------------------------------- ภาพ
@router.get("/photos", response_class=HTMLResponse)
def photos(request: Request, round: int = 0, dept: int = 0, status: str = "", page: int = 1,
           user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    rounds = db.query(Round).filter(Round.status != "planned").order_by(Round.id.desc()).all()
    rnd = db.get(Round, round) if round else active_round(db)
    depts = db.query(Department).order_by(Department.name).all()
    mine = dept_ids(user)
    limited = not can_view_dept(user, -1, s)
    if limited:
        depts = [d for d in depts if d.id in mine]
        if dept not in mine:
            dept = 0
    items, total = [], 0
    if rnd:
        q = db.query(Photo).filter(Photo.round_id == rnd.id)
        if limited:
            q = q.filter(Photo.department_id.in_(mine or {-1}))
        if dept:
            q = q.filter(Photo.department_id == dept)
        if status == "waiting":
            q = q.filter(Photo.status.in_(["pending", "processing"]))
        elif status in ("done", "rejected", "error"):
            q = q.filter(Photo.status == status)
        elif status == "review":
            q = q.filter(Photo.review_flag.is_(True))
        elif status == "override":
            q = q.filter(Photo.overridden.is_(True))
        elif status == "unverified":
            q = q.filter(Photo.status == "done", Photo.verified_at.is_(None))
        total = q.count()
        page = max(1, page)
        items = q.order_by(Photo.id.desc()).offset((page - 1) * PAGE).limit(PAGE).all()
    return render(request, "photos.html", user, db, rounds=rounds, rnd=rnd, depts=depts, dept=dept,
                  status=status, items=items, total=total, page=page, pages=max(1, -(-total // PAGE)))


@router.get("/photos/{pid}", response_class=HTMLResponse)
def photo_detail(pid: int, request: Request, user=Depends(current_user), db=Depends(get_db)):
    p = get_photo(db, pid, user)
    rnd = db.get(Round, p.round_id)
    before = db.get(Photo, p.after_of) if p.after_of else None
    after = db.query(Photo).filter(Photo.after_of == p.id).order_by(Photo.id.desc()).first()
    can_fix = rnd.status == "open" and p.department_id in [d.id for d in upload_depts(user, db)]
    checklist = rules.applicable(rnd.checklist or [], rnd.rubric or [], p) if (rnd.mode or "level") == "checklist" else []
    return render(request, "photo.html", user, db, p=p, rnd=rnd, deletable=can_delete(user, p, rnd),
                  before=before, after=after, can_fix=can_fix, can_verify=may_verify(user, p),
                  checklist=checklist, zones=rules.zones_of(checklist),
                  actions=db.query(Action).filter(Action.photo_id == p.id).order_by(Action.id).all(),
                  can_act=can_manage_actions(user, p.department_id), today=act_mod.today())


def can_manage_actions(user: User, dept_id: int) -> bool:
    """งานแก้ไขจัดการได้โดยคนของแผนกนั้น และโดยผู้ที่มีสิทธิ์ยืนยันผล"""
    return can(user, "score") or dept_id in dept_ids(user)


# --------------------------------------------------------------------------- งานแก้ไข
@router.get("/actions", response_class=HTMLResponse)
def actions_page(request: Request, dept: int = 0, status: str = "open", user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    depts = db.query(Department).order_by(Department.name).all()
    limited = not can_view_dept(user, -1, s)
    if limited:
        depts = [d for d in depts if d.id in dept_ids(user)]
        if dept not in dept_ids(user):
            dept = 0
    q = db.query(Action)
    if limited:
        q = q.filter(Action.department_id.in_(dept_ids(user) or {-1}))
    if dept:
        q = q.filter(Action.department_id == dept)
    day = act_mod.today()
    counts = dict(open=q.filter(Action.status == "open").count(),
                  overdue=q.filter(Action.status == "open", Action.due_date.isnot(None), Action.due_date < day).count(),
                  done=q.filter(Action.status == "done").count())
    if status == "overdue":
        q = q.filter(Action.status == "open", Action.due_date.isnot(None), Action.due_date < day)
    elif status in ("open", "done"):
        q = q.filter(Action.status == status)
    items = q.order_by(Action.status.desc(), Action.due_date.asc().nulls_last(), Action.id.desc()).limit(200).all()
    return render(request, "actions.html", user, db, items=items, depts=depts, dept=dept, status=status, counts=counts,
                  today=day, mine=dept_ids(user), can_score=can(user, "score"))


@router.post("/actions/save")
def action_save(request: Request, form=Depends(form_data), user=Depends(current_user), db=Depends(get_db)):
    from datetime import date as _date
    aid = int(form.get("id") or 0)
    act = db.get(Action, aid) if aid else None
    if act is None:
        p = db.get(Photo, int(form.get("photo_id") or 0))
        if p is None:
            raise HTTPException(400, "งานแก้ไขต้องอ้างถึงภาพที่พบข้อบกพร่อง")
        act = Action(round_id=p.round_id, department_id=p.department_id, area_name=p.area_name, photo_id=p.id,
                     created_by=user.full_name or user.username, severity="minor", title="")
        db.add(act)
    if not can_manage_actions(user, act.department_id):
        raise HTTPException(403, "งานแก้ไขนี้เป็นของแผนกอื่น")
    title = (form.get("title") or act.title or "").strip()[:400]
    if not title:
        flash(request, "ใส่รายละเอียดของงานแก้ไข", "err")
        return RedirectResponse(request.headers.get("referer") or "/actions", 303)
    act.title = title
    act.pic = (form.get("pic") or "").strip()[:120]
    try:
        act.due_date = _date.fromisoformat(form.get("due_date")) if form.get("due_date") else act.due_date
    except ValueError:
        pass
    if form.get("severity") in ("minor", "major"):
        act.severity = form.get("severity")
    log(db, user, "save_action", f"{act.area_name}: {title[:120]} ผู้รับผิดชอบ {act.pic or '-'} กำหนด {act.due_date or '-'}")
    db.commit()
    flash(request, "บันทึกงานแก้ไขแล้ว")
    return RedirectResponse(request.headers.get("referer") or "/actions", 303)


@router.post("/actions/{aid}/{what}")
def action_change(aid: int, what: str, request: Request, form=Depends(form_data), user=Depends(current_user),
                  db=Depends(get_db)):
    act = db.get(Action, aid)
    if act is None:
        raise HTTPException(404, "ไม่พบงานแก้ไขนี้")
    if not can_manage_actions(user, act.department_id):
        raise HTTPException(403, "งานแก้ไขนี้เป็นของแผนกอื่น")
    if what == "close":
        note = (form.get("note") or "").strip()[:1000]
        if len(note) < 3:
            flash(request, "ใส่สิ่งที่ได้ทำเพื่อปิดงาน", "err")
        else:
            act.status, act.closed_at, act.closed_by, act.close_note = "done", now(), user.full_name or user.username, note
            log(db, user, "close_action", f"งาน {act.id} {act.area_name}: {note[:200]}")
            flash(request, "ปิดงานแก้ไขแล้ว")
    elif what == "reopen":
        if not can(user, "score"):
            raise HTTPException(403, "การเปิดงานที่ปิดแล้วต้องมีสิทธิ์ยืนยันผล")
        act.status, act.closed_at, act.closed_by = "open", None, ""
        log(db, user, "reopen_action", f"งาน {act.id} {act.area_name}")
        flash(request, "เปิดงานแก้ไขอีกครั้ง")
    else:
        raise HTTPException(404, "ไม่รู้จักคำสั่งนี้")
    db.commit()
    return RedirectResponse(request.headers.get("referer") or "/actions", 303)


@router.get("/actions.csv")
def actions_export(dept: int = 0, user=Depends(need("export")), db=Depends(get_db)):
    return Response(backup.actions_csv(db, dept), media_type="text/csv; charset=utf-8",
                    headers=download("5S_งานแก้ไข.csv", "5S_actions.csv"))


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, round: int = 0, user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    rounds = db.query(Round).filter(Round.status != "planned").order_by(Round.id.desc()).all()
    rnd = db.get(Round, round) if round else active_round(db)
    data, allowed = None, False
    if rnd:
        allowed = can_rank(user, rnd, s) and can_view_dept(user, -1, s)
        if allowed:
            data = scoring.dashboard(db, rnd)
    return render(request, "dashboard.html", user, db, rounds=rounds, rnd=rnd, d=data, allowed=allowed)


def may_verify(user: User, p: Photo) -> bool:
    """ผู้ยืนยันต้องไม่ใช่คนที่ส่งภาพนั้นเอง (ยกเว้นผู้ดูแลระบบ) เพื่อให้มีคนที่สองดูหลักฐานเสมอ"""
    return can(user, "score") and (user.role == "admin" or p.uploader_id != user.id)


@router.get("/verify", response_class=HTMLResponse)
def verify_queue(request: Request, round: int = 0, dept: int = 0, user=Depends(need("score")), db=Depends(get_db)):
    """คิวยืนยันผล: ภาพที่ AI ให้คะแนนแล้วแต่ยังไม่มีคนดู เรียงภาพที่ AI ไม่นิ่งและคะแนนต่ำขึ้นก่อน"""
    s = settings_store.load()
    rounds = db.query(Round).filter(Round.status != "planned").order_by(Round.id.desc()).all()
    rnd = db.get(Round, round) if round else active_round(db)
    depts = db.query(Department).order_by(Department.name).all()
    if not can_view_dept(user, -1, s):
        depts = [d for d in depts if d.id in dept_ids(user)]
    items, total = [], 0
    if rnd:
        q = db.query(Photo).filter(Photo.round_id == rnd.id, Photo.status == "done", Photo.verified_at.is_(None))
        if not can_view_dept(user, -1, s):
            q = q.filter(Photo.department_id.in_(dept_ids(user) or {-1}))
        if dept:
            q = q.filter(Photo.department_id == dept)
        total = q.count()
        items = q.order_by(Photo.review_flag.desc().nulls_last(), Photo.percent.asc(), Photo.id).limit(12).all()
    return render(request, "verify.html", user, db, rounds=rounds, rnd=rnd, depts=depts, dept=dept, items=items,
                  total=total, mine=[p.id for p in items if not may_verify(user, p)])


def _image_response(data: bytes) -> Response:
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photos/{pid}/image")
def photo_image(pid: int, user=Depends(current_user), db=Depends(get_db)):
    p = get_photo(db, pid, user)
    blob = db.get(PhotoImage, p.id) if p.has_image else None
    blob = blob or db.get(PhotoThumb, p.id)
    if blob is None:
        raise HTTPException(404, "ไม่พบไฟล์ภาพ")
    return _image_response(blob.data)


@router.get("/photos/{pid}/thumb")
def photo_thumb(pid: int, user=Depends(current_user), db=Depends(get_db)):
    p = get_photo(db, pid, user)
    blob = db.get(PhotoThumb, p.id)
    if blob is None:
        raise HTTPException(404, "ไม่พบไฟล์ภาพ")
    return _image_response(blob.data)


@router.post("/photos/{pid}/delete")
def photo_delete(pid: int, request: Request, user=Depends(current_user), db=Depends(get_db)):
    p = get_photo(db, pid, user)
    rnd = db.get(Round, p.round_id)
    if not can_delete(user, p, rnd):
        raise HTTPException(403, "ลบได้เฉพาะภาพที่คุณส่งเอง และรอบการตรวจยังเปิดอยู่")
    db.query(PhotoImage).filter(PhotoImage.photo_id == p.id).delete()
    db.query(PhotoThumb).filter(PhotoThumb.photo_id == p.id).delete()
    db.query(Photo).filter(Photo.after_of == p.id).update({"after_of": None})
    db.query(Action).filter(Action.photo_id == p.id).update({"photo_id": None})
    log(db, user, "delete_photo", f"ภาพ {p.id} {p.department.name} / {p.area_name}")
    db.delete(p)
    db.commit()
    flash(request, "ลบภาพแล้ว")
    return RedirectResponse(f"/photos?round={rnd.id}", 303)


# --------------------------------------------------------------------------- อันดับ รายงาน ส่งออก
@router.get("/ranking", response_class=HTMLResponse)
def ranking_page(request: Request, round: int = 0, user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    rounds = db.query(Round).filter(Round.status != "planned").order_by(Round.id.desc()).all()
    rnd = db.get(Round, round) if round else active_round(db)
    ranking, allowed = None, False
    if rnd:
        allowed = can_rank(user, rnd, s)
        if allowed:
            ranking = scoring.round_ranking(db, rnd)
    return render(request, "ranking.html", user, db, rounds=rounds, rnd=rnd, ranking=ranking, allowed=allowed)


@router.get("/rounds/{rid}/dept/{did}", response_class=HTMLResponse)
def dept_report(rid: int, did: int, request: Request, user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    rnd, dept = db.get(Round, rid), db.get(Department, did)
    if rnd is None or dept is None:
        raise HTTPException(404, "ไม่พบรอบการตรวจหรือแผนกนี้")
    own = did in dept_ids(user)
    if not own and not (can_view_dept(user, did, s) and can_rank(user, rnd, s)):
        raise HTTPException(403, "ยังดูรายงานของแผนกอื่นไม่ได้จนกว่าจะปิดรอบ")
    ranking = scoring.round_ranking(db, rnd)
    row = scoring.find_row(ranking, did)
    photos_ = (db.query(Photo).filter(Photo.round_id == rid, Photo.department_id == did)
               .order_by(Photo.percent.asc().nulls_last(), Photo.id).all())
    summary = (db.query(DeptSummary).filter(DeptSummary.round_id == rid, DeptSummary.department_id == did)
               .order_by(DeptSummary.id.desc()).first())
    old = scoring.superseded(photos_) if s.get("after_replaces", True) else set()
    actions, seen = [], set()        # คำแนะนำรวมจากภาพคะแนนต่ำก่อน ไม่ต้องเรียก AI เพิ่ม
    for p in photos_:
        if p.id in old:
            continue
        for a in (p.analysis or {}).get("top_actions", []):
            if a not in seen and len(actions) < 8:
                seen.add(a)
                actions.append(dict(text=a, photo=p))
    return render(request, "dept_report.html", user, db, rnd=rnd, dept=dept, row=row, ranking=ranking,
                  photos=photos_, summary=summary, actions=actions, old=old, show_rank=can_rank(user, rnd, s))


@router.get("/rounds/{rid}/report", response_class=HTMLResponse)
def full_report(rid: int, user=Depends(current_user), db=Depends(get_db)):
    rnd = db.get(Round, rid)
    if rnd is None:
        raise HTTPException(404, "ไม่พบรอบการตรวจนี้")
    s = settings_store.load()
    if not (can(user, "export") or (s["member_see_all"] and can_rank(user, rnd, s))):
        raise HTTPException(403, "รายงานรวมเปิดให้ดูเมื่อปิดรอบการตรวจแล้ว หรือเมื่อได้รับสิทธิ์ส่งออกข้อมูล")
    return HTMLResponse(backup.render_report(db, rnd))


EXPORTS = {
    "scores.xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "5S_{name}.xlsx"),
    "ranking.csv": ("text/csv; charset=utf-8", "5S_{name}_อันดับ.csv"),
    "photos.csv": ("text/csv; charset=utf-8", "5S_{name}_รายภาพ.csv"),
    "data.json": ("application/json", "5S_{name}.json"),
    "checks.csv": ("text/csv; charset=utf-8", "5S_{name}_รายข้อ.csv"),
}


@router.get("/rounds/{rid}/export/{kind}")
def export(rid: int, kind: str, dept: int = 0, user=Depends(need("export")), db=Depends(get_db)):
    rnd = db.get(Round, rid)
    if rnd is None or kind not in EXPORTS:
        raise HTTPException(404, "ไม่พบรอบการตรวจหรือรูปแบบไฟล์นี้")
    if kind == "scores.xlsx":
        data = backup.export_excel(db, rnd)
    elif kind == "ranking.csv":
        data = backup.ranking_csv(db, rnd)
    elif kind == "photos.csv":
        data = backup.photos_csv(db, rnd, dept)
    elif kind == "checks.csv":
        data = backup.checks_csv(db, rnd, dept)
    else:
        data = backup.round_json(db, rnd)
    log(db, user, "export", f"{rnd.name}: {kind}")
    db.commit()
    mime, pattern = EXPORTS[kind]
    return Response(data, media_type=mime, headers=download(pattern.format(name=rnd.name), f"5S_round_{rid}_{kind}"))


@router.get("/rounds/{rid}/dataset.zip")
def dataset_export(rid: int, user=Depends(need("export")), db=Depends(get_db)):
    """ภาพที่ยืนยันผลแล้วพร้อมป้ายกำกับ สำหรับฝึกโมเดลตรวจจับในภายหลัง"""
    import os
    import tempfile
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    rnd = db.get(Round, rid)
    if rnd is None:
        raise HTTPException(404, "ไม่พบรอบการตรวจนี้")
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        n = backup.build_dataset_zip(db, rnd, path)
    except Exception:
        os.unlink(path)
        raise
    log(db, user, "export", f"{rnd.name}: dataset {n} ภาพ")
    db.commit()
    return FileResponse(path, media_type="application/zip", background=BackgroundTask(os.unlink, path),
                        headers=download(f"5S_{rnd.name}_dataset.zip", f"5S_round_{rid}_dataset.zip"))


def trend_data(db, limit: int = 8) -> dict:
    rounds = list(reversed(db.query(Round).filter(Round.status != "planned").order_by(Round.id.desc()).limit(limit).all()))
    table, depts = {}, {}
    for r in rounds:
        rk = scoring.round_ranking(db, r, with_prev=False)
        for row in rk["ranked"] + rk["unranked"]:
            depts[row["dept"].id] = row["dept"]
            table.setdefault(row["dept"].id, {})[r.id] = dict(avg=row["avg"], rank=row["rank"], scored=row["scored"])
    order = sorted(depts.values(), key=lambda d: d.name)
    return dict(rounds=rounds, depts=order, table=table)


@router.get("/trend", response_class=HTMLResponse)
def trend(request: Request, user=Depends(current_user), db=Depends(get_db)):
    s = settings_store.load()
    data = trend_data(db)
    data["rounds"] = [r for r in data["rounds"] if can_rank(user, r, s)]
    if not can_view_dept(user, -1, s):
        data["depts"] = [d for d in data["depts"] if d.id in dept_ids(user)]
    return render(request, "trend.html", user, db, **data)


@router.get("/trend.csv")
def trend_csv(user=Depends(need("export")), db=Depends(get_db)):
    data = trend_data(db, 24)
    rows = [["แผนก"] + [r.name for r in data["rounds"]]]
    for d in data["depts"]:
        rows.append([d.name] + [(data["table"].get(d.id, {}).get(r.id) or {}).get("avg", "") or "" for r in data["rounds"]])
    return Response(backup._csv(rows), media_type="text/csv; charset=utf-8",
                    headers=download("5S_แนวโน้มคะแนน.csv", "5S_trend.csv"))


@router.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "version": config.APP_VERSION})

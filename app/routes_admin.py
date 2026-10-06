"""หน้าจัดการระบบ: แต่ละส่วนเปิดให้ตามสิทธิ์ของบัญชี ส่วนผู้ใช้ การตั้งค่า การแจ้งเตือน และกล้อง เป็นของผู้ดูแลระบบเท่านั้น"""
import io
import json
import os
import secrets
import tempfile
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from PIL import Image, ImageDraw
from sqlalchemy import case, func
from starlette.background import BackgroundTask

from . import (actions as act_mod, ai, backup, cameras, config, notify, rules, scoring, security, settings_store,
               storage, worker)
from .db import (DEFAULT_CRITERIA, Action, AuditArea, AuditLog, Camera, Checkpoint, checklist_snapshot, seed_checkpoints, Channel, Criterion, Department, DeptSummary, NotifyEvent,
                 NotifyLog, Photo, PhotoImage, PhotoThumb, Round, User, get_db, log, now, rubric_snapshot)
from .photos import area_types
from .routes_main import download, form_data, may_verify
from .security import admin_user, can, current_user, manager_user, need
from .web import flash, render, to_bkk

router = APIRouter(prefix="/admin")


def back(url: str):
    return RedirectResponse(url, 303)


def _int(v, default=0, lo=None, hi=None):
    try:
        n = int(float(str(v).strip()))
    except Exception:
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def bump_rule_rev(db) -> int:
    """ทุกการแก้เกณฑ์หรือรายการตรวจ = เกณฑ์ฉบับใหม่ รอบการตรวจบันทึกเลขฉบับที่ใช้ไว้"""
    rev = int(settings_store.load().get("_rule_rev", 0) or 0) + 1
    settings_store.save(db, {"_rule_rev": rev})
    return rev


RULE_ACTIONS = ("save_criterion", "delete_criterion", "reset_criteria", "save_checkpoint", "delete_checkpoint",
                "reset_checkpoints", "save_zone", "delete_zone")


def _date(v):
    try:
        return date.fromisoformat(str(v).strip()) if v else None
    except Exception:
        return None


# --------------------------------------------------------------------------- ภาพรวม
@router.get("", response_class=HTMLResponse)
def overview(request: Request, user=Depends(manager_user), db=Depends(get_db)):
    s = settings_store.load()
    counts = dict(db.query(Photo.status, func.count(Photo.id)).group_by(Photo.status).all())
    return render(request, "admin/index.html", user, db, usage=storage.usage(db, s), counts=counts,
                  ai_ready=ai.is_configured(s), profiles=ai.profiles(s), used_today=worker.usage_today(db),
                  wstate=worker.state, n_depts=db.query(Department).filter(Department.active.is_(True)).count(),
                  n_users=db.query(User).filter(User.active.is_(True)).count(),
                  open_rounds=db.query(Round).filter(Round.status == "open").count(),
                  n_channels=db.query(Channel).filter(Channel.active.is_(True)).count(),
                  n_cameras=db.query(Camera).filter(Camera.active.is_(True)).count(),
                  n_review=db.query(Photo).filter(Photo.review_flag.is_(True)).count(),
                  n_unverified=db.query(Photo).filter(Photo.status == "done", Photo.verified_at.is_(None)).count(),
                  n_areas=db.query(AuditArea).filter(AuditArea.active.is_(True)).count(),
                  system_channel=any("system" in (c.events or []) for c in
                                     db.query(Channel).filter(Channel.active.is_(True)).all()),
                  alerts=db.query(AuditLog).filter(AuditLog.action == "alert",
                                                   AuditLog.at > now() - timedelta(days=14))
                  .order_by(AuditLog.id.desc()).limit(8).all())


# --------------------------------------------------------------------------- รอบการตรวจ
@router.get("/rounds", response_class=HTMLResponse)
def rounds_page(request: Request, user=Depends(need("rounds")), db=Depends(get_db)):
    rounds = db.query(Round).order_by(Round.id.desc()).all()
    stats = {}
    for rid, n, img, size in (db.query(Photo.round_id, func.count(Photo.id),
                                       func.sum(case((Photo.has_image.is_(True), 1), else_=0)),
                                       func.sum(case((Photo.has_image.is_(True), Photo.image_bytes), else_=0)))
                              .group_by(Photo.round_id).all()):
        stats[rid] = dict(photos=n, images=int(img or 0), bytes=int(size or 0))
    return render(request, "admin/rounds.html", user, db, rounds=rounds, stats=stats,
                  n_criteria=len(rubric_snapshot(db)), today=to_bkk(now()).date().isoformat())


@router.post("/rounds/save")
def round_save(request: Request, form=Depends(form_data), user=Depends(need("rounds")), db=Depends(get_db)):
    name = (form.get("name") or "").strip()[:160]
    if not name:
        flash(request, "ใส่ชื่อรอบการตรวจ", "err")
        return back("/admin/rounds")
    rid = _int(form.get("id"))
    rnd = db.get(Round, rid) if rid else None
    created = rnd is None
    if created:
        rubric = rubric_snapshot(db)
        if not rubric:
            flash(request, "ยังไม่มีเกณฑ์ที่เปิดใช้ ตั้งเกณฑ์ก่อนสร้างรอบ", "err")
            return back("/admin/criteria" if can(user, "criteria") else "/admin/rounds")
        s = settings_store.load()
        mode = form.get("mode") if form.get("mode") in ("level", "checklist") else s.get("scoring_mode", "level")
        checklist = checklist_snapshot(db) if mode == "checklist" else None
        if mode == "checklist" and not checklist:
            flash(request, "โหมดรายการตรวจต้องมีรายการตรวจที่เปิดใช้อย่างน้อย 1 ข้อ", "err")
            return back("/admin/checkpoints" if can(user, "criteria") else "/admin/rounds")
        rnd = Round(rubric=rubric, status="open", mode=mode, checklist=checklist,
                    rule_rev=int(s.get("_rule_rev", 0) or 0))
        db.add(rnd)
        log(db, user, "create_round", f"{name} ({'รายการตรวจ' if mode == 'checklist' else 'ระดับ 0-4'}, เกณฑ์ฉบับที่ {rnd.rule_rev})")
    rnd.name = name
    rnd.note = (form.get("note") or "").strip()[:1000]
    rnd.start_date, rnd.end_date = _date(form.get("start_date")), _date(form.get("end_date"))
    rnd.min_photos = _int(form.get("min_photos"), 3, 1, 200)
    db.flush()
    if created:
        notify.emit(db, "round", round_id=rnd.id, payload={"action": "open"})
    db.commit()
    cameras.invalidate()
    flash(request, f"บันทึกรอบ {name} แล้ว")
    return back("/admin/rounds")


ROUND_ACTIONS = {"close": "rounds", "reopen": "rounds", "reanalyze": "rounds", "retry-errors": "rounds",
                 "sync-rubric": "rounds", "purge-images": "storage", "delete": "storage"}


@router.post("/rounds/{rid}/{action}")
def round_action(rid: int, action: str, request: Request, user=Depends(current_user), db=Depends(get_db)):
    if action not in ROUND_ACTIONS:
        raise HTTPException(404, "ไม่รู้จักคำสั่งนี้")
    perm = ROUND_ACTIONS[action]
    if not can(user, perm):
        raise HTTPException(403, f"บัญชีของคุณยังไม่ได้รับสิทธิ์ \"{security.PERMS[perm][0]}\"")
    rnd = db.get(Round, rid)
    if rnd is None:
        raise HTTPException(404, "ไม่พบรอบการตรวจนี้")
    q = db.query(Photo).filter(Photo.round_id == rid)
    if action == "close":
        rnd.status, rnd.closed_at = "closed", now()
        top = scoring.round_ranking(db, rnd, with_prev=False)["ranked"][:3]
        text = "\n".join(f"อันดับ {r['rank']}: {r['dept'].name} {notify._num(r['avg'])}%" for r in top)
        notify.emit(db, "round", round_id=rid, payload={"action": "close", "top": text})
        flash(request, f"ปิดรอบ {rnd.name} แล้ว ไม่รับภาพเพิ่ม")
    elif action == "reopen":
        rnd.status, rnd.closed_at = "open", None
        notify.emit(db, "round", round_id=rid, payload={"action": "open"})
        flash(request, f"เปิดรอบ {rnd.name} อีกครั้ง")
    elif action in ("reanalyze", "retry-errors", "sync-rubric"):
        if action == "sync-rubric":
            rnd.rubric = rubric_snapshot(db)
            rnd.rule_rev = int(settings_store.load().get("_rule_rev", 0) or 0)
            if (rnd.mode or "level") == "checklist":
                rnd.checklist = checklist_snapshot(db)
        target = q.filter(Photo.has_image.is_(True))
        if action == "retry-errors":
            target = target.filter(Photo.status == "error")
        n = target.update({"status": "pending", "attempts": 0, "next_try_at": None, "error": ""},
                          synchronize_session=False)
        kept = q.filter(Photo.has_image.is_(False)).count() if action != "retry-errors" else 0
        msg = f"ส่ง {n} ภาพเข้าคิววิเคราะห์ใหม่"
        if action == "sync-rubric":
            msg = "ใช้เกณฑ์ล่าสุดกับรอบนี้แล้ว " + msg
        if kept:
            msg += f" ({kept} ภาพที่ลบภาพเต็มไปแล้วยังใช้คะแนนเดิม)"
        flash(request, msg)
    elif action == "purge-images":
        ids = [r[0] for r in q.filter(Photo.has_image.is_(True)).with_entities(Photo.id).all()]
        n = storage.purge_images(db, ids)
        storage.compact(db)
        flash(request, f"ลบภาพเต็ม {n} ภาพของรอบ {rnd.name} แล้ว คะแนน คำอธิบาย และภาพย่อยังอยู่")
    else:   # delete
        ids = [r[0] for r in q.with_entities(Photo.id).all()]
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            db.query(PhotoImage).filter(PhotoImage.photo_id.in_(chunk)).delete(synchronize_session=False)
            db.query(PhotoThumb).filter(PhotoThumb.photo_id.in_(chunk)).delete(synchronize_session=False)
        q.delete(synchronize_session=False)
        db.query(DeptSummary).filter(DeptSummary.round_id == rid).delete(synchronize_session=False)
        db.query(NotifyEvent).filter(NotifyEvent.round_id == rid).delete(synchronize_session=False)
        db.delete(rnd)
        flash(request, f"ลบรอบ {rnd.name} และข้อมูลทั้งหมดของรอบแล้ว")
    log(db, user, f"round_{action}", rnd.name)
    db.commit()
    cameras.invalidate()
    worker.wake()
    if action == "delete":
        storage.compact(db)
    return back(request.headers.get("referer") or "/admin/rounds")


@router.get("/rounds/{rid}/backup.zip")
def round_backup(rid: int, user=Depends(need("storage")), db=Depends(get_db)):
    rnd = db.get(Round, rid)
    if rnd is None:
        raise HTTPException(404, "ไม่พบรอบการตรวจนี้")
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        backup.build_round_zip(db, rnd, path, user)
    except Exception:
        os.unlink(path)
        raise
    stamp = now().strftime("%Y%m%d")
    return FileResponse(path, media_type="application/zip", background=BackgroundTask(os.unlink, path),
                        headers=download(f"5S_{rnd.name}_{stamp}.zip", f"5S_round_{rid}_{stamp}.zip"))


@router.get("/rounds/{rid}/export.xlsx")
def round_excel(rid: int):
    return back(f"/rounds/{rid}/export/scores.xlsx")       # ลิงก์ของรุ่น 1.0


# --------------------------------------------------------------------------- เกณฑ์
@router.get("/criteria", response_class=HTMLResponse)
def criteria_page(request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    items = db.query(Criterion).order_by(Criterion.sort_order, Criterion.id).all()
    total = sum(c.max_score for c in items if c.active)
    history = (db.query(AuditLog).filter(AuditLog.action.in_(RULE_ACTIONS)).order_by(AuditLog.id.desc()).limit(15).all())
    return render(request, "admin/criteria.html", user, db, items=items, total=total, history=history,
                  rule_rev=int(settings_store.load().get("_rule_rev", 0) or 0))


@router.post("/criteria/save")
def criteria_save(request: Request, form=Depends(form_data), user=Depends(need("criteria")), db=Depends(get_db)):
    cid = _int(form.get("id"))
    code = (form.get("code") or "").strip().upper()[:12]
    name = (form.get("name") or "").strip()[:120]
    if not code or not name:
        flash(request, "ใส่รหัสและชื่อเกณฑ์ให้ครบ", "err")
        return back("/admin/criteria")
    if db.query(Criterion).filter(Criterion.code == code, Criterion.id != cid).first():
        flash(request, f"รหัส {code} มีอยู่แล้ว ใช้รหัสอื่น", "err")
        return back("/admin/criteria")
    try:
        max_score = float(form.get("max_score") or 0)
    except ValueError:
        max_score = 0
    if not 0 < max_score <= 1000:
        flash(request, "คะแนนเต็มต้องมากกว่า 0 และไม่เกิน 1000", "err")
        return back("/admin/criteria")
    c = db.get(Criterion, cid) if cid else None
    if c is None:
        c = Criterion()
        db.add(c)
    c.code, c.name, c.max_score = code, name, max_score
    c.focus = (form.get("focus") or "").strip()[:1500]
    c.levels = [(form.get(f"level{i}") or "").strip()[:600] for i in range(5)]
    c.allow_na = form.get("allow_na") == "1"
    c.active = form.get("active") == "1"
    c.sort_order = _int(form.get("sort_order"), 100)
    c.kind = "sustain" if form.get("kind") == "sustain" else "ai"
    rev = bump_rule_rev(db)
    log(db, user, "save_criterion", f"ฉบับที่ {rev}: {code} {name} เต็ม {max_score}")
    db.commit()
    flash(request, f"บันทึกเกณฑ์ {name} แล้ว มีผลกับรอบที่สร้างใหม่")
    return back("/admin/criteria")


@router.post("/criteria/{cid}/delete")
def criteria_delete(cid: int, request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    c = db.get(Criterion, cid)
    if c is not None:
        log(db, user, "delete_criterion", f"ฉบับที่ {bump_rule_rev(db)}: {c.code} {c.name}")
        db.delete(c)
        db.commit()
        flash(request, f"ลบเกณฑ์ {c.name} แล้ว (รอบที่ตรวจไปแล้วยังใช้เกณฑ์ชุดเดิม)")
    return back("/admin/criteria")


@router.post("/criteria/reset")
def criteria_reset(request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    db.query(Criterion).delete()
    for i, c in enumerate(DEFAULT_CRITERIA):
        db.add(Criterion(sort_order=(i + 1) * 10, **c))
    log(db, user, "reset_criteria", f"ฉบับที่ {bump_rule_rev(db)}")
    db.commit()
    flash(request, "คืนค่าเกณฑ์ตั้งต้น 5 ข้อแล้ว")
    return back("/admin/criteria")


# --------------------------------------------------------------------------- รายการตรวจ (rule engine)
@router.get("/checkpoints", response_class=HTMLResponse)
def checkpoints_page(request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    crits = db.query(Criterion).order_by(Criterion.sort_order, Criterion.id).all()
    items = {}
    for k in db.query(Checkpoint).filter(Checkpoint.area_id.is_(None)).order_by(Checkpoint.sort_order, Checkpoint.id).all():
        items.setdefault(k.crit_code, []).append(k)
    zones = db.query(Checkpoint).filter(Checkpoint.area_id.isnot(None)).count()
    s = settings_store.load()
    return render(request, "admin/checkpoints.html", user, db, crits=crits, items=items, zones=zones, s=s,
                  rule_rev=int(s.get("_rule_rev", 0) or 0),
                  open_rounds=db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).all())


def _checkpoint_fields(k: Checkpoint, form, db) -> str:
    text_ = (form.get("text") or "").strip()[:600]
    crit = (form.get("crit_code") or "").strip().upper()
    if not text_ or db.query(Criterion).filter(Criterion.code == crit).first() is None:
        return "ใส่ข้อความของรายการตรวจ และเลือกหมวดที่มีอยู่"
    try:
        points, minor = float(form.get("points") or 5), float(form.get("minor_points") or 0)
    except ValueError:
        return "แต้มต้องเป็นตัวเลข"
    if not 0 < points <= 100 or not 0 <= minor <= points:
        return "แต้มเต็มต้องมากกว่า 0 และแต้มของบกพร่องเล็กน้อยต้องไม่เกินแต้มเต็ม"
    k.text, k.crit_code, k.points, k.minor_points = text_, crit, points, minor
    k.text_en = (form.get("text_en") or "").strip()[:600]
    k.minor_hint = (form.get("minor_hint") or "").strip()[:400]
    k.major_hint = (form.get("major_hint") or "").strip()[:400]
    k.sort_order = _int(form.get("sort_order"), 100)
    k.active = form.get("active", "1") == "1"
    return ""


@router.post("/checkpoints/save")
def checkpoint_save(request: Request, form=Depends(form_data), user=Depends(need("criteria")), db=Depends(get_db)):
    kid = _int(form.get("id"))
    code = (form.get("code") or "").strip().upper()[:12]
    k = db.get(Checkpoint, kid) if kid else None
    if not code or not code.replace("-", "").replace("_", "").isalnum() or code.startswith("Z"):
        flash(request, "รหัสข้อใช้ตัวอักษรอังกฤษและตัวเลข และไม่ขึ้นต้นด้วย Z (ตัว Z สงวนไว้ให้โซน)", "err")
        return back("/admin/checkpoints")
    if db.query(Checkpoint).filter(Checkpoint.code == code, Checkpoint.id != kid).first():
        flash(request, f"รหัสข้อ {code} มีอยู่แล้ว", "err")
        return back("/admin/checkpoints")
    if k is None:
        k = Checkpoint(code=code, text="", crit_code="")
        db.add(k)
    problem = _checkpoint_fields(k, form, db)
    if problem:
        db.rollback()
        flash(request, problem, "err")
        return back("/admin/checkpoints")
    k.code = code
    types = area_types(settings_store.load())
    k.area_types = [t for t in form.getlist("area_types") if t in types]
    k.allow_na = form.get("allow_na") == "1"
    rev = bump_rule_rev(db)
    log(db, user, "save_checkpoint", f"ฉบับที่ {rev}: {code} {k.text[:120]} ({k.points:g}/{k.minor_points:g}/0)")
    db.commit()
    flash(request, f"บันทึกข้อ {code} แล้ว มีผลกับรอบที่สร้างใหม่")
    return back("/admin/checkpoints")


@router.post("/checkpoints/reset")
def checkpoints_reset(request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    db.query(Checkpoint).filter(Checkpoint.area_id.is_(None)).delete()
    db.flush()
    seed_checkpoints(db)
    log(db, user, "reset_checkpoints", f"ฉบับที่ {bump_rule_rev(db)}")
    db.commit()
    flash(request, "คืนค่ารายการตรวจตั้งต้น 13 ข้อแล้ว (โซนของจุดตรวจไม่ถูกแตะ)")
    return back("/admin/checkpoints")


@router.post("/checkpoints/{kid}/delete")
def checkpoint_delete(kid: int, request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    k = db.get(Checkpoint, kid)
    target = "/admin/checkpoints"
    if k is not None:
        if k.area_id:
            target = f"/admin/areas/{k.area_id}/zones"
        log(db, user, "delete_zone" if k.area_id else "delete_checkpoint", f"ฉบับที่ {bump_rule_rev(db)}: {k.code} {k.text[:120]}")
        db.delete(k)
        db.commit()
        flash(request, f"ลบข้อ {k.code} แล้ว รอบที่ตรวจไปแล้วยังใช้รายการชุดเดิม")
    return back(target)


@router.get("/areas/{aid}/zones", response_class=HTMLResponse)
def zones_page(aid: int, request: Request, user=Depends(need("criteria")), db=Depends(get_db)):
    area = db.get(AuditArea, aid)
    if area is None:
        raise HTTPException(404, "ไม่พบจุดตรวจนี้")
    q = db.query(Photo).filter(Photo.area_id == aid, Photo.has_image.is_(True))
    ref = (q.filter(Photo.camera_id.isnot(None)).order_by(Photo.id.desc()).first() or q.order_by(Photo.id.desc()).first())
    return render(request, "admin/zones.html", user, db, area=area, ref=ref,
                  from_camera=bool(ref and rules.fixed_view(ref)),
                  items=db.query(Checkpoint).filter(Checkpoint.area_id == aid).order_by(Checkpoint.id).all(),
                  crits=db.query(Criterion).filter(Criterion.active.is_(True)).order_by(Criterion.sort_order).all(),
                  cams=db.query(Camera).filter(Camera.department_id == area.department_id,
                                               Camera.area_name == area.name).count())


@router.post("/areas/{aid}/zones/save")
def zone_save(aid: int, request: Request, form=Depends(form_data), user=Depends(need("criteria")), db=Depends(get_db)):
    area = db.get(AuditArea, aid)
    if area is None:
        raise HTTPException(404, "ไม่พบจุดตรวจนี้")
    box = [_int(form.get(n), -1, 0, 1000) for n in ("x1", "y1", "x2", "y2")]
    if box[2] - box[0] < 30 or box[3] - box[1] < 30:
        flash(request, "ลากกรอบบนภาพก่อนบันทึก กรอบต้องไม่เล็กเกินไป", "err")
        return back(f"/admin/areas/{aid}/zones")
    k = Checkpoint(code="ZTMP", text="", crit_code="", area_id=aid, zone=box, allow_na=False, area_types=[])
    db.add(k)
    problem = _checkpoint_fields(k, form, db)
    if problem:
        db.rollback()
        flash(request, problem, "err")
        return back(f"/admin/areas/{aid}/zones")
    db.flush()
    k.code = f"Z{k.id}"
    rev = bump_rule_rev(db)
    log(db, user, "save_zone", f"ฉบับที่ {rev}: {k.code} {area.name}: {k.text[:120]}")
    db.commit()
    flash(request, f"บันทึกโซน {k.code} แล้ว ใช้กับภาพจากกล้องติดตายของจุดนี้ในรอบที่สร้างใหม่")
    return back(f"/admin/areas/{aid}/zones")


@router.get("/quality", response_class=HTMLResponse)
def quality_page(request: Request, round: int = 0, user=Depends(need("score")), db=Depends(get_db)):
    return render(request, "admin/quality.html", user, db, q=scoring.ai_quality(db, round), round=round,
                  rounds=db.query(Round).filter(Round.mode == "checklist").order_by(Round.id.desc()).all())


# --------------------------------------------------------------------------- แผนก
@router.get("/departments", response_class=HTMLResponse)
def departments_page(request: Request, user=Depends(need("departments")), db=Depends(get_db)):
    items = db.query(Department).order_by(Department.active.desc(), Department.name).all()
    counts = dict(db.query(Photo.department_id, func.count(Photo.id)).group_by(Photo.department_id).all())
    return render(request, "admin/departments.html", user, db, items=items, counts=counts)


@router.post("/departments/save")
def department_save(request: Request, form=Depends(form_data), user=Depends(need("departments")), db=Depends(get_db)):
    did = _int(form.get("id"))
    code = (form.get("code") or "").strip().upper()[:20]
    name = (form.get("name") or "").strip()[:120]
    if not code or not name:
        flash(request, "ใส่รหัสและชื่อแผนกให้ครบ", "err")
        return back("/admin/departments")
    if db.query(Department).filter(Department.code == code, Department.id != did).first():
        flash(request, f"รหัสแผนก {code} มีอยู่แล้ว", "err")
        return back("/admin/departments")
    d = db.get(Department, did) if did else None
    if d is None:
        d = Department()
        db.add(d)
    d.code, d.name = code, name
    d.name_en = (form.get("name_en") or "").strip()[:120]
    d.zone = (form.get("zone") or "").strip()[:120]
    d.active = form.get("active", "1") == "1"
    log(db, user, "save_department", f"{code} {name}")
    db.commit()
    flash(request, f"บันทึกแผนก {name} แล้ว")
    return back("/admin/departments")


@router.post("/departments/{did}/delete")
def department_delete(did: int, request: Request, user=Depends(need("departments")), db=Depends(get_db)):
    d = db.get(Department, did)
    if d is None:
        return back("/admin/departments")
    in_use = (db.query(Photo).filter(Photo.department_id == did).count()
              or db.query(Camera).filter(Camera.department_id == did).count())
    if in_use:
        d.active = False
        flash(request, f"แผนก {d.name} มีภาพหรือกล้องในระบบ จึงปิดการใช้งานแทนการลบ", "warn")
    else:
        db.query(User).filter(User.department_id == did).update({"department_id": None})
        db.query(Channel).filter(Channel.department_id == did).update({"department_id": None, "active": False})
        db.query(DeptSummary).filter(DeptSummary.department_id == did).delete()
        db.delete(d)
        flash(request, f"ลบแผนก {d.name} แล้ว")
    log(db, user, "delete_department", d.name)
    db.commit()
    notify.refresh_channels(db)
    return back("/admin/departments")


# --------------------------------------------------------------------------- จุดตรวจที่โรงงานกำหนด
@router.get("/areas", response_class=HTMLResponse)
def areas_page(request: Request, user=Depends(need("departments")), db=Depends(get_db)):
    depts = db.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all()
    items = {}
    for a in db.query(AuditArea).order_by(AuditArea.active.desc(), AuditArea.sort_order, AuditArea.id).all():
        items.setdefault(a.department_id, []).append(a)
    counts = dict(db.query(Photo.area_id, func.count(Photo.id)).filter(Photo.area_id.isnot(None))
                  .group_by(Photo.area_id).all())
    return render(request, "admin/areas.html", user, db, depts=depts, items=items, counts=counts,
                  s=settings_store.load())


@router.post("/areas/save")
def area_save(request: Request, form=Depends(form_data), user=Depends(need("departments")), db=Depends(get_db)):
    aid = _int(form.get("id"))
    dept = db.get(Department, _int(form.get("department_id")))
    name = (form.get("name") or "").strip()[:160]
    if dept is None or not name:
        flash(request, "เลือกแผนกและใส่ชื่อจุดตรวจ", "err")
        return back("/admin/areas")
    if db.query(AuditArea).filter(AuditArea.department_id == dept.id, AuditArea.name == name, AuditArea.id != aid).first():
        flash(request, f"แผนก {dept.name} มีจุดตรวจชื่อ {name} อยู่แล้ว", "err")
        return back("/admin/areas")
    a = db.get(AuditArea, aid) if aid else None
    if a is None:
        a = AuditArea()
        db.add(a)
    types = area_types(settings_store.load())
    a.department_id, a.name = dept.id, name
    a.area_type = form.get("area_type") if form.get("area_type") in types else types[0]
    a.standard = (form.get("standard") or "").strip()[:1500]
    a.required = form.get("required") == "1"
    a.active = form.get("active", "1") == "1"
    a.sort_order = _int(form.get("sort_order"), 100)
    log(db, user, "save_area", f"{dept.name} / {name}{' (บังคับ)' if a.required else ''}")
    db.commit()
    flash(request, f"บันทึกจุดตรวจ {name} แล้ว")
    return back("/admin/areas")


@router.post("/areas/{aid}/delete")
def area_delete(aid: int, request: Request, user=Depends(need("departments")), db=Depends(get_db)):
    a = db.get(AuditArea, aid)
    if a is not None:
        if db.query(Photo).filter(Photo.area_id == aid).count():
            a.active = False
            flash(request, f"จุดตรวจ {a.name} มีภาพในระบบ จึงปิดใช้แทนการลบ ภาพและคะแนนเดิมยังอยู่", "warn")
        else:
            db.delete(a)
            flash(request, f"ลบจุดตรวจ {a.name} แล้ว")
        log(db, user, "delete_area", a.name)
        db.commit()
    return back("/admin/areas")


# --------------------------------------------------------------------------- ผู้ใช้และสิทธิ์รายบัญชี
@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    items = db.query(User).order_by(User.active.desc(), User.role, User.username).all()
    depts = db.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all()
    return render(request, "admin/users.html", user, db, items=items, depts=depts,
                  role_perms={k: sorted(v) for k, v in security.ROLE_PERMS.items()})


@router.post("/users/save")
def user_save(request: Request, form=Depends(form_data), user=Depends(admin_user), db=Depends(get_db)):
    uid = _int(form.get("id"))
    username = (form.get("username") or "").strip().lower()[:50]
    role = form.get("role") if form.get("role") in config.ROLES else "member"
    dept_id = _int(form.get("department_id")) or None
    password = form.get("password") or ""
    target = db.get(User, uid) if uid else None
    if not username or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
        flash(request, "ชื่อผู้ใช้ใช้ได้เฉพาะตัวอักษรอังกฤษ ตัวเลข จุด ขีด และขีดล่าง", "err")
        return back("/admin/users")
    if db.query(User).filter(User.username == username, User.id != uid).first():
        flash(request, f"ชื่อผู้ใช้ {username} มีอยู่แล้ว", "err")
        return back("/admin/users")
    extra = sorted({_int(x) for x in form.getlist("extra_depts") if _int(x)} - {dept_id or 0})
    perms = {}
    for key in security.PERMS:
        v = form.get(f"perm_{key}")
        if v in ("1", "0"):
            perms[key] = v == "1"
    if role == "member" and not dept_id and not extra and not perms.get("upload_all"):
        flash(request, "ตัวแทนแผนกต้องเลือกแผนก", "err")
        return back("/admin/users")
    if target is None or password:
        problem = security.password_problem(password)
        if problem:
            flash(request, problem, "err")
            return back("/admin/users")
    active = form.get("active", "1") == "1"
    if target is not None and target.role == "admin" and (role != "admin" or not active):
        others = db.query(User).filter(User.role == "admin", User.active.is_(True), User.id != target.id).count()
        if others == 0:
            flash(request, "ต้องมีผู้ดูแลระบบที่ใช้งานได้อย่างน้อย 1 คน", "err")
            return back("/admin/users")
    if target is None:
        target = User(username=username, password_hash="")
        db.add(target)
    target.username, target.role, target.department_id, target.active = username, role, dept_id, active
    target.full_name = (form.get("full_name") or "").strip()[:120]
    target.perms = {} if role == "admin" else perms
    target.extra_depts = extra
    if password:
        target.password_hash = security.hash_password(password)
        target.must_change = target.id != user.id        # ให้เจ้าของบัญชีตั้งรหัสเองตอนเข้าครั้งแรก
    changed = ", ".join(f"{'เปิด' if v else 'ปิด'} {security.PERMS[k][0]}" for k, v in perms.items())
    log(db, user, "save_user", f"{username} ({role}) {changed}")
    db.commit()
    if target.id == user.id and password:
        request.session.update(uid=user.id, stamp=security.session_stamp(target))
    flash(request, f"บันทึกผู้ใช้ {username} แล้ว")
    return back("/admin/users")


# --------------------------------------------------------------------------- ตั้งค่า
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    return render(request, "admin/settings.html", user, db, s=settings_store.load(),
                  used_today=worker.usage_today(db))


@router.post("/settings")
def settings_save(request: Request, form=Depends(form_data), user=Depends(admin_user), db=Depends(get_db)):
    old = settings_store.load()
    types = [x.strip()[:60] for x in (form.get("area_types") or "").splitlines() if x.strip()][:30]
    good = _int(form.get("band_good"), 80, 1, 100)
    url = (form.get("public_url") or "").strip().rstrip("/")[:200]
    new = {"org_name": (form.get("org_name") or "").strip()[:120] or old["org_name"],
           "public_url": url if url.startswith(("http://", "https://")) else "",
           "area_types": list(dict.fromkeys(types)) or old["area_types"],
           "band_good": good, "band_mid": _int(form.get("band_mid"), 60, 0, max(good - 1, 0)),
           "ai_extra": (form.get("ai_extra") or "").strip()[:3000],
           "ai_passes": 2 if form.get("ai_passes") == "2" else 1,
           "after_replaces": form.get("after_replaces") == "1",
           "webcam_width": _int(form.get("webcam_width"), 1920, 640, 3840),
           "allow_free_area": form.get("allow_free_area") == "1",
           "require_coverage": form.get("require_coverage") == "1",
           "verify_required": form.get("verify_required") == "1",
           "host_limit_mb": _int(form.get("host_limit_mb"), 500, 20, 1000000),
           "storage_warn_pct": _int(form.get("storage_warn_pct"), 80, 10, 99),
           "max_photos_per_dept": _int(form.get("max_photos_per_dept"), 0, 0, 100000),
           "backup_remind_days": _int(form.get("backup_remind_days"), 7, 0, 365),
           "scoring_mode": "checklist" if form.get("scoring_mode") == "checklist" else "level",
           "auto_actions": form.get("auto_actions") == "1",
           "action_due_days": _int(form.get("action_due_days"), 7, 0, 365),
           "action_due_days_major": _int(form.get("action_due_days_major"), 3, 0, 365),
           "ai_rpm": _int(form.get("ai_rpm"), 6, 1, 60),
           "ai_daily": _int(form.get("ai_daily"), 200, 1, 100000),
           "ai_max_attempts": _int(form.get("ai_max_attempts"), 4, 1, 10),
           "img_max_side": _int(form.get("img_max_side"), 1280, 640, 2400),
           "img_quality": _int(form.get("img_quality"), 78, 50, 92),
           "allow_gallery": form.get("allow_gallery") == "1",
           "storage_budget_mb": _int(form.get("storage_budget_mb"), 350, 20, 100000),
           "retention_days": _int(form.get("retention_days"), 90, 0, 3650),
           "purge_requires_backup": form.get("purge_requires_backup") == "1",
           "ranking_visibility": "closed" if form.get("ranking_visibility") == "closed" else "always",
           "member_see_all": form.get("member_see_all") == "1"}
    for slot in ("ai1", "ai2"):
        kind = form.get(f"{slot}_type")
        new[f"{slot}_type"] = kind if kind in ("none", "gemini", "openai", "demo") else "none"
        new[f"{slot}_base"] = (form.get(f"{slot}_base") or "").strip()[:300]
        new[f"{slot}_model"] = (form.get(f"{slot}_model") or "").strip()[:120]
        key = (form.get(f"{slot}_key") or "").strip()
        if form.get(f"{slot}_key_clear") == "1":
            new[f"{slot}_key"] = ""
        elif key:
            new[f"{slot}_key"] = key[:400]
    settings_store.save(db, new)
    log(db, user, "save_settings", f"AI หลัก {new['ai1_type']} {new['ai1_model']}, สำรอง {new['ai2_type']} {new['ai2_model']}, "
                                   f"ประเมิน {new['ai_passes']} รอบ")
    db.commit()
    worker.wake()
    flash(request, "บันทึกการตั้งค่าแล้ว")
    return back("/admin/settings")


def _cfg_from(body: dict, s: dict) -> dict:
    slot = "ai2" if body.get("slot") == "ai2" else "ai1"
    return dict(slot=slot, type=body.get("type") or s[f"{slot}_type"],
                base=(body.get("base") or "").strip(), model=(body.get("model") or "").strip(),
                key=(body.get("key") or "").strip() or s.get(f"{slot}_key", ""))


def _test_image() -> bytes:
    """ภาพจำลองชั้นวางของ ใช้ทดสอบว่าโมเดลรับภาพและตอบตามรูปแบบได้"""
    im = Image.new("RGB", (640, 480), (214, 218, 212))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 360, 640, 480], fill=(120, 134, 126))
    d.line([0, 372, 640, 372], fill=(242, 183, 5), width=8)
    for y in (90, 190, 290):
        d.rectangle([60, y, 580, y + 12], fill=(70, 80, 90))
    for i, x in enumerate(range(80, 560, 80)):
        d.rectangle([x, 120, x + 56, 190], fill=[(42, 93, 168), (31, 122, 77), (200, 50, 31)][i % 3])
        d.rectangle([x, 222, x + 56, 290], fill=(180, 150, 100))
    d.rectangle([420, 380, 520, 450], fill=(150, 110, 70))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


@router.post("/ai/models")
async def ai_models(request: Request, user=Depends(admin_user)):
    import anyio
    cfg = _cfg_from(await request.json(), settings_store.load())
    if cfg["type"] not in ("gemini", "openai"):
        return JSONResponse({"ok": False, "error": "เลือกผู้ให้บริการก่อน"})
    try:
        models = await anyio.to_thread.run_sync(ai.list_models, cfg)
        tested = False
        if cfg["type"] == "gemini":
            likely = models
        elif cfg.get("_known_vision"):                       # ผู้ให้บริการบอกเองว่ารุ่นใดรับภาพได้ (เช่น OpenRouter)
            likely, tested = [m for m in models if m in cfg["_known_vision"]], True
        elif models and not ai.is_local(cfg["base"]):        # ไม่บอก: ถามแต่ละรุ่นด้วยภาพทดสอบเล็ก ๆ
            found, complete = await anyio.to_thread.run_sync(ai.find_vision, cfg, models)
            if complete or found:
                likely, tested = found, True
                models = found + [m for m in models if m not in found]
            else:
                likely = [m for m in models if ai.likely_vision(m)]
        else:
            likely = [m for m in models if ai.likely_vision(m)]
        return {"ok": True, "models": models, "likely": likely, "type": cfg["type"], "tested": tested}
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/ai/test")
async def ai_test(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    import anyio
    s = settings_store.load()
    cfg = _cfg_from(await request.json(), s)
    rubric = rubric_snapshot(db)
    if cfg["type"] == "demo":
        return {"ok": True, "message": "โหมดทดลองไม่เรียก AI จริง คะแนนที่ได้ใช้จัดอันดับไม่ได้"}
    if cfg["type"] not in ("gemini", "openai"):
        return JSONResponse({"ok": False, "error": "เลือกผู้ให้บริการก่อน"})
    if not rubric:
        return JSONResponse({"ok": False, "error": "ยังไม่มีเกณฑ์ที่เปิดใช้"})
    test = {"ai1_type": cfg["type"], "ai1_base": cfg["base"], "ai1_key": cfg["key"], "ai1_model": cfg["model"],
            "ai_extra": s.get("ai_extra", "")}

    def run():
        return ai.analyze(_test_image(), rubric, dict(area_type="คลังสินค้า", area_name="ภาพทดสอบระบบ", note=""),
                          test, on_call=worker.count_call)
    try:
        result, _, model = await anyio.to_thread.run_sync(run)
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    if not result["image_ok"]:
        return {"ok": True, "message": f"เชื่อมต่อ {model} ได้ โมเดลตอบว่าภาพทดสอบประเมินไม่ได้ ({result['image_issue']}) ซึ่งถือว่ารูปแบบคำตอบถูกต้อง"}
    sc, mx, _pct = ai.totals(result["criteria"])
    return {"ok": True, "message": f"เชื่อมต่อ {model} ได้ และตอบตามรูปแบบครบ {len(result['criteria'])} เกณฑ์ "
                                   f"(ภาพทดสอบได้ {sc:g}/{mx:g}) พร้อมใช้งาน"}


# --------------------------------------------------------------------------- การแจ้งเตือน
@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    return render(request, "admin/notifications.html", user, db,
                  items=db.query(Channel).order_by(Channel.active.desc(), Channel.id).all(),
                  depts=db.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all(),
                  logs=db.query(NotifyLog).order_by(NotifyLog.id.desc()).limit(60).all(),
                  KINDS=notify.KINDS, EVENTS=notify.EVENTS, s=settings_store.load())


@router.post("/notifications/save")
def notification_save(request: Request, form=Depends(form_data), user=Depends(admin_user), db=Depends(get_db)):
    cid = _int(form.get("id"))
    kind = form.get("kind")
    name = (form.get("name") or "").strip()[:120]
    if kind not in notify.KINDS or not name:
        flash(request, "ใส่ชื่อและเลือกชนิดของช่องทาง", "err")
        return back("/admin/notifications")
    ch = db.get(Channel, cid) if cid else None
    old = dict(ch.config or {}) if ch is not None and ch.kind == kind else {}
    cfg = {}
    for field, _label, secret, _hint in notify.KINDS[kind][1]:
        value = (form.get(f"cfg_{kind}_{field}") or "").strip()[:600]
        cfg[field] = value or (old.get(field, "") if secret else "")      # ช่องลับเว้นว่าง = ใช้ค่าเดิม
    events = [e for e in notify.EVENTS if form.get(f"ev_{e}") == "1"]
    if not events:
        flash(request, "เลือกเรื่องที่ต้องการแจ้งอย่างน้อย 1 เรื่อง", "err")
        return back("/admin/notifications")
    if ch is None:
        ch = Channel()
        db.add(ch)
    ch.name, ch.kind, ch.config, ch.events = name, kind, cfg, events
    ch.department_id = _int(form.get("department_id")) or None
    ch.active = form.get("active", "1") == "1"
    log(db, user, "save_channel", f"{name} ({kind}) {', '.join(events)}")
    db.commit()
    notify.refresh_channels(db)
    flash(request, f"บันทึกช่องทาง {name} แล้ว กดส่งข้อความทดสอบเพื่อยืนยัน")
    return back("/admin/notifications")


@router.post("/notifications/{cid}/{action}")
def notification_action(cid: int, action: str, request: Request, user=Depends(admin_user), db=Depends(get_db)):
    ch = db.get(Channel, cid)
    if ch is None:
        raise HTTPException(404, "ไม่พบช่องทางนี้")
    if action == "delete":
        log(db, user, "delete_channel", ch.name)
        db.delete(ch)
        flash(request, f"ลบช่องทาง {ch.name} แล้ว")
    elif action == "test":
        scope = f"แผนก {ch.department.name}" if ch.department else "ทุกแผนก"
        try:
            notify.send(ch.kind, ch.config, "[5ส Vision] ข้อความทดสอบ",
                        f"ช่องทาง \"{ch.name}\" พร้อมรับการแจ้งเตือนของ{scope}\nส่งโดย {user.full_name or user.username}",
                        dict(event="test"))
            ch.last_ok_at, ch.last_error = now(), ""
            db.add(NotifyLog(channel=ch.name, kind="test", ok=True, detail="ข้อความทดสอบ"))
            flash(request, f"ส่งข้อความทดสอบไปที่ {ch.name} แล้ว ตรวจที่ปลายทางว่าได้รับ")
        except notify.NotifyError as e:
            ch.last_error = str(e)[:500]
            db.add(NotifyLog(channel=ch.name, kind="test", ok=False, detail=str(e)[:500]))
            flash(request, f"ส่งไม่สำเร็จ: {e}", "err")
    else:
        raise HTTPException(404, "ไม่รู้จักคำสั่งนี้")
    db.commit()
    notify.refresh_channels(db)
    return back("/admin/notifications")


# --------------------------------------------------------------------------- กล้อง IP
@router.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    s = settings_store.load()
    return render(request, "admin/cameras.html", user, db,
                  items=db.query(Camera).order_by(Camera.active.desc(), Camera.name).all(),
                  depts=db.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all(),
                  token=s.get("agent_token", ""), agent_online=cameras.agent_online(), s=s)


@router.post("/cameras/save")
def camera_save(request: Request, form=Depends(form_data), user=Depends(admin_user), db=Depends(get_db)):
    cid = _int(form.get("id"))
    name = (form.get("name") or "").strip()[:120]
    dept = db.get(Department, _int(form.get("department_id")))
    url = (form.get("url") or "").strip()[:1000]
    source = "rtsp" if form.get("source") == "rtsp" else "snapshot"
    if not name or dept is None or not url:
        flash(request, "ใส่ชื่อกล้อง แผนก และลิงก์ของกล้องให้ครบ", "err")
        return back("/admin/cameras")
    ok_scheme = ("rtsp://",) if source == "rtsp" else ("http://", "https://")
    if not url.lower().startswith(ok_scheme):
        flash(request, f"ลิงก์ของกล้องต้องขึ้นต้นด้วย {' หรือ '.join(ok_scheme)}", "err")
        return back("/admin/cameras")
    cam = db.get(Camera, cid) if cid else None
    if cam is None:
        cam = Camera()
        db.add(cam)
    cam.name, cam.department_id, cam.url, cam.source = name, dept.id, url, source
    cam.area_name = (form.get("area_name") or "").strip()[:160] or name
    types = area_types(settings_store.load())
    cam.area_type = form.get("area_type") if form.get("area_type") in types else types[0]
    cam.mode = "agent" if form.get("mode") == "agent" else "direct"
    cam.auth = "digest" if form.get("auth") == "digest" else "basic"
    cam.username = (form.get("username") or "").strip()[:120]
    if form.get("password"):
        cam.password = form.get("password")[:200]
    elif not cam.username:
        cam.password = ""
    cam.active = form.get("active", "1") == "1"
    log(db, user, "save_camera", f"{name} ({cam.mode}, {cam.source}) แผนก {dept.name}")
    db.commit()
    cameras.invalidate()
    flash(request, f"บันทึกกล้อง {name} แล้ว")
    return back("/admin/cameras")


@router.post("/cameras/token")
def camera_token(request: Request, user=Depends(admin_user), db=Depends(get_db)):
    settings_store.save(db, {"agent_token": secrets.token_urlsafe(24)})
    log(db, user, "new_agent_token")
    db.commit()
    flash(request, "สร้างรหัสของโปรแกรมกล้องใหม่แล้ว รหัสเดิมใช้ไม่ได้อีก นำรหัสใหม่ไปใส่ในเครื่องที่รัน camera_agent")
    return back("/admin/cameras")


@router.post("/cameras/{cid}/delete")
def camera_delete(cid: int, request: Request, user=Depends(admin_user), db=Depends(get_db)):
    cam = db.get(Camera, cid)
    if cam is not None:
        log(db, user, "delete_camera", cam.name)
        db.delete(cam)
        db.commit()
        cameras.invalidate()
        flash(request, f"ลบกล้อง {cam.name} แล้ว ภาพที่ถ่ายไว้ยังอยู่")
    return back("/admin/cameras")


@router.get("/cameras/{cid}/test.jpg")
def camera_test(cid: int, user=Depends(admin_user), db=Depends(get_db)):
    cam = db.get(Camera, cid)
    if cam is None:
        raise HTTPException(404, "ไม่พบกล้องนี้")
    if cam.mode == "agent":
        raise HTTPException(400, "กล้องนี้ถ่ายผ่านโปรแกรมบนเครื่องในโรงงาน ทดสอบด้วยคำสั่ง camera_agent --once")
    try:
        data = cameras.grab(cam)
        cam.last_error = ""
        db.commit()
    except cameras.CameraError as e:
        cam.last_error = str(e)[:500]
        db.commit()
        raise HTTPException(502, str(e))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- พื้นที่และสำรองข้อมูล
@router.get("/storage", response_class=HTMLResponse)
def storage_page(request: Request, user=Depends(need("storage")), db=Depends(get_db)):
    s = settings_store.load()
    rounds = db.query(Round).order_by(Round.id.desc()).all()
    stats = {}
    for rid, n, size in db.query(Photo.round_id, func.count(Photo.id), func.sum(Photo.image_bytes)) \
            .filter(Photo.has_image.is_(True)).group_by(Photo.round_id).all():
        stats[rid] = dict(images=n, bytes=int(size or 0))
    totals = dict(db.query(Photo.round_id, func.count(Photo.id)).group_by(Photo.round_id).all())
    newer = {}
    for r in rounds:
        if r.last_backup_at:
            newer[r.id] = db.query(Photo).filter(Photo.round_id == r.id, Photo.created_at > r.last_backup_at).count()
    return render(request, "admin/storage.html", user, db, usage=storage.usage(db, s), rounds=rounds, stats=stats,
                  totals=totals, newer=newer, s=s, is_sqlite=storage.IS_SQLITE)


@router.post("/storage/cleanup")
def storage_cleanup(request: Request, user=Depends(need("storage")), db=Depends(get_db)):
    res = storage.auto_cleanup(db, settings_store.load())
    storage.compact(db)
    n = res["by_age"] + res["by_space"]
    flash(request, f"ลบภาพเต็มตามนโยบาย {n} ภาพ" if n else "ไม่มีภาพที่เข้าเงื่อนไขการลบตามนโยบายตอนนี้",
          "ok" if n else "warn")
    return back("/admin/storage")


@router.post("/storage/compact")
def storage_compact(request: Request, user=Depends(need("storage")), db=Depends(get_db)):
    before = storage.physical_bytes(db)
    storage.compact(db, full=True)
    after = storage.physical_bytes(db)
    log(db, user, "compact_storage", f"{(before or 0) / storage.MB:.1f} MB -> {(after or 0) / storage.MB:.1f} MB")
    db.commit()
    flash(request, f"คืนพื้นที่แล้ว ขนาดฐานข้อมูลก่อนทำ {(before or 0) / storage.MB:.1f} MB หลังทำ {(after or 0) / storage.MB:.1f} MB")
    return back("/admin/storage")


@router.get("/backup/system.json")
def system_backup(user=Depends(need("storage")), db=Depends(get_db)):
    data = json.dumps(backup.system_json(db), ensure_ascii=False, indent=1).encode("utf-8")
    log(db, user, "backup_system")
    db.commit()
    name = f"5S_system_{now():%Y%m%d}.json"
    return Response(data, media_type="application/json", headers=download(name, name))


@router.post("/restore")
def restore(request: Request, file: UploadFile = File(...), user=Depends(need("storage")), db=Depends(get_db)):
    s = settings_store.load()
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    u = storage.usage(db, s)
    if size > config.MAX_RESTORE_BYTES:
        flash(request, "ไฟล์สำรองใหญ่เกิน 600 MB", "err")
    elif u["logical"] + size > u["budget"]:
        flash(request, "พื้นที่ไม่พอสำหรับนำรอบนี้กลับ เพิ่มงบพื้นที่ในหน้าตั้งค่า หรือลบภาพเก่าก่อน", "err")
    else:
        try:
            rnd = backup.restore_round_zip(db, file.file, user)
            flash(request, f"นำรอบ {rnd.name} กลับเข้าระบบแล้ว")
        except ValueError as e:
            db.rollback()
            flash(request, str(e), "err")
    return back("/admin/storage")


# --------------------------------------------------------------------------- ปรับคะแนน / วิเคราะห์ใหม่ / สรุปแผนก
@router.post("/photos/{pid}/override")
def photo_override(pid: int, request: Request, form=Depends(form_data), user=Depends(need("score")),
                   db=Depends(get_db)):
    p = db.get(Photo, pid)
    if p is None:
        raise HTTPException(404, "ไม่พบภาพนี้")
    if not may_verify(user, p):
        raise HTTPException(403, "ปรับคะแนนภาพที่ตัวเองส่งไม่ได้ ให้กรรมการคนอื่นเป็นผู้ตรวจ")
    note = (form.get("note") or "").strip()[:1000]
    if len(note) < 5:
        flash(request, "ใส่เหตุผลที่ปรับคะแนน เพื่อให้ตรวจสอบย้อนหลังได้", "err")
        return back(f"/photos/{pid}")
    rnd = db.get(Round, p.round_id)
    old = p.analysis or {}
    if (rnd.mode or "level") == "checklist":
        prev = {c["code"]: c for c in old.get("checks", [])}
        checks = []
        for k in rules.applicable(rnd.checklist or [], rnd.rubric or [], p):
            v, b = form.get(f"check_{k['code']}") or "", prev.get(k["code"], {})
            if v not in rules.STATUS or (v == "na" and not k.get("allow_na", True)):
                flash(request, f"เลือกผลของข้อ {k['code']}", "err")
                return back(f"/photos/{pid}")
            ai_status = b.get("ai_status", b.get("status"))          # สิ่งที่ AI เสนอไว้เดิม เก็บไว้วัดความแม่น
            checks.append(dict(code=k["code"], text=k["text"], crit=k["crit"], max=float(k["points"]), minor=float(k["minor"]),
                               zone=list(k["zone"]) if k.get("zone") else None, status=v, points=0.0, evidence=b.get("evidence", ""),
                               action=b.get("action", "") if v in rules.NG else "", box=b.get("box") if v in rules.NG else None,
                               ai_status=ai_status, changed=ai_status != v))
        original = old.get("ai_original") or (dict(criteria=old.get("criteria", []), score=p.score, max=p.max_score,
                                                   percent=p.percent, model=p.model, status=p.status) if old else None)
        result = rules.finalize(db, p, list(rnd.rubric or []), dict(
            image_ok=True, image_issue="", scene=old.get("scene", ""), summary=old.get("summary", ""),
            top_actions=old.get("top_actions", []), checks=checks, ai_original=original))
        worker.apply_result(p, result, p.provider or "manual", p.model or "manual")
        p.overridden, p.override_by, p.override_note, p.override_at = True, user.full_name or user.username, note, now()
        p.review_flag = False
        p.verified_by, p.verified_at = user.full_name or user.username, now()
        changed = [c["code"] for c in checks if c["changed"]]
        log(db, user, "override_score", f"ภาพ {p.id}: แก้ข้อ {', '.join(changed) or '-'} เหตุผล: {note}")
        if settings_store.load().get("auto_actions", True):
            act_mod.create_for_photo(db, p, user, settings_store.load())
        notify.emit(db, "result", round_id=p.round_id, department_id=p.department_id, photo_id=p.id)
        db.commit()
        flash(request, "บันทึกผลที่ปรับแล้ว ระบบคิดคะแนนใหม่ตามกติกา")
        return back(f"/photos/{pid}")
    prev = {c["code"]: c for c in old.get("criteria", [])}
    crit = []
    for c in (rnd.rubric or []):
        v = form.get(f"level_{c['code']}") or ""
        before = prev.get(c["code"], {})
        na = v == "na" and c.get("allow_na")
        level = None if na else _int(v, -1)
        if not na and not 0 <= level <= ai.MAX_LEVEL:
            flash(request, f"เลือกระดับของเกณฑ์ {c['name']}", "err")
            return back(f"/photos/{pid}")
        crit.append(dict(code=c["code"], name=c["name"], max=float(c["max"]), na=bool(na), level=level,
                         score=0.0 if na else ai.score_of(c["max"], level),
                         reason=before.get("reason", ""), findings=before.get("findings", []),
                         recommendations=before.get("recommendations", []),
                         changed=before.get("level") != level or bool(before.get("na")) != bool(na)))
    original = old.get("ai_original") or (dict(criteria=old.get("criteria", []), score=p.score, max=p.max_score,
                                               percent=p.percent, model=p.model, status=p.status) if old else None)
    result = dict(image_ok=True, image_issue="", scene=old.get("scene", ""), summary=old.get("summary", ""),
                  top_actions=old.get("top_actions", []), criteria=crit, ai_original=original)
    worker.apply_result(p, result, p.provider or "manual", p.model or "manual")
    p.overridden, p.override_by, p.override_note, p.override_at = True, user.full_name or user.username, note, now()
    p.review_flag = False
    p.verified_by, p.verified_at = user.full_name or user.username, now()
    log(db, user, "override_score", f"ภาพ {p.id}: {p.score:g}/{p.max_score:g} เหตุผล: {note}")
    notify.emit(db, "result", round_id=p.round_id, department_id=p.department_id, photo_id=p.id)
    db.commit()
    flash(request, "บันทึกคะแนนที่ปรับแล้ว รายงานจะระบุว่าภาพนี้ปรับโดยกรรมการ")
    return back(f"/photos/{pid}")


@router.post("/photos/{pid}/{action}")
def photo_action(pid: int, action: str, request: Request, user=Depends(need("score")), db=Depends(get_db)):
    p = db.get(Photo, pid)
    if p is None:
        raise HTTPException(404, "ไม่พบภาพนี้")
    if action == "reanalyze":
        if not p.has_image:
            flash(request, "ภาพเต็มถูกลบไปแล้ว จึงวิเคราะห์ใหม่ไม่ได้", "err")
        else:
            p.status, p.attempts, p.next_try_at, p.error = "pending", 0, None, ""
            log(db, user, "reanalyze_photo", f"ภาพ {p.id}")
            db.commit()
            worker.wake()
            flash(request, "ส่งภาพเข้าคิววิเคราะห์ใหม่แล้ว")
    elif action in ("verify", "accept"):
        if not may_verify(user, p):
            raise HTTPException(403, "ยืนยันภาพที่ตัวเองส่งไม่ได้ ให้กรรมการคนอื่นเป็นผู้ตรวจ")
        if p.status != "done":
            flash(request, "ยืนยันได้เฉพาะภาพที่ให้คะแนนแล้ว", "err")
        else:
            p.review_flag = False
            p.verified_by, p.verified_at = user.full_name or user.username, now()
            log(db, user, "verify_photo", f"ภาพ {p.id} {p.department.name} / {p.area_name}: ยืนยัน {p.score:g}/{p.max_score:g}")
            made = act_mod.create_for_photo(db, p, user, settings_store.load()) if settings_store.load().get("auto_actions", True) else 0
            db.commit()
            if made:
                flash(request, f"สร้างงานแก้ไข {made} งานจากข้อที่ไม่ผ่าน")
            flash(request, f"ยืนยันผลของภาพ {p.id} แล้ว")
        target = request.headers.get("referer") or ""
        if "/verify" in target:
            return back(target)
    else:
        raise HTTPException(404, "ไม่รู้จักคำสั่งนี้")
    return back(f"/photos/{pid}")


@router.post("/rounds/{rid}/dept/{did}/summarize")
def dept_summarize(rid: int, did: int, request: Request, user=Depends(need("score")), db=Depends(get_db)):
    rnd, dept = db.get(Round, rid), db.get(Department, did)
    if rnd is None or dept is None:
        raise HTTPException(404, "ไม่พบรอบการตรวจหรือแผนกนี้")
    s = settings_store.load()
    photos = db.query(Photo).filter(Photo.round_id == rid, Photo.department_id == did, Photo.status == "done").all()
    if not photos:
        flash(request, "แผนกนี้ยังไม่มีภาพที่ให้คะแนนแล้ว", "err")
        return back(f"/rounds/{rid}/dept/{did}")
    st = scoring.dept_stats(photos, rnd.rubric or [], bool(s.get("after_replaces", True)))
    payload = dict(
        average_percent=st["avg"],
        criteria=[dict(code=c["code"], name=c["name"], avg_percent=st["crit"].get(c["code"])) for c in rnd.rubric or []],
        photos=[dict(area=p.area_name, percent=p.percent, summary=(p.analysis or {}).get("summary", ""),
                     findings=[f for c in (p.analysis or {}).get("criteria", []) for f in c.get("findings", [])][:8],
                     actions=(p.analysis or {}).get("top_actions", []))
                for p in sorted(photos, key=lambda x: x.percent or 0)[:40]])
    try:
        content, model = ai.summarize(payload, s, on_call=worker.count_call)
    except ai.AIError as e:
        flash(request, f"สรุปไม่สำเร็จ: {e}", "err")
        return back(f"/rounds/{rid}/dept/{did}")
    db.query(DeptSummary).filter(DeptSummary.round_id == rid, DeptSummary.department_id == did).delete()
    db.add(DeptSummary(round_id=rid, department_id=did, content=content, model=model))
    log(db, user, "summarize_dept", f"{rnd.name} / {dept.name}")
    db.commit()
    flash(request, f"สรุปคำแนะนำของ {dept.name} แล้ว")
    return back(f"/rounds/{rid}/dept/{did}")


# --------------------------------------------------------------------------- บันทึกการใช้งาน
@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, user=Depends(need("logs")), db=Depends(get_db)):
    items = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(300).all()
    return render(request, "admin/logs.html", user, db, items=items)

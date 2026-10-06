"""รหัสผ่าน การล็อกอิน และสิทธิ์การใช้งาน"""
import hashlib
import hmac
import os
import time

from fastapi import Depends, HTTPException, Request

from .db import User, get_db

_ITER = 200_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _ITER)
    return f"pbkdf2${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def password_problem(pw: str) -> str:
    if len(pw) < 8:
        return "รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร"
    if pw.isdigit() or pw.isalpha():
        return "รหัสผ่านต้องมีทั้งตัวอักษรและตัวเลข"
    return ""


# ---- กันเดารหัสผ่าน: ผิด 5 ครั้งใน 10 นาที พัก 10 นาที ----
_fails: dict = {}
_WINDOW, _LIMIT = 600, 5


def login_locked(key: str) -> bool:
    t = time.time()
    hits = [x for x in _fails.get(key, []) if t - x < _WINDOW]
    _fails[key] = hits
    return len(hits) >= _LIMIT


def login_failed(key: str):
    _fails.setdefault(key, []).append(time.time())


def login_ok(key: str):
    _fails.pop(key, None)


class NeedLogin(Exception):
    pass


class NeedPasswordChange(Exception):
    pass


def session_stamp(user: User) -> str:
    """เปลี่ยนรหัสผ่านเมื่อไร session เก่าทุกเครื่องหลุดทันที"""
    return user.password_hash[-12:]


def current_user(request: Request, db=Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active or request.session.get("stamp") != session_stamp(user):
        request.session.clear()
        raise NeedLogin()
    if user.must_change and not request.url.path.startswith(("/account/password", "/logout")):
        raise NeedPasswordChange()
    return user


# ---- สิทธิ์รายบัญชี: บทบาทให้ค่าตั้งต้น ผู้ดูแลเปิดหรือปิดทีละข้อให้แต่ละบัญชีได้
PERMS = {
    "upload_all": ("ส่งภาพแทนได้ทุกแผนก", "ไม่เปิด = ส่งได้เฉพาะแผนกของตัวเองและแผนกเพิ่มเติมที่กำหนด"),
    "view_all": ("ดูภาพและผลคะแนนของทุกแผนก", "ไม่เปิด = เป็นไปตามค่าตั้งของระบบ"),
    "rank_live": ("ดูอันดับขณะรอบยังเปิดอยู่", "ไม่เปิด = เป็นไปตามค่าตั้งของระบบ"),
    "export": ("ส่งออกข้อมูลและรายงาน", "Excel, CSV, JSON และรายงานรวมสำหรับพิมพ์"),
    "cameras_use": ("สั่งถ่ายจากกล้อง IP", "ใช้กล้องที่ผู้ดูแลลงทะเบียนไว้"),
    "score": ("ยืนยันผล ปรับคะแนน และสั่งวิเคราะห์ใหม่", "สำหรับหัวหน้าหรือกรรมการที่ตรวจหลักฐานภาพ ยืนยันภาพที่ตัวเองส่งไม่ได้"),
    "delete_any": ("ลบภาพของผู้อื่น", "ไม่เปิด = ลบได้เฉพาะภาพที่ตัวเองส่ง ขณะรอบยังเปิด"),
    "rounds": ("จัดการรอบการตรวจ", "สร้าง แก้ไข ปิด และเปิดรอบ"),
    "criteria": ("จัดการเกณฑ์การให้คะแนน", ""),
    "departments": ("จัดการแผนก", ""),
    "storage": ("สำรอง นำกลับ และลบข้อมูล", "รวมถึงการลบภาพเต็มและลบทั้งรอบ"),
    "logs": ("ดูบันทึกการใช้งาน", ""),
}
ROLE_PERMS = {
    "member": set(),
    "auditor": {"upload_all", "view_all", "rank_live", "export", "cameras_use"},
    "admin": set(PERMS),
}
MANAGE_PERMS = ("rounds", "criteria", "departments", "storage", "logs")


def can(user: User, perm: str) -> bool:
    if user is None:
        return False
    if user.role == "admin":
        return True
    override = (user.perms or {}).get(perm)
    if override is not None:
        return bool(override)
    return perm in ROLE_PERMS.get(user.role, set())


def manages(user: User) -> bool:
    return user is not None and (user.role == "admin" or any(can(user, p) for p in MANAGE_PERMS))


def dept_ids(user: User) -> set:
    ids = {int(x) for x in (user.extra_depts or []) if str(x).isdigit()}
    if user.department_id:
        ids.add(user.department_id)
    return ids


def need(perm: str):
    def dependency(user: User = Depends(current_user)) -> User:
        if not can(user, perm):
            raise HTTPException(403, f"บัญชีของคุณยังไม่ได้รับสิทธิ์ \"{PERMS[perm][0]}\" ติดต่อผู้ดูแลระบบ")
        return user
    return dependency


def manager_user(user: User = Depends(current_user)) -> User:
    if not manages(user):
        raise HTTPException(403, "หน้านี้สำหรับผู้ที่ได้รับสิทธิ์จัดการระบบ")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "หน้านี้สำหรับผู้ดูแลระบบเท่านั้น")
    return user

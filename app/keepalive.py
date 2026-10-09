"""กัน host ฟรีไม่ให้หลับขณะที่ยังมีงานค้าง

host ฟรีอย่าง Render ปิดบริการเมื่อไม่มีคำขอเข้ามา 15 นาที เธรดของคิววิเคราะห์จึงหยุดไปด้วย
ภาพที่ยังรอ AI อยู่จะไม่ถูกทำต่อจนกว่าจะมีคนเปิดเว็บอีกครั้ง (อาจเป็นหลายชั่วโมง)

ไฟล์นี้ให้ระบบเรียกที่อยู่สาธารณะของตัวเอง (/healthz) ทุก 10 นาที เฉพาะช่วงที่จำเป็น
- queue (ค่าเริ่มต้น): เฉพาะขณะที่คิววิเคราะห์ยังมีภาพค้าง คิวหมดแล้วปล่อยให้หลับตามปกติ
- hours: เพิ่มช่วงเวลาทำงานที่ตั้งไว้ด้วย (เช่น 07:00-18:00) เหมาะกับระบบที่ให้กล้องถ่ายตามตารางเวลา
- off: ไม่เรียก

/healthz ไม่แตะฐานข้อมูล การเรียกนี้จึงไม่ปลุกฐานข้อมูลบนคลาวด์
ที่อยู่ที่ใช้: ตัวแปร RENDER_EXTERNAL_URL (Render ใส่ให้เอง) ถ้าไม่มีจึงใช้ ที่อยู่เว็บของระบบ ในหน้าตั้งค่า
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("fives.keepalive")

EVERY = 600.0                    # host ฟรีส่วนใหญ่หลับที่ 15 นาที: เรียกทุก 10 นาทีเผื่อพลาดได้หนึ่งครั้ง
CONFIG_GIVE_UP = 2 * 3600.0      # คิวหยุดเพราะการตั้งค่าผิดนานเกินนี้: เลิกกันหลับ (ผู้ดูแลได้รับการแจ้งเตือนไปแล้ว)
_transport = None                # ชุดทดสอบใส่ transport จำลอง
_state = {"at": None, "ok": None, "detail": "", "last": 0.0, "count": 0}


def target(s: dict) -> str:
    url = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or s.get("public_url") or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


def in_hours(spec: str, at: datetime = None) -> bool:
    """spec แบบ 07:00-18:00 (เวลาไทย) ช่วงข้ามเที่ยงคืนก็ได้ เช่น 22:00-06:00"""
    try:
        a, b = [x.strip() for x in (spec or "").split("-", 1)]
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
    except (ValueError, AttributeError):
        return False
    t = (at or datetime.now(timezone.utc)) + timedelta(hours=7)
    cur, start, end = t.hour * 60 + t.minute, ah * 60 + am, bh * 60 + bm
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def clean_hours(spec: str) -> str:
    """คืนช่วงเวลาในรูป HH:MM-HH:MM หรือข้อความว่างถ้ารูปแบบไม่ถูก"""
    try:
        a, b = [x.strip() for x in (spec or "").split("-", 1)]
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
        if not (0 <= ah < 24 and 0 <= bh < 24 and 0 <= am < 60 and 0 <= bm < 60) or (ah, am) == (bh, bm):
            return ""
        return f"{ah:02d}:{am:02d}-{bh:02d}:{bm:02d}"
    except (ValueError, AttributeError):
        return ""


def wanted(s: dict, busy: bool, paused_kind: str = "", paused_since=None) -> str:
    """คืนเหตุผลที่ควรกันหลับตอนนี้ หรือข้อความว่างถ้าไม่ต้อง"""
    mode = s.get("keep_awake", "queue")
    if mode == "off":
        return ""
    if mode == "hours" and in_hours(s.get("keep_awake_hours", "")):
        return "อยู่ในช่วงเวลาที่ตั้งให้ระบบตื่น"
    if busy:
        if paused_kind == "config" and paused_since is not None:
            waited = (datetime.now(timezone.utc).replace(tzinfo=None) - paused_since).total_seconds()
            if waited > CONFIG_GIVE_UP:
                return ""
        return "คิววิเคราะห์ยังมีภาพค้าง"
    return ""


def ping(url: str) -> bool:
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), transport=_transport, follow_redirects=True) as c:
            r = c.get(url + "/healthz", headers={"user-agent": "fives-vision-keepalive"})
        ok, detail = r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        ok, detail = False, type(e).__name__
    _state.update(at=datetime.now(timezone.utc).replace(tzinfo=None), ok=ok, detail=detail, count=_state["count"] + 1)
    if not ok:
        log.warning("keep-awake call to %s failed: %s", url, detail)
    return ok


def tick(s: dict, busy: bool, paused_kind: str = "", paused_since=None) -> bool:
    """เรียกจากผู้เฝ้าคิวทุกไม่กี่วินาที: ถึงรอบและมีเหตุให้ตื่นจึงเรียกจริง คืน True ถ้าได้เรียก"""
    url = target(s)
    if not url or not wanted(s, busy, paused_kind, paused_since):
        return False
    if time.time() - _state["last"] < EVERY:
        return False
    _state["last"] = time.time()
    ping(url)
    return True


def status(s: dict) -> dict:
    url = target(s)
    return dict(mode=s.get("keep_awake", "queue"), hours=s.get("keep_awake_hours", ""), url=url,
                source="host" if os.getenv("RENDER_EXTERNAL_URL") else ("setting" if url else ""),
                at=_state["at"], ok=_state["ok"], detail=_state["detail"], count=_state["count"])

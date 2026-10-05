"""กล้อง IP: ดึงภาพนิ่งจากกล้อง และสถานะของโปรแกรมกล้อง (agent) บนเครื่องในโรงงาน

direct  เซิร์ฟเวอร์ดึงภาพจากกล้องเอง ใช้ได้เมื่อเซิร์ฟเวอร์อยู่ในเครือข่ายเดียวกับกล้อง (รันระบบบนเครื่องในโรงงาน)
agent   ระบบอยู่บน cloud ซึ่งมองไม่เห็นกล้องในโรงงาน จึงให้ tools/camera_agent.py บนเครื่องในโรงงาน
        ดึงภาพแล้วส่งขึ้นมาแทน
"""
import hmac
import time

import httpx

from .db import Camera

_transport = None            # ชุดทดสอบใส่ transport จำลอง
MAX_FRAME = 12 * 1024 * 1024


class CameraError(Exception):
    pass


def _first_jpeg(resp: httpx.Response) -> bytes:
    """กล้องบางรุ่นให้ลิงก์เป็นสตรีม MJPEG: อ่านมาจนได้ภาพแรกแล้วหยุด"""
    buf = b""
    for chunk in resp.iter_bytes(65536):
        buf += chunk
        a = buf.find(b"\xff\xd8")
        b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
        if a >= 0 and b > a:
            return buf[a:b + 2]
        if len(buf) > MAX_FRAME:
            break
    raise CameraError("อ่านภาพจากสตรีมของกล้องไม่ได้")


def _snapshot(cam: Camera) -> bytes:
    if not (cam.url or "").lower().startswith(("http://", "https://")):
        raise CameraError("ลิงก์ภาพนิ่งต้องขึ้นต้นด้วย http:// หรือ https://")
    auth = None
    if cam.username:
        auth = (httpx.DigestAuth if cam.auth == "digest" else httpx.BasicAuth)(cam.username, cam.password or "")
    try:
        # กล้องส่วนใหญ่ใช้ใบรับรองที่ออกเอง จึงไม่ตรวจใบรับรองของกล้อง
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0), verify=False, transport=_transport,
                          follow_redirects=True) as c:
            with c.stream("GET", cam.url, auth=auth) as r:
                if r.status_code in (401, 403):
                    raise CameraError("กล้องปฏิเสธชื่อผู้ใช้หรือรหัสผ่าน ลองสลับชนิดการยืนยันตัวตน basic กับ digest")
                if r.status_code != 200:
                    raise CameraError(f"กล้องตอบกลับ {r.status_code} ตรวจลิงก์ภาพนิ่งของกล้อง")
                kind = r.headers.get("content-type", "").lower()
                if "multipart" in kind:
                    return _first_jpeg(r)
                data = r.read()
    except httpx.TimeoutException:
        raise CameraError("กล้องไม่ตอบภายในเวลา ตรวจว่าเซิร์ฟเวอร์อยู่ในเครือข่ายเดียวกับกล้อง")
    except httpx.HTTPError as e:
        raise CameraError(f"เชื่อมต่อกล้องไม่ได้ ({type(e).__name__}) ตรวจ IP และเครือข่าย")
    if len(data) > MAX_FRAME or len(data) < 500:
        raise CameraError("ข้อมูลที่ได้จากกล้องไม่ใช่ภาพนิ่ง")
    return data


def _rtsp(cam: Camera) -> bytes:
    try:
        import cv2
    except ImportError:
        raise CameraError("กล้องแบบ RTSP ต้องติดตั้งแพ็กเกจเพิ่ม: pip install opencv-python-headless "
                          "หรือเปลี่ยนไปใช้ลิงก์ภาพนิ่ง (HTTP snapshot) ของกล้อง")
    url = cam.url or ""
    if cam.username and "@" not in url and "://" in url:
        from urllib.parse import quote
        scheme, rest = url.split("://", 1)
        url = f"{scheme}://{quote(cam.username, safe='')}:{quote(cam.password or '', safe='')}@{rest}"
    cap = cv2.VideoCapture(url)
    try:
        if not cap.isOpened():
            raise CameraError("เปิดสตรีม RTSP ไม่ได้ ตรวจลิงก์ ชื่อผู้ใช้ และรหัสผ่าน")
        frame = None
        for _ in range(8):                 # ทิ้งเฟรมแรก ๆ ที่มักเป็นภาพค้างหรือภาพเสีย
            ok, f = cap.read()
            if ok:
                frame = f
        if frame is None:
            raise CameraError("อ่านภาพจากสตรีม RTSP ไม่ได้")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            raise CameraError("แปลงภาพจากกล้องไม่สำเร็จ")
        return buf.tobytes()
    finally:
        cap.release()


def grab(cam: Camera) -> bytes:
    return _rtsp(cam) if cam.source == "rtsp" else _snapshot(cam)


# --------------------------------------------------------------------------- agent (อยู่ในหน่วยความจำ ไม่แตะฐานข้อมูล)
agent = {"seen": 0.0, "requests": {}, "cache": None, "last_error": ""}


def agent_online() -> bool:
    return time.time() - agent["seen"] < 150


def token_ok(given: str, real: str) -> bool:
    return bool(real) and bool(given) and hmac.compare_digest(given.encode(), real.encode())


def invalidate():
    agent["cache"] = None

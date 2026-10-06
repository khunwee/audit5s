#!/usr/bin/env python3
"""5ส Vision camera agent — โปรแกรมกล้องสำหรับเครื่องในโรงงาน

ใช้เมื่อระบบอยู่บน cloud (เช่น Render) ซึ่งมองไม่เห็นกล้อง IP ในเครือข่ายโรงงาน
โปรแกรมนี้รันบนคอมพิวเตอร์ในโรงงาน ดึงภาพนิ่งจากกล้องที่ผู้ดูแลลงทะเบียนไว้ในหน้า จัดการระบบ > กล้อง
แล้วส่งขึ้นระบบ ภาพจะเข้ารอบการตรวจที่เปิดอยู่ และถูก AI ให้คะแนนเหมือนภาพจากมือถือ

ต้องการแค่ Python 3.8+ ไม่ต้องติดตั้งแพ็กเกจเพิ่ม (กล้องแบบ RTSP ต้อง: pip install opencv-python-headless)

ตัวอย่าง
  ถ่ายตามตารางเวลาที่ตั้งในหน้า จัดการ > กล้อง (แนะนำ: แยกตารางได้รายกล้องและรายแผนก แก้ตารางที่หน้าเว็บได้เลย)
      python camera_agent.py --server https://xxx.onrender.com --token รหัส
  ถ่ายทุกกล้องตอนนี้ 1 ครั้งแล้วจบ (ใช้ทดสอบ หรือสั่งจาก Task Scheduler ของ Windows)
      python camera_agent.py --server https://xxx.onrender.com --token รหัส --once
  ถ่ายทุกวันตามเวลา
      python camera_agent.py --server https://xxx.onrender.com --token รหัส --times 09:30,14:30
  สุ่มเวลาวันละ 2 ครั้งในช่วงเวลางาน
      python camera_agent.py --server https://xxx.onrender.com --token รหัส --random 2 --between 08:30-16:30
  รับคำสั่งถ่ายจากปุ่มในหน้าเว็บด้วย (ถามระบบทุก 20 วินาทีเฉพาะช่วงเวลาที่กำหนด)
      python camera_agent.py --server https://xxx.onrender.com --token รหัส --times 09:30 --listen 08:00-17:00

หมายเหตุสำหรับ host ฟรี: โหมด --listen ทำให้เซิร์ฟเวอร์ตื่นตลอดช่วงเวลานั้น ซึ่งใช้ชั่วโมงฟรีรายเดือนของ host
ถ้าใน workspace เดียวกันมีหลายบริการ ให้ใช้เฉพาะ --times หรือ --random
"""
import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

VERSION = "1.5.0"
_NO_VERIFY = ssl.create_default_context()
_NO_VERIFY.check_hostname = False
_NO_VERIFY.verify_mode = ssl.CERT_NONE      # กล้อง IP ส่วนใหญ่ใช้ใบรับรองที่ออกเอง


def say(msg: str):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


class Api:
    def __init__(self, server: str, token: str):
        self.base = server.rstrip("/")
        self.headers = {"X-Agent-Token": token, "User-Agent": f"5s-camera-agent/{VERSION}"}

    def _open(self, req, timeout=90):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"ระบบตอบกลับ {e.code}: {detail}")
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"ติดต่อระบบไม่ได้: {getattr(e, 'reason', e)}")

    def poll(self) -> dict:
        return self._open(urllib.request.Request(self.base + "/api/agent/poll", headers=self.headers), timeout=90)

    def upload(self, camera_id: int, jpeg: bytes = None, error: str = "", scheduled: str = "") -> dict:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="camera_id"\r\n\r\n{camera_id}\r\n'.encode()]
        if scheduled:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="scheduled"\r\n\r\n{scheduled}\r\n'.encode())
        if error:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="error"\r\n\r\n'.encode()
                         + error.encode("utf-8") + b"\r\n")
        if jpeg:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="camera.jpg"\r\n'
                         f'Content-Type: image/jpeg\r\n\r\n'.encode() + jpeg + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        headers = dict(self.headers, **{"Content-Type": f"multipart/form-data; boundary={boundary}"})
        req = urllib.request.Request(self.base + "/api/agent/upload", data=b"".join(parts), headers=headers, method="POST")
        return self._open(req, timeout=120)


def http_snapshot(cam: dict) -> bytes:
    handlers = [urllib.request.HTTPSHandler(context=_NO_VERIFY)]
    if cam.get("username"):
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, cam["url"], cam["username"], cam.get("password") or "")
        cls = urllib.request.HTTPDigestAuthHandler if cam.get("auth") == "digest" else urllib.request.HTTPBasicAuthHandler
        handlers.append(cls(mgr))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(cam["url"], timeout=15) as r:
            kind = (r.headers.get("Content-Type") or "").lower()
            if "multipart" in kind:                       # สตรีม MJPEG: อ่านจนได้ภาพแรก
                buf = b""
                while len(buf) < 12 * 1024 * 1024:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    a = buf.find(b"\xff\xd8")
                    b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                    if a >= 0 and b > a:
                        return buf[a:b + 2]
                raise RuntimeError("อ่านภาพจากสตรีมของกล้องไม่ได้")
            data = r.read(12 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("กล้องปฏิเสธชื่อผู้ใช้หรือรหัสผ่าน ลองสลับ basic กับ digest ในหน้าตั้งค่ากล้อง")
        raise RuntimeError(f"กล้องตอบกลับ {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"เชื่อมต่อกล้องไม่ได้: {getattr(e, 'reason', e)}")
    if len(data) < 500:
        raise RuntimeError("ข้อมูลที่ได้จากกล้องไม่ใช่ภาพนิ่ง")
    return data


def rtsp_snapshot(cam: dict) -> bytes:
    try:
        import cv2
    except ImportError:
        raise RuntimeError("กล้อง RTSP ต้องติดตั้ง: pip install opencv-python-headless")
    url = cam["url"]
    if cam.get("username") and "@" not in url and "://" in url:
        from urllib.parse import quote
        scheme, rest = url.split("://", 1)
        url = f"{scheme}://{quote(cam['username'], safe='')}:{quote(cam.get('password') or '', safe='')}@{rest}"
    cap = cv2.VideoCapture(url)
    try:
        if not cap.isOpened():
            raise RuntimeError("เปิดสตรีม RTSP ไม่ได้")
        frame = None
        for _ in range(8):
            ok, f = cap.read()
            if ok:
                frame = f
        if frame is None:
            raise RuntimeError("อ่านภาพจากสตรีม RTSP ไม่ได้")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            raise RuntimeError("แปลงภาพจากกล้องไม่สำเร็จ")
        return buf.tobytes()
    finally:
        cap.release()


def grab(cam: dict) -> bytes:
    return rtsp_snapshot(cam) if cam.get("source") == "rtsp" else http_snapshot(cam)


def capture(api, cam: dict, scheduled: str = "") -> bool:
    try:
        jpeg = grab(cam)
    except Exception as e:
        say(f"  {cam['name']}: ถ่ายไม่สำเร็จ ({e})")
        try:
            api.upload(cam["id"], error=str(e))
        except RuntimeError:
            pass
        return False
    try:
        res = api.upload(cam["id"], jpeg=jpeg, scheduled=scheduled)
        say(f"  {cam['name']}: ส่งแล้ว {len(jpeg) // 1024} KB เป็นภาพเลขที่ {res.get('id')}")
        return True
    except RuntimeError as e:
        say(f"  {cam['name']}: ส่งขึ้นระบบไม่สำเร็จ ({e})")
        return False


def capture_all(api: Api, data: dict) -> int:
    if not data.get("open_round"):
        say("ไม่มีรอบการตรวจที่เปิดรับภาพ ข้ามการถ่ายรอบนี้")
        return 0
    cams = data.get("cameras") or []
    if not cams:
        say("ยังไม่มีกล้องที่ตั้งให้ถ่ายผ่านโปรแกรมนี้ (หน้า จัดการระบบ > กล้อง)")
        return 0
    say(f"ถ่าย {len(cams)} กล้อง")
    return sum(1 for cam in cams if capture(api, cam))


def parse_times(text: str) -> list:
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if part:
            h, m = part.split(":")
            out.append((int(h), int(m)))
    return out


def parse_range(text: str) -> tuple:
    a, b = text.split("-")
    (h1, m1), (h2, m2) = parse_times(a)[0], parse_times(b)[0]
    return h1 * 60 + m1, h2 * 60 + m2


def plan_for(day: datetime, fixed: list, n_random: int, window: tuple) -> list:
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    times = [base + timedelta(hours=h, minutes=m) for h, m in fixed]
    if n_random > 0:
        lo, hi = window
        times += [base + timedelta(minutes=x) for x in random.sample(range(lo, max(hi, lo + n_random)), n_random)]
    return sorted(times)


# --------------------------------------------------------------------------- โหมดตามตารางของระบบ
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_state.json")


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def load_done(day: str) -> set:
    """ครั้งที่ถ่ายไปแล้วของวันนี้ เก็บในไฟล์ข้างโปรแกรม ปิดแล้วเปิดใหม่จึงไม่ถ่ายซ้ำ"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {(int(c), t) for c, t in data.get("done", [])} if data.get("date") == day else set()
    except (OSError, ValueError, TypeError):
        return set()


def save_done(day: str, done: set):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": day, "done": sorted(done)}, f)
    except OSError:
        pass


def auto_due(data: dict, hhmm: str, done: set) -> list:
    """(กล้อง, เวลาตามตาราง, ยังอยู่ในช่วงที่ถ่ายชดเชยได้หรือไม่) ของครั้งที่ถึงเวลาแล้วและยังไม่ได้ทำ"""
    out, grace = [], int(data.get("grace") or 20)
    for cam in data.get("cameras") or []:
        for t in (data.get("plan") or {}).get(str(cam["id"]), []):
            if (cam["id"], t) not in done and _minutes(t) <= _minutes(hhmm):
                out.append((cam, t, _minutes(hhmm) - _minutes(t) <= grace))
    return out


def server_clock(data: dict, local: datetime) -> timedelta:
    """ส่วนต่างระหว่างเวลาไทยของระบบกับนาฬิกาของเครื่องนี้ (เครื่องที่ตั้งเขตเวลาผิดยังถ่ายตรงเวลา)"""
    try:
        there = datetime.strptime(f"{data['date']} {data['now']}", "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return timedelta(0)
    diff = there - local.replace(second=0, microsecond=0)
    return diff if abs(diff) > timedelta(minutes=3) else timedelta(0)


def auto_step(api, state: dict, local: datetime = None) -> list:
    """หนึ่งรอบของโหมดตามตาราง คืน [(ชื่อกล้อง, เวลา, ผล)] ผล = sent | failed | late | closed"""
    local = local or datetime.now()
    sync_every = state.get("sync_minutes", 60) * 60
    stale = state.get("data") is None or time.time() - state.get("synced", 0) >= sync_every
    now = local + state.get("offset", timedelta(0))
    if stale or state.get("day") != now.strftime("%Y-%m-%d"):
        data = api.poll()
        state.update(data=data, synced=time.time(), offset=server_clock(data, local))
        now = local + state["offset"]
        day = data.get("date") or now.strftime("%Y-%m-%d")
        if state.get("day") != day:
            state.update(day=day, done=load_done(day))
            plan = data.get("plan") or {}
            total = sum(len(v) for v in plan.values())
            say(f"ตารางของวันที่ {day}: {len(data.get('cameras') or [])} กล้อง รวม {total} ครั้ง"
                + (f" (นาฬิกาเครื่องนี้ต่างจากเวลาไทย {int(state['offset'].total_seconds() // 60)} นาที ใช้เวลาของระบบ)" if state["offset"] else ""))
            for cam in data.get("cameras") or []:
                times = plan.get(str(cam["id"]), [])
                say(f"  {cam.get('department', '')} / {cam['name']}: " + (", ".join(times) if times else "ไม่มีการถ่ายวันนี้"))
    hhmm, results = now.strftime("%H:%M"), []
    if not auto_due(state["data"], hhmm, state["done"]):
        return results
    data = api.poll()                                   # ก่อนถ่าย: รับการตั้งค่ากล้องและสถานะรอบล่าสุด
    state.update(data=data, synced=time.time())
    for cam, t, fresh in auto_due(data, hhmm, state["done"]):
        state["done"].add((cam["id"], t))
        if not fresh:
            say(f"  {cam['name']}: เลยเวลา {t} มานานเกินไป ข้ามครั้งนี้")
            results.append((cam["name"], t, "late"))
        elif not data.get("open_round"):
            say(f"  {cam['name']}: ถึงเวลา {t} แต่ไม่มีรอบการตรวจที่เปิดรับภาพ")
            results.append((cam["name"], t, "closed"))
        else:
            results.append((cam["name"], t, "sent" if capture(api, cam, scheduled=t) else "failed"))
    save_done(state["day"], state["done"])
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="5ส Vision camera agent")
    ap.add_argument("--server", required=True, help="ที่อยู่เว็บของระบบ เช่น https://xxx.onrender.com")
    ap.add_argument("--token", required=True, help="รหัสของโปรแกรมกล้อง จากหน้า จัดการระบบ > กล้อง")
    ap.add_argument("--once", action="store_true", help="ถ่ายทุกกล้องตอนนี้ 1 ครั้งแล้วจบ")
    ap.add_argument("--times", default="", help="เวลาถ่ายประจำวัน เช่น 09:30,14:30")
    ap.add_argument("--random", type=int, default=0, help="จำนวนครั้งที่สุ่มเวลาถ่ายต่อวัน")
    ap.add_argument("--between", default="08:30-16:30", help="ช่วงเวลาที่ใช้สุ่ม")
    ap.add_argument("--listen", default="", help="ช่วงเวลาที่รับคำสั่งถ่ายจากหน้าเว็บ เช่น 08:00-17:00")
    ap.add_argument("--poll-seconds", type=int, default=20, help="ความถี่ที่ถามระบบในช่วง --listen")
    ap.add_argument("--sync-minutes", type=int, default=60,
                    help="โหมดตามตาราง: รับตารางและรายชื่อกล้องใหม่จากระบบทุกกี่นาที (ก่อนถ่ายทุกครั้งจะรับใหม่อยู่แล้ว)")
    args = ap.parse_args(argv)
    api = Api(args.server, args.token)

    if args.once:
        try:
            return 0 if capture_all(api, api.poll()) > 0 else 1
        except RuntimeError as e:
            say(str(e))
            return 2

    try:
        fixed, window = parse_times(args.times), parse_range(args.between)
        listen = parse_range(args.listen) if args.listen else None
    except (ValueError, IndexError):
        say("รูปแบบเวลาไม่ถูกต้อง ใช้ ชั่วโมง:นาที เช่น 09:30 และช่วงเวลาเช่น 08:00-17:00")
        return 2
    say(f"camera agent {VERSION} เริ่มทำงาน เชื่อมกับ {api.base}")
    if not fixed and not args.random:
        # ไม่ได้ระบุเวลาในคำสั่ง: ถ่ายตามตารางรายกล้องที่ตั้งในหน้า จัดการ > กล้อง
        say("โหมดตามตารางของระบบ: ตั้งและแก้ตารางได้ที่หน้า จัดการ > กล้อง")
        state = {"sync_minutes": max(5, args.sync_minutes)}
        while True:
            now = datetime.now()
            try:
                auto_step(api, state, now)
                minute = now.hour * 60 + now.minute
                if listen and listen[0] <= minute < listen[1]:
                    data = api.poll()
                    wanted = set(data.get("requests") or [])
                    for cam in data.get("cameras") or []:
                        if cam["id"] in wanted:
                            say(f"ได้รับคำสั่งถ่ายจากหน้าเว็บ: {cam['name']}")
                            capture(api, cam)
            except RuntimeError as e:
                say(str(e))
            except KeyboardInterrupt:
                return 0
            in_listen = listen and listen[0] <= (now.hour * 60 + now.minute) < listen[1]
            try:
                time.sleep(max(5, args.poll_seconds) if in_listen else 20)
            except KeyboardInterrupt:
                return 0
    day, plan = None, []
    while True:
        now = datetime.now()
        if day != now.date():
            day = now.date()
            plan = [t for t in plan_for(now, fixed, args.random, window) if t > now - timedelta(minutes=1)]
            if plan:
                say("เวลาถ่ายของวันนี้: " + ", ".join(t.strftime("%H:%M") for t in plan))
        try:
            due = [t for t in plan if t <= now]
            if due:
                plan = [t for t in plan if t > now]
                capture_all(api, api.poll())
            minute = now.hour * 60 + now.minute
            if listen and listen[0] <= minute < listen[1]:
                data = api.poll()
                wanted = set(data.get("requests") or [])
                for cam in data.get("cameras") or []:
                    if cam["id"] in wanted:
                        say(f"ได้รับคำสั่งถ่ายจากหน้าเว็บ: {cam['name']}")
                        capture(api, cam)
        except RuntimeError as e:
            say(str(e))
        except KeyboardInterrupt:
            return 0
        in_listen = listen and listen[0] <= (now.hour * 60 + now.minute) < listen[1]
        try:
            time.sleep(max(5, args.poll_seconds) if in_listen else 30)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())

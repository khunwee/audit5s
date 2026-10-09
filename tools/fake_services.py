"""บริการจำลองสำหรับตรวจโปรแกรมแบบใช้งานจริง ผ่าน HTTP จริง
- ผู้ให้บริการ AI สามเจ้า: Gemini (/v1beta/...), OpenAI-compatible ตัวที่สอง (/two/v1), ตัวที่สาม (/three/v1)
- กล้อง IP (/cam/snap.jpg) และปลายทางแจ้งเตือน (/hook)
สั่งพฤติกรรมด้วย POST /control {"ai1": "ok|429day|429min|503|401|slow", ...}
"""
import io
import json
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

ST = {"ai1": "ok", "ai2": "ok", "ai3": "ok", "latency": 0.25, "ng": {"major": [], "minor": []}, "image_ok": True}
CALLS = {"ai1": 0, "ai2": 0, "ai3": 0, "cam": 0, "models": 0}
HOOKS = []
L = threading.Lock()

Q429 = {"429day": ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "12310s"),
        "429min": ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "4s")}


def answer(prompt: str) -> str:
    if "รายการตรวจ (แต่ละข้อคือสภาพที่ถูกต้อง)" in prompt:
        codes = re.search(r"ต้องมีครบทุกข้อตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        checks = []
        for c in codes:
            c = c.strip()
            st = "major" if c in ST["ng"]["major"] else "minor" if c in ST["ng"]["minor"] else "ok"
            checks.append(dict(code=c, status=st, evidence=f"เห็นสิ่งของวางผิดที่ มุมซ้ายล่าง ({c})" if st != "ok" else "เรียบร้อย",
                               action=f"จัดเก็บให้เข้าที่ ({c})" if st != "ok" else "", box=[600, 100, 900, 450] if st != "ok" else None))
        out = dict(image_ok=ST["image_ok"], image_issue="" if ST["image_ok"] else "ภาพมืดเกินไป", scene="โต๊ะทำงานในสำนักงาน",
                   checks=checks, summary="โดยรวมเรียบร้อย", top_actions=["จัดเก็บของบนพื้น"])
    elif "ต้องมีครบทุกเกณฑ์ตามลำดับนี้" in prompt:
        codes = re.search(r"ต้องมีครบทุกเกณฑ์ตามลำดับนี้: (.+)", prompt).group(1).split(", ")
        out = dict(image_ok=ST["image_ok"], image_issue="" if ST["image_ok"] else "ภาพมืดเกินไป", scene="-", summary="สรุป", top_actions=[],
                   criteria=[dict(code=c.strip(), na=False, level=3, reason="เหตุผล", findings=["พบกล่องบนพื้น"], recommendations=["จัดเก็บ"])
                             for c in codes])
    else:
        out = dict(overview="ภาพรวมของแผนกอยู่ในเกณฑ์ดี", strengths=["โต๊ะสะอาด"],
                   priorities=[dict(issue="ของวางบนพื้น", action="ทำที่วางกระเป๋า", where="โต๊ะทำงาน")])
    return json.dumps(out, ensure_ascii=False)


def snapshot() -> bytes:
    rnd = random.Random(time.time())
    im = Image.new("RGB", (1280, 720), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    for _ in range(30):
        x, y = rnd.randrange(1200), rnd.randrange(650)
        im.paste((rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)), (x, y, x + 70, y + 50))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:                  # ผู้เรียกปิดการเชื่อมต่อไปก่อน (เช่น ครบเวลารอ หรือโปรแกรมที่ตรวจถูกปิด)
            pass

    def do_GET(self):
        if self.path.startswith("/stats"):
            with L:
                return self.send(200, {"calls": dict(CALLS), "hooks": list(HOOKS), "state": {k: v for k, v in ST.items()}})
        if self.path.startswith("/cam/"):
            with L:
                CALLS["cam"] += 1
            return self.send(200, snapshot(), "image/jpeg")
        with L:
            CALLS["models"] += 1
        if "/v1beta/models" in self.path:
            return self.send(200, {"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                                              {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]}]})
        if "/three/" in self.path:      # แบบ Mistral: บอกความสามารถรับภาพใน capabilities
            return self.send(200, {"data": [{"id": "mistral-small-latest", "capabilities": {"vision": True, "completion_chat": True}},
                                            {"id": "codestral-latest", "capabilities": {"vision": False, "completion_chat": True}}]})
        return self.send(200, {"data": [{"id": "qwen/qwen3.8-27b"}, {"id": "whisper-large-v3"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if self.path == "/control":
            with L:
                ST.update(body)
            return self.send(200, {"ok": True})
        if self.path.startswith("/hook"):
            with L:
                HOOKS.append(dict(path=self.path, body=body, secret=self.headers.get("X-5S-Secret", "")))
            return self.send(200, {"ok": True})
        gem = "generateContent" in self.path
        slot = "ai1" if gem else ("ai3" if "/three/" in self.path else "ai2")
        if gem:
            parts = body["contents"][0]["parts"]
            prompt = parts[-1]["text"]
        else:
            content = body["messages"][1]["content"]
            prompt = content[0]["text"] if isinstance(content, list) else content
        with L:
            CALLS[slot] += 1
            mode = ST[slot]
        if mode in Q429:
            qid, delay = Q429[mode]
            return self.send(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                             "message": "You exceeded your current quota, please check your plan and billing details.",
                                             "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": qid}]},
                                                         {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})
        if mode == "503":
            return self.send(503, {"error": {"message": "The model is overloaded. Please try again later."}})
        if mode == "401":
            return self.send(401, {"error": {"message": "Incorrect API key provided."}})
        time.sleep(ST["latency"] * (8 if mode == "slow" else 1))
        text = answer(prompt)
        if gem:
            return self.send(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})
        self.send(200, {"choices": [{"message": {"content": text}}]})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()

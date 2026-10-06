"""ข้อมูลของจอแสดงผล (TV): อันดับของรอบปัจจุบัน ประวัติคะแนนและอันดับย้อนหลัง คะแนนรายหมวด ข้อที่ไม่ผ่านบ่อย และงานแก้ไข

ผลถูกเก็บไว้ในหน่วยความจำและคำนวณใหม่เฉพาะเมื่อข้อมูลในฐานข้อมูลเปลี่ยน (db.DATA_VERSION) หรือขึ้นวันใหม่
จอที่เปิดทิ้งไว้และถามข้อมูลทุกไม่กี่นาทีจึงไม่ทำให้ฐานข้อมูลบน cloud ฟรีต้องตื่นตลอดเวลา
"""
import re
import threading
from datetime import timedelta

from . import config, scoring, settings_store
from .db import DATA_VERSION, Department, Round, now
from .web import band

SLIDES = {"ranking": "อันดับของรอบปัจจุบัน", "trend": "แนวโน้มคะแนนของแต่ละแผนก", "ranks": "ประวัติอันดับ",
          "history": "ตารางคะแนนย้อนหลัง", "category": "คะแนนรายหมวดและข้อที่ไม่ผ่านบ่อย", "actions": "งานแก้ไข"}
_cache = {}
_lock = threading.Lock()
stats = {"computed": 0, "served": 0}


def english_name(name: str) -> str:
    """ชื่อหมวดที่เขียนแบบ 'สะสาง (Seiri)': ใช้ส่วนในวงเล็บเป็นชื่อภาษาอังกฤษ"""
    m = re.search(r"\(([A-Za-z][A-Za-z0-9 /&.-]*)\)\s*$", name or "")
    return m.group(1).strip() if m else ""


def pick_round(db, s):
    """รอบที่จะขึ้นจอ: auto เคารพการตั้งค่าว่าอันดับเปิดให้ดูระหว่างรอบหรือหลังปิดรอบ"""
    latest_open = db.query(Round).filter(Round.status == "open").order_by(Round.id.desc()).first()
    latest_closed = db.query(Round).filter(Round.status == "closed").order_by(Round.id.desc()).first()
    mode = s.get("tv_round", "auto")
    if mode == "open":
        return latest_open or latest_closed
    if mode == "closed":
        return latest_closed
    if s.get("ranking_visibility", "always") == "always":
        return latest_open or latest_closed
    return latest_closed


def _d(v):
    return v.strftime("%d/%m/%Y") if v else ""


def compute(db, s, n_rounds: int) -> dict:
    stats["computed"] += 1
    stamp = (now() + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")
    base = dict(ok=True, version=config.APP_VERSION, generated=stamp, org=s.get("org_name", ""),
                bands=dict(good=s.get("band_good", 80), mid=s.get("band_mid", 60)))
    cur = pick_round(db, s)
    if cur is None:
        return dict(base, round=None, rows=[], unranked=[], history=dict(rounds=[], series=[]), cats=[], pareto=[],
                    actions=dict(open=0, overdue=0), summary=dict(good=0, mid=0, low=0, avg=None, photos=0))
    rounds = list(reversed(db.query(Round).filter(Round.id <= cur.id, Round.status != "planned")
                           .order_by(Round.id.desc()).limit(n_rounds).all()))
    en = {d.id: (d.name_en or "") for d in db.query(Department).all()}
    per, names = [], {}
    for r in rounds:
        rk = scoring.round_ranking(db, r, with_prev=False)
        table = {}
        for row in rk["ranked"] + rk["unranked"]:
            names[row["dept"].id] = row["dept"]
            table[row["dept"].id] = dict(avg=row["avg"], rank=row["rank"], scored=row["scored"])
        per.append(table)
    dash = scoring.dashboard(db, cur)
    rk = dash["ranking"]
    prev = per[-2] if len(per) > 1 else {}
    rows = []
    for r in rk["ranked"]:
        d, before = r["dept"], prev.get(r["dept"].id) or {}
        ac = dash["act_by_dept"].get(d.id, {"open": 0, "overdue": 0})
        rows.append(dict(id=d.id, code=d.code, name=d.name, name_en=en.get(d.id, ""), rank=r["rank"], avg=r["avg"],
                         band=band(r["avg"]), scored=r["scored"], unverified=r["unverified"],
                         delta=round(r["avg"] - before["avg"], 1) if before.get("avg") is not None else None,
                         rank_delta=(before["rank"] - r["rank"]) if before.get("rank") else None,
                         open=ac["open"], overdue=ac["overdue"]))
    unranked = [dict(name=r["dept"].name, name_en=en.get(r["dept"].id, ""), scored=r["scored"], need=cur.min_photos,
                     missing=len(r["missing"])) for r in rk["unranked"] + rk["idle"]]
    current = {r["id"] for r in rows}
    order = [r["id"] for r in rows] + sorted((i for i in names if i not in current), key=lambda i: names[i].name)
    series = [dict(id=i, name=names[i].name, name_en=en.get(i, ""),
                   avg=[(t.get(i) or {}).get("avg") for t in per], rank=[(t.get(i) or {}).get("rank") for t in per])
              for i in order if any((t.get(i) or {}).get("avg") is not None for t in per)]
    text_en = {k["code"]: k.get("text_en", "") for k in (cur.checklist or [])}
    avgs = [r["avg"] for r in rows]
    return dict(
        base,
        round=dict(id=cur.id, name=cur.name, status=cur.status, start=_d(cur.start_date), end=_d(cur.end_date),
                   mode=cur.mode or "level", min_photos=cur.min_photos),
        rows=rows, unranked=unranked,
        history=dict(rounds=[dict(id=r.id, name=r.name) for r in rounds], series=series),
        cats=[dict(code=c["code"], name=c["name"], name_en=english_name(c["name"]), avg=dash["cats"].get(c["code"]),
                   band=band(dash["cats"].get(c["code"]))) for c in rk["rubric"]],
        pareto=[dict(code=e["code"], text=e["text"], text_en=text_en.get(e["code"], ""), count=e["count"],
                     minor=e["minor"], major=e["major"], areas=e["areas"]) for e in dash["pareto"][:6]],
        checklist=dash["checklist"], actions=dash["actions"],
        summary=dict(dash["bands"], avg=round(sum(avgs) / len(avgs), 1) if avgs else None, photos=dash["n_photos"]))


def get(db, n_rounds: int) -> dict:
    """คืนข้อมูลของจอ: ใช้ผลที่เก็บไว้ถ้าฐานข้อมูลไม่เปลี่ยนและยังเป็นวันเดิม"""
    s = settings_store.load()
    n_rounds = max(2, min(int(n_rounds or s.get("tv_rounds", 6)), 12))
    day = (now() + timedelta(hours=7)).date().isoformat()
    key = (DATA_VERSION["n"], n_rounds, day, s.get("tv_round", "auto"), s.get("ranking_visibility", "always"),
           s.get("band_good", 80), s.get("band_mid", 60), s.get("org_name", ""))
    with _lock:
        hit = _cache.get(n_rounds)
        if hit and hit[0] == key:
            stats["served"] += 1
            return hit[1]
    data = compute(db, s, n_rounds)
    with _lock:
        _cache[n_rounds] = (key, data)
    return data


def display_config(s: dict) -> dict:
    """ค่ากลางของจอที่ผู้ดูแลตั้ง แต่ละจอปรับทับได้เองและเก็บไว้ในเบราว์เซอร์ของจอนั้น"""
    slides = [x for x in (s.get("tv_slides") or []) if x in SLIDES] or ["ranking"]
    return dict(title=s.get("tv_title") or s.get("org_name") or "5ส", title_en=s.get("tv_title_en") or s.get("tv_title")
                or s.get("org_name") or "5S", lang=s.get("tv_lang", "th"), theme=s.get("tv_theme", "light"),
                slides=slides, seconds=int(s.get("tv_seconds", 15)), rows=int(s.get("tv_rows", 8)),
                rounds=int(s.get("tv_rounds", 6)), refresh=int(s.get("tv_refresh_min", 5)), hours=s.get("tv_hours", ""),
                clock=bool(s.get("tv_clock", True)), unranked=bool(s.get("tv_unranked", True)))

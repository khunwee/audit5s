"""การรวมคะแนนและจัดอันดับ

หลักที่ใช้ (ตั้งใจให้แผนกที่ส่งภาพมากไม่ได้เปรียบ และเลือกถ่ายเฉพาะมุมสวยได้ยากขึ้น)
- คะแนนของภาพ = คะแนนรวม / คะแนนเต็มของเกณฑ์ที่ประเมินได้ x 100
- คะแนนของแผนก = ค่าเฉลี่ยคะแนนของทุกภาพที่ให้คะแนนแล้วในรอบนั้น
- แผนกที่ภาพยังไม่ถึงจำนวนขั้นต่ำของรอบ จะยังไม่ถูกจัดอันดับ
- คะแนนเท่ากัน: แผนกที่ภาพคะแนนต่ำสุดสูงกว่า (สม่ำเสมอกว่า) อยู่ก่อน
"""
from collections import defaultdict

from .db import Department, Photo, Round


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def superseded(photos: list) -> set:
    """เลขที่ภาพที่มีภาพหลังแก้ไขซึ่งให้คะแนนแล้ว (ภาพเก่าของจุดเดียวกันไม่นำมาเฉลี่ย)"""
    return {p.after_of for p in photos if p.after_of and p.status == "done" and p.percent is not None}


def dept_stats(photos: list, rubric: list, replace: bool = True) -> dict:
    old = superseded(photos) if replace else set()
    scored = [p for p in photos if p.status == "done" and p.percent is not None and p.id not in old]
    crit = {}
    for c in rubric:
        vals = []
        for p in scored:
            for item in (p.analysis or {}).get("criteria", []):
                if item.get("code") == c["code"] and not item.get("na") and item.get("max"):
                    vals.append(item["score"] / item["max"] * 100)
        crit[c["code"]] = _mean(vals)
    pcts = [p.percent for p in scored]
    return dict(total=len(photos), scored=len(scored),
                waiting=sum(1 for p in photos if p.status in ("pending", "processing")),
                rejected=sum(1 for p in photos if p.status == "rejected"),
                errors=sum(1 for p in photos if p.status == "error"),
                overridden=sum(1 for p in scored if p.overridden), replaced=len(old),
                review=sum(1 for p in scored if p.review_flag),
                avg=_mean(pcts), low=min(pcts) if pcts else None, high=max(pcts) if pcts else None,
                crit=crit)


def round_ranking(db, rnd: Round, with_prev: bool = True) -> dict:
    from . import settings_store
    replace = bool(settings_store.load().get("after_replaces", True))
    rubric = list(rnd.rubric or [])
    photos = db.query(Photo).filter(Photo.round_id == rnd.id).all()
    by = defaultdict(list)
    for p in photos:
        by[p.department_id].append(p)
    depts = {d.id: d for d in db.query(Department).all()}
    rows = []
    for did, d in depts.items():
        if not d.active and did not in by:
            continue
        st = dept_stats(by.get(did, []), rubric, replace)
        st.update(dept=d, qualified=st["scored"] >= max(1, rnd.min_photos), rank=None, delta=None)
        rows.append(st)
    ranked = sorted([r for r in rows if r["qualified"]],
                    key=lambda r: (-r["avg"], -(r["low"] or 0), r["dept"].name))
    for i, r in enumerate(ranked):
        same = i > 0 and ranked[i - 1]["avg"] == r["avg"] and ranked[i - 1]["low"] == r["low"]
        r["rank"] = ranked[i - 1]["rank"] if same else i + 1
    prev = None
    if with_prev:
        prev = db.query(Round).filter(Round.id < rnd.id).order_by(Round.id.desc()).first()
        if prev:
            old = {r["dept"].id: r["avg"] for r in round_ranking(db, prev, with_prev=False)["ranked"]}
            for r in ranked:
                if old.get(r["dept"].id) is not None:
                    r["delta"] = round(r["avg"] - old[r["dept"].id], 2)
    unranked = sorted([r for r in rows if not r["qualified"] and r["total"] > 0],
                      key=lambda r: r["dept"].name)
    idle = sorted([r for r in rows if r["total"] == 0], key=lambda r: r["dept"].name)
    return dict(ranked=ranked, unranked=unranked, idle=idle, rubric=rubric, prev=prev,
                photo_count=len(photos),
                full_score=round(sum(c["max"] for c in rubric), 2))


def find_row(ranking: dict, dept_id: int):
    for group in ("ranked", "unranked", "idle"):
        for r in ranking[group]:
            if r["dept"].id == dept_id:
                return r
    return None

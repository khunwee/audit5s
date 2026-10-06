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


def dept_stats(photos: list, rubric: list, replace: bool = True, verified_only: bool = False,
               required: dict = None) -> dict:
    """สถิติของแผนกในหนึ่งรอบ

    verified_only  นับคะแนนเฉพาะภาพที่หัวหน้าหรือกรรมการยืนยันแล้ว
    required       {เลขที่จุดตรวจ: ชื่อ} ของจุดตรวจบังคับของแผนก ใช้บอกว่ายังขาดจุดใด
    """
    old = superseded(photos) if replace else set()
    done = [p for p in photos if p.status == "done" and p.percent is not None and p.id not in old]
    scored = [p for p in done if p.verified_at is not None] if verified_only else done
    crit = {}
    for c in rubric:
        vals = []
        for p in scored:
            for item in (p.analysis or {}).get("criteria", []):
                if item.get("code") == c["code"] and not item.get("na") and item.get("max"):
                    vals.append(item["score"] / item["max"] * 100)
        crit[c["code"]] = _mean(vals)
    pcts = [p.percent for p in scored]
    required = required or {}
    covered = {p.area_id for p in scored if p.area_id}
    missing = [name for aid, name in required.items() if aid not in covered]
    unverified = sum(1 for p in done if p.verified_at is None)
    return dict(total=len(photos), scored=len(scored),
                waiting=sum(1 for p in photos if p.status in ("pending", "processing")),
                rejected=sum(1 for p in photos if p.status == "rejected"),
                errors=sum(1 for p in photos if p.status == "error"),
                overridden=sum(1 for p in scored if p.overridden), replaced=len(old),
                review=sum(1 for p in scored if p.review_flag),
                unverified=unverified, verified=len(done) - unverified,
                areas_required=len(required), areas_covered=len(required) - len(missing), missing=missing,
                avg=_mean(pcts), low=min(pcts) if pcts else None, high=max(pcts) if pcts else None,
                crit=crit)


def round_ranking(db, rnd: Round, with_prev: bool = True) -> dict:
    from . import settings_store
    from .db import AuditArea
    cfg = settings_store.load()
    replace = bool(cfg.get("after_replaces", True))
    verified_only, need_cover = bool(cfg.get("verify_required")), bool(cfg.get("require_coverage"))
    required = defaultdict(dict)
    for a in (db.query(AuditArea).filter(AuditArea.active.is_(True), AuditArea.required.is_(True))
              .order_by(AuditArea.sort_order, AuditArea.id).all()):
        required[a.department_id][a.id] = a.name
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
        st = dept_stats(by.get(did, []), rubric, replace, verified_only, required.get(did))
        st.update(dept=d, rank=None, delta=None,
                  qualified=st["scored"] >= max(1, rnd.min_photos) and not (need_cover and st["missing"]))
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
                verify_required=verified_only, require_coverage=need_cover,
                has_areas=any(required.values()),
                photo_count=len(photos),
                full_score=round(sum(c["max"] for c in rubric), 2))


def find_row(ranking: dict, dept_id: int):
    for group in ("ranked", "unranked", "idle"):
        for r in ranking[group]:
            if r["dept"].id == dept_id:
                return r
    return None


def dashboard(db, rnd: Round) -> dict:
    """ภาพรวมของรอบ: สถานะสี คะแนนรายหมวดทั้งโรงงาน ข้อที่ไม่ผ่านบ่อย (Pareto) ข้อที่พบซ้ำจากรอบก่อน และงานแก้ไขค้าง"""
    from . import actions as act_mod, settings_store
    from .db import Action
    from .web import band
    rk = round_ranking(db, rnd)
    rows = rk["ranked"] + rk["unranked"]
    bands = {"good": 0, "mid": 0, "low": 0}
    for r in rk["ranked"]:
        bands[band(r["avg"])] += 1
    photos = db.query(Photo).filter(Photo.round_id == rnd.id, Photo.status == "done").all()
    old = superseded(photos) if settings_store.load().get("after_replaces", True) else set()
    photos = [p for p in photos if p.id not in old and p.percent is not None]
    cats = {}
    for c in rk["rubric"]:
        vals = [i["score"] / i["max"] * 100 for p in photos for i in (p.analysis or {}).get("criteria", [])
                if i.get("code") == c["code"] and not i.get("na") and i.get("max")]
        cats[c["code"]] = _mean(vals)
    pareto, repeats = {}, []
    for p in photos:
        for x in (p.analysis or {}).get("checks", []):
            if x.get("status") in ("minor", "major"):
                e = pareto.setdefault(x["code"], dict(code=x["code"], text=x["text"], minor=0, major=0, areas=set()))
                e[x["status"]] += 1
                e["areas"].add(p.area_name)
                if x.get("repeat"):
                    repeats.append(dict(dept=p.department.name, area=p.area_name, text=x["text"], photo=p))
    top = sorted(pareto.values(), key=lambda e: (-(e["minor"] + e["major"]), -e["major"], e["code"]))[:10]
    for e in top:
        e["count"], e["areas"] = e["minor"] + e["major"], len(e["areas"])
    open_acts = db.query(Action).filter(Action.status == "open").all()
    by_dept = defaultdict(lambda: dict(open=0, overdue=0))
    for a in open_acts:
        by_dept[a.department_id]["open"] += 1
        if act_mod.is_overdue(a):
            by_dept[a.department_id]["overdue"] += 1
    return dict(ranking=rk, rows=rows, bands=bands, cats=cats, pareto=top, repeats=repeats[:20],
                n_photos=len(photos), checklist=(rnd.mode or "level") == "checklist",
                actions=dict(open=len(open_acts), overdue=sum(1 for a in open_acts if act_mod.is_overdue(a))),
                act_by_dept=dict(by_dept), max_count=max([e["count"] for e in top] or [1]))


def ai_quality(db, round_id: int = 0) -> dict:
    """เทียบสิ่งที่ AI เสนอกับสิ่งที่คนยืนยัน ในภาพที่ยืนยันแล้วของโหมดรายการตรวจ

    false_alarm = AI ว่าไม่ผ่าน คนว่าผ่าน / miss = AI ว่าผ่าน คนว่าไม่ผ่าน / severity = ไม่ผ่านทั้งคู่แต่ระดับต่างกัน
    """
    q = db.query(Photo).filter(Photo.status == "done", Photo.verified_at.isnot(None))
    if round_id:
        q = q.filter(Photo.round_id == round_id)
    per, total = {}, dict(n=0, agree=0, false_alarm=0, miss=0, severity=0, visibility=0)
    n_photos = 0
    for p in q.all():
        a = p.analysis or {}
        if a.get("mode") != "checklist":
            continue
        n_photos += 1
        for x in a.get("checks", []):
            ai_status, final = x.get("ai_status", x.get("status")), x.get("status")
            if ai_status is None:
                continue
            e = per.setdefault(x["code"], dict(code=x["code"], text=x["text"], n=0, agree=0, false_alarm=0, miss=0,
                                               severity=0, visibility=0))
            ng_ai, ng_h = ai_status in ("minor", "major"), final in ("minor", "major")
            kind = ("agree" if ai_status == final else "visibility" if "na" in (ai_status, final)
                    else "false_alarm" if ng_ai and not ng_h else "miss" if ng_h and not ng_ai else "severity")
            for target in (e, total):
                target["n"] += 1
                target[kind] += 1
    rows = sorted(per.values(), key=lambda e: (e["agree"] / e["n"] if e["n"] else 1, e["code"]))
    for e in rows + [total]:
        e["pct"] = round(e["agree"] / e["n"] * 100, 1) if e["n"] else None
    return dict(rows=rows, total=total, photos=n_photos)

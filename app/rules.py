"""Rule engine ของโหมดรายการตรวจ

หน้าที่ของ AI คือบอกสภาพของแต่ละข้อ (ผ่าน / บกพร่องเล็กน้อย / บกพร่องมาก / มองไม่เห็น) พร้อมหลักฐาน
หน้าที่ของไฟล์นี้คือแปลงผลนั้นเป็นคะแนนตามกติกาที่โรงงานตั้ง: AI ไม่ได้ให้คะแนนเอง และกติกาแก้ได้โดยไม่ต้องสอน AI ใหม่

คะแนนของหมวด = คะแนนเต็มของหมวด x (แต้มที่ได้ / แต้มเต็มของข้อที่ประเมินได้ในหมวดนั้น)
หมวดแบบ sustain (สร้างนิสัย) ไม่ได้ดูจากภาพ: คำนวณจากข้อที่พบซ้ำจากรอบก่อนของจุดเดียวกัน และงานแก้ไขที่เกินกำหนด
"""
from datetime import timedelta

from .db import Action, Photo, now

STATUS = {"ok": "ผ่าน", "minor": "บกพร่องเล็กน้อย", "major": "บกพร่องมาก", "na": "มองไม่เห็นในภาพนี้"}
NG = ("minor", "major")
RANK = {"ok": 0, "minor": 1, "major": 2}


def fixed_view(photo: Photo) -> bool:
    """ภาพจากกล้องติดตาย มุมภาพคงที่ จึงใช้กรอบโซนได้"""
    return photo.camera_id is not None or (photo.source or "") in ("ipcam", "agent")


def applicable(checklist: list, rubric: list, photo: Photo) -> list:
    """รายการตรวจที่ใช้กับภาพนี้: ข้อทั่วไปตามประเภทพื้นที่ + ข้อเฉพาะของจุดตรวจ (ข้อที่เป็นโซนใช้เฉพาะภาพจากกล้องติดตาย)"""
    crits = {c["code"] for c in rubric if (c.get("kind") or "ai") != "sustain"}
    out = []
    for k in checklist or []:
        if k["crit"] not in crits:
            continue
        if k.get("area_id"):
            if k["area_id"] != photo.area_id or (k.get("zone") and not fixed_view(photo)):
                continue
        elif k.get("types") and photo.area_type not in k["types"]:
            continue
        out.append(k)
    return out


def zones_of(checks: list) -> list:
    return [(k["code"], k["zone"]) for k in checks if k.get("zone")]


def points_of(check: dict) -> float:
    if check["status"] == "ok":
        return float(check["max"])
    if check["status"] == "minor":
        return float(min(check.get("minor", 0), check["max"]))
    return 0.0


def previous_audit(db, photo: Photo):
    """ผลล่าสุดของจุดเดียวกันจากรอบก่อนหน้า (โหมดรายการตรวจ)"""
    rows = (db.query(Photo).filter(Photo.department_id == photo.department_id, Photo.area_name == photo.area_name,
                                   Photo.round_id < photo.round_id, Photo.status == "done")
            .order_by(Photo.round_id.desc(), Photo.id.desc()).limit(8).all())
    for p in rows:
        if (p.analysis or {}).get("mode") == "checklist":
            return p
    return None


def sustain(db, photo: Photo, checks: list, crit: dict) -> dict:
    """คะแนนสร้างนิสัยของจุดนี้: แก้ปัญหาที่พบรอบก่อนได้กี่ข้อ หักเพิ่มตามงานแก้ไขที่เกินกำหนด"""
    prev = previous_audit(db, photo)
    today = (now() + timedelta(hours=7)).date()
    overdue = (db.query(Action).filter(Action.department_id == photo.department_id, Action.area_name == photo.area_name,
                                       Action.status == "open", Action.due_date.isnot(None), Action.due_date < today).count())
    base = dict(code=crit["code"], name=crit["name"], max=float(crit["max"]), level=None, computed=True,
                findings=[], recommendations=[])
    if prev is None and not overdue:
        return dict(base, na=True, score=0.0, reason="ยังไม่มีผลรอบก่อนของจุดนี้ให้เทียบ จึงไม่คิดคะแนนหมวดนี้ในรอบแรก")
    before = {x["code"]: x for x in (prev.analysis or {}).get("checks", []) if x.get("status") in NG} if prev else {}
    current = {x["code"]: x for x in checks if x["status"] != "na"}
    comparable = [c for c in before if c in current]
    repeats = [current[c] for c in comparable if current[c]["status"] in NG]
    fraction = 1.0 - (len(repeats) / len(comparable) if comparable else 0.0)
    fraction = max(0.0, fraction - 0.1 * overdue)
    for x in repeats:
        x["repeat"] = True
        base["findings"].append(f"พบซ้ำจากรอบก่อน: {x['text']}")
    if overdue:
        base["findings"].append(f"งานแก้ไขของจุดนี้เกินกำหนด {overdue} งาน")
    if repeats or overdue:
        base["recommendations"].append("แก้ที่สาเหตุของข้อที่พบซ้ำ และปิดงานแก้ไขที่ค้างให้ทันกำหนด")
    if comparable:
        reason = f"รอบก่อนพบข้อบกพร่อง {len(comparable)} ข้อ แก้ได้แล้ว {len(comparable) - len(repeats)} ข้อ พบซ้ำ {len(repeats)} ข้อ"
    else:
        reason = "รอบก่อนไม่พบข้อบกพร่องที่เทียบได้กับภาพนี้"
    if overdue:
        reason += f" และมีงานแก้ไขเกินกำหนด {overdue} งาน (หักงานละ 10%)"
    return dict(base, na=False, score=round(float(crit["max"]) * fraction, 2), reason=reason,
                repeats=len(repeats), overdue=overdue)


def score(db, photo: Photo, rubric: list, checks: list) -> list:
    """แปลงผลรายข้อเป็นคะแนนรายหมวด (โครงสร้างเดียวกับโหมดระดับ ส่วนอื่นของระบบจึงใช้ต่อได้ทันที)"""
    for x in checks:
        x["points"] = points_of(x) if x["status"] != "na" else 0.0
    out = []
    for c in rubric:
        if (c.get("kind") or "ai") == "sustain":
            out.append(sustain(db, photo, checks, c))
            continue
        mine = [x for x in checks if x["crit"] == c["code"]]
        seen = [x for x in mine if x["status"] != "na"]
        item = dict(code=c["code"], name=c["name"], max=float(c["max"]), level=None,
                    n_ok=sum(1 for x in seen if x["status"] == "ok"),
                    n_minor=sum(1 for x in seen if x["status"] == "minor"),
                    n_major=sum(1 for x in seen if x["status"] == "major"),
                    findings=[x["evidence"] for x in seen if x["status"] in NG and x.get("evidence")],
                    recommendations=[x["action"] for x in seen if x["status"] in NG and x.get("action")])
        if not seen:
            item.update(na=True, score=0.0, reason="ไม่มีข้อตรวจของหมวดนี้ที่ประเมินได้จากภาพนี้")
        else:
            got, full = sum(x["points"] for x in seen), sum(float(x["max"]) for x in seen)
            item.update(na=False, score=round(float(c["max"]) * got / full, 2) if full else 0.0,
                        reason=f"ผ่าน {item['n_ok']} จาก {len(seen)} ข้อ"
                               + (f", บกพร่องเล็กน้อย {item['n_minor']}" if item["n_minor"] else "")
                               + (f", บกพร่องมาก {item['n_major']}" if item["n_major"] else ""))
        out.append(item)
    return out


def finalize(db, photo: Photo, rubric: list, result: dict) -> dict:
    """ใส่คะแนนรายหมวดลงในผลของโหมดรายการตรวจ (ใช้ทั้งหลัง AI ตอบ และหลังกรรมการปรับรายข้อ)"""
    result["mode"] = "checklist"
    if result.get("image_ok", True):
        result["criteria"] = score(db, photo, rubric, result.get("checks", []))
        if all(c["na"] for c in result["criteria"]):
            result["image_ok"] = False
            result["image_issue"] = result.get("image_issue") or "ไม่มีข้อตรวจใดที่ประเมินได้จากภาพนี้"
    return result

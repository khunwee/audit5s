"""ชุดตั้งค่าเริ่มต้น: ตั้งหมวด รายการตรวจ ประเภทพื้นที่ และค่าที่แนะนำให้ครบในครั้งเดียว

มี 2 ชุด: โรงงาน และ สำนักงาน ผู้ดูแลเลือกชุดที่ตรงกับงาน แล้วแก้ทุกอย่างต่อได้ตามปกติที่หน้าเดิมของแต่ละเรื่อง
ชุดตั้งค่าไม่แตะสิ่งที่เป็นของหน่วยงานเอง: ชื่อโรงงาน API key แผนก ผู้ใช้ จุดตรวจ โซน กล้อง ช่องทางแจ้งเตือน รอบ และภาพ
"""
from . import settings_store
from .db import DEFAULT_CRITERIA, Checkpoint, Criterion, log

# หมวด 5ส ใช้ร่วมกันทั้งสองชุด: 4 หมวดแรกตรวจจากภาพ หมวดสร้างนิสัยระบบคำนวณจากการพบซ้ำและงานแก้ไขที่เกินกำหนด
CATEGORIES = [("S1", "สะสาง (Seiri)", 20, "ai"), ("S2", "สะดวก (Seiton)", 20, "ai"), ("S3", "สะอาด (Seiso)", 20, "ai"),
              ("S4", "สุขลักษณะ / สร้างมาตรฐาน (Seiketsu)", 20, "ai"), ("S5", "สร้างนิสัย (Shitsuke)", 20, "sustain")]

_FEW = "พบ 1-2 จุด และไม่กีดขวางการทำงานหรือความปลอดภัย"
_MANY = "พบหลายจุด หรือกีดขวางการทำงาน ทางเดิน หรืออุปกรณ์ความปลอดภัย"
PROD = ["สายการผลิต", "คลังสินค้า", "ซ่อมบำรุง"]
F_OFFICE = ["สำนักงานในโรงงาน"]

# (รหัส, หมวด, สภาพที่ถูกต้อง, บกพร่องเล็กน้อยเมื่อ, บกพร่องมากเมื่อ, แต้ม, ประเภทพื้นที่ที่ใช้ [] = ทุกประเภท, ข้อความภาษาอังกฤษ)
FACTORY_CHECKS = [
    ("C01", "S1", "ไม่มีของที่ไม่จำเป็น ของเสีย หรือของชำรุดวางอยู่ในพื้นที่", _FEW, _MANY, 5, [],
     "No unneeded, defective or damaged items in the area"),
    ("C02", "S1", "ไม่มีพาเลทเปล่าหรือพาเลทที่ไม่ใช้งานวางค้างอยู่", "พบ 1 ใบ วางชิดขอบ ไม่กีดขวาง",
     "พบหลายใบ หรือวางกีดขวางทางเดินหรือพื้นที่ทำงาน", 5, PROD, "No empty or unused pallets left in the area"),
    ("C03", "S1", "ปริมาณงานระหว่างผลิตและวัสดุไม่ล้นเกินพื้นที่หรือชั้นที่กำหนด", "ล้นออกนอกพื้นที่เล็กน้อย 1 จุด",
     "กองล้นหลายจุด หรือวางซ้อนสูงจนเสี่ยงล้ม", 5, PROD, "Work in progress and materials do not overflow the designated area or rack"),
    ("C04", "S2", "ทางเดินโล่ง ไม่มีสิ่งของวางอยู่ในทางเดิน", "มีของล้ำเข้ามาในทางเดินเล็กน้อย 1 จุด ยังเดินผ่านได้สะดวก",
     "มีของวางขวางทางเดิน หรือทางเดินแคบลงจนผ่านลำบาก", 5, [], "Walkways are clear, with nothing placed in them"),
    ("C05", "S2", "วัสดุและชิ้นงานอยู่ภายในเส้นหรือพื้นที่ที่กำหนด ไม่วางทับหรือคร่อมเส้นเหลือง", "วางทับหรือคร่อมเส้น 1-2 จุด",
     "วางนอกพื้นที่ที่กำหนดหลายจุด หรือไม่มีการวางตามพื้นที่เลย", 5, PROD,
     "Materials and parts are inside the marked lines or designated area, not on or across yellow lines"),
    ("C06", "S2", "เครื่องมือและอุปกรณ์อยู่ในตำแหน่งที่กำหนด", "มีเครื่องมือวางผิดที่ 1-2 ชิ้น", "เครื่องมือหลายชิ้นวางกระจาย หรือวางบนพื้น",
     5, PROD, "Tools and equipment are in their designated positions"),
    ("C07", "S2", "ถัง กล่อง และภาชนะวางตรงตำแหน่งและเป็นแนวเดียวกัน", "วางเบี้ยวหรือผิดตำแหน่ง 1-2 ใบ",
     "วางปะปน ซ้อนไม่เป็นระเบียบ หรือผิดตำแหน่งหลายใบ", 5, PROD, "Bins, boxes and containers are in position and aligned"),
    ("C14", "S2", "สายไฟ สายลม และท่อไม่พาดขวางทางเดินหรือพื้นที่ทำงาน", "มีสายพาดผ่านพื้นที่ทำงาน 1 จุด โดยไม่ขวางทางเดิน",
     "มีสายพาดขวางทางเดิน หรือพาดระเกะระกะหลายจุด", 5, [], "Cables, air hoses and pipes do not cross walkways or work areas"),
    ("C08", "S3", "ไม่มีเศษวัสดุหรือขยะบนพื้น", "พบเศษเล็กน้อย 1-2 จุด", "พบเศษวัสดุหรือขยะหลายจุด หรือกองสะสม", 5, [],
     "No scrap or litter on the floor"),
    ("C09", "S3", "ไม่มีคราบน้ำมันหรือน้ำบนพื้น", "พบคราบแห้งหรือคราบขนาดเล็ก 1 จุด", "พบคราบเปียก คราบขนาดใหญ่ หรือหลายจุด ซึ่งเสี่ยงลื่น",
     5, PROD, "No oil or water on the floor"),
    ("C10", "S3", "พื้น เครื่องจักร โต๊ะ และชั้นวางสะอาด ไม่มีฝุ่นหรือคราบสะสม", "พบฝุ่นหรือคราบเล็กน้อย 1-2 จุด",
     "พบฝุ่นหนาหรือคราบสกปรกเห็นชัดหลายบริเวณ", 5, [], "Floors, machines, benches and racks are clean, with no built-up dust or stains"),
    ("C15", "S3", "ถังขยะไม่ล้น และแยกประเภทตามป้ายหรือสีของถัง", "ถังเต็มถึงขอบ 1 ใบ หรือมีขยะผิดประเภทเล็กน้อย",
     "ถังล้นจนขยะตกพื้น หรือทิ้งปะปนโดยไม่แยกประเภท", 5, [], "Waste bins are not overflowing and waste is sorted by bin label or colour"),
    ("C11", "S4", "วัสดุ ชิ้นงาน และชั้นวางมีป้ายชี้บ่งที่อ่านได้ชัดเจน", "ป้ายขาดหายหรืออ่านยาก 1-2 จุด",
     "ไม่มีป้ายชี้บ่งเป็นส่วนใหญ่ หรือป้ายเสียหายหลายจุด", 5, PROD, "Materials, parts and racks have clear, readable identification labels"),
    ("C12", "S4", "เส้นแบ่งพื้นที่และป้ายมาตรฐานอยู่ในสภาพดี ไม่ลบเลือน", "เส้นหรือป้ายซีดจางบางช่วง",
     "เส้นหรือป้ายลบเลือนจนแยกพื้นที่ไม่ได้ หรือไม่มีเส้นแบ่ง", 5, PROD, "Floor markings and standard signs are in good condition and not faded"),
    ("C13", "S4", "ถังดับเพลิง ทางหนีไฟ และตู้ไฟฟ้าไม่ถูกสิ่งของกีดขวาง", "มีของวางใกล้แต่ยังเข้าถึงได้", "มีของวางขวางจนเข้าถึงไม่ได้ทันที",
     10, [], "Fire extinguishers, fire exits and electrical panels are not blocked"),
    ("C16", "S4", "มีป้ายชื่อพื้นที่หรือป้ายผู้รับผิดชอบพื้นที่ติดอยู่และอ่านได้", "มีป้ายแต่ซีดจางหรือถูกบังบางส่วน",
     "ไม่มีป้าย หรือป้ายเสียหายจนอ่านไม่ได้", 5, [], "An area name or area-owner sign is posted and readable"),
    # สำนักงานที่อยู่ในโรงงาน: ใช้ข้อของสำนักงานเฉพาะกับประเภทพื้นที่นี้
    ("O01", "S1", "บนโต๊ะทำงานมีเฉพาะของที่ใช้งานประจำ ไม่มีเอกสารหรือของกองสะสม", "มีเอกสารหรือของกองบนโต๊ะ 1-2 โต๊ะ",
     "โต๊ะส่วนใหญ่มีเอกสารหรือของกองสะสม", 5, F_OFFICE, "Desks hold only items in regular use, with no piles of documents or clutter"),
    ("O04", "S2", "แฟ้มและเอกสารเก็บในตู้หรือชั้นเป็นแนว และมีป้ายสันแฟ้ม", "แฟ้มไม่มีป้ายหรือวางไม่เป็นแนว 1-2 จุด",
     "แฟ้มส่วนใหญ่ไม่มีป้าย หรือวางกองปะปนกัน", 5, F_OFFICE, "Files and documents are stored upright in cabinets or shelves with spine labels"),
    ("O06", "S2", "สายไฟและสายสัญญาณใต้โต๊ะเก็บเป็นระเบียบ", "สายพันกันหรือห้อยระเกะระกะ 1-2 โต๊ะ", "สายพาดทางเดิน หรือระเกะระกะหลายโต๊ะ",
     5, F_OFFICE, "Power and signal cables under desks are tidy"),
]

# หัวข้อที่ไม่ใช่สภาพของพื้นที่โดยตรง: ถ่ายภาพบอร์ด 5ส และใบตรวจเช็คเป็นหลักฐาน ใช้กับประเภทพื้นที่นี้เท่านั้น
BOARD = ["บอร์ดและเอกสาร 5ส"]
BOARD_CHECKS = [
    ("D01", "S4", "บอร์ด 5ส แสดงผลการตรวจหรือแผนงานของเดือนปัจจุบัน", "ข้อมูลบนบอร์ดเป็นของเดือนก่อน", "บอร์ดว่าง ข้อมูลเก่าเกิน 2 เดือน หรือไม่มีบอร์ด",
     5, BOARD, "The 5S board shows audit results or the plan for the current month"),
    ("D02", "S4", "ใบตรวจเช็คประจำวันหรือประจำสัปดาห์ลงบันทึกครบถึงวันล่าสุด", "ขาดการลงบันทึก 1-2 ช่อง", "ขาดการลงบันทึกหลายวัน หรือไม่มีใบตรวจเช็ค",
     5, BOARD, "The daily or weekly check sheet is filled in up to the latest day"),
    ("D03", "S4", "มีแผนผังพื้นที่และชื่อผู้รับผิดชอบแต่ละพื้นที่ติดแสดง", "มีแผนผังแต่ไม่ระบุผู้รับผิดชอบ หรืออ่านยาก", "ไม่มีแผนผังและไม่ระบุผู้รับผิดชอบ",
     5, BOARD, "An area layout with the owner of each area is displayed"),
]

DESK = ["โต๊ะทำงาน"]
OFFICE_CHECKS = [
    ("O01", "S1", "บนโต๊ะทำงานมีเฉพาะของที่ใช้งานประจำ ไม่มีเอกสารหรือของกองสะสม", "มีเอกสารหรือของกองบนโต๊ะ 1-2 โต๊ะ",
     "โต๊ะส่วนใหญ่มีเอกสารหรือของกองสะสม", 5, DESK, "Desks hold only items in regular use, with no piles of documents or clutter"),
    ("O02", "S1", "ไม่มีกล่อง เอกสารเก่า หรืออุปกรณ์ที่ไม่ใช้วางบนพื้น ใต้โต๊ะ หรือบนหลังตู้", "พบ 1-2 จุด", "พบหลายจุด หรือกองสูงจนเสี่ยงหล่น",
     5, [], "No boxes, old documents or unused equipment on the floor, under desks or on top of cabinets"),
    ("O03", "S1", "ไม่มีอุปกรณ์สำนักงานชำรุดหรือของที่เลิกใช้วางค้างในพื้นที่", "พบ 1 ชิ้น", "พบหลายชิ้น หรือวางกีดขวาง", 5, [],
     "No broken office equipment or disused items left in the area"),
    ("O04", "S2", "แฟ้มและเอกสารเก็บในตู้หรือชั้นเป็นแนว และมีป้ายสันแฟ้ม", "แฟ้มไม่มีป้ายหรือวางไม่เป็นแนว 1-2 จุด",
     "แฟ้มส่วนใหญ่ไม่มีป้าย หรือวางกองปะปนกัน", 5, ["โต๊ะทำงาน", "ห้องเก็บเอกสาร"],
     "Files and documents are stored upright in cabinets or shelves with spine labels"),
    ("O05", "S2", "ตู้ ชั้น และลิ้นชักมีป้ายบอกสิ่งที่เก็บ", "ป้ายขาดหายหรืออ่านยาก 1-2 จุด", "ส่วนใหญ่ไม่มีป้าย", 5, [],
     "Cabinets, shelves and drawers are labelled with their contents"),
    ("O06", "S2", "สายไฟและสายสัญญาณเก็บเป็นระเบียบ ไม่พาดทางเดิน", "สายพันกันหรือห้อยระเกะระกะ 1-2 จุด", "สายพาดทางเดิน หรือระเกะระกะหลายจุด",
     5, [], "Power and signal cables are tidy and do not cross walkways"),
    ("O07", "S2", "ทางเดินและหน้าประตูไม่มีสิ่งของวางกีดขวาง", "มีของล้ำเข้ามาเล็กน้อย 1 จุด ยังเดินผ่านได้สะดวก", "มีของวางขวางทางเดินหรือหน้าประตู",
     5, [], "Walkways and doorways are free of obstructions"),
    ("O08", "S2", "ห้องประชุมพร้อมใช้: เก้าอี้เข้าที่ กระดานลบแล้ว ไม่มีของค้างบนโต๊ะ", "มีของค้างหรือเก้าอี้ไม่เข้าที่เล็กน้อย",
     "ห้องไม่พร้อมใช้: ของค้างหลายจุด กระดานไม่ลบ เก้าอี้กระจัดกระจาย", 5, ["ห้องประชุม"],
     "Meeting room is ready to use: chairs in place, board erased, nothing left on the table"),
    ("O09", "S3", "โต๊ะ พื้น และอุปกรณ์สำนักงานสะอาด ไม่มีฝุ่น คราบ หรือเศษอาหาร", "พบฝุ่น คราบ หรือเศษเล็กน้อย 1-2 จุด",
     "พบฝุ่นหนา คราบสกปรก หรือเศษอาหารหลายจุด", 5, [], "Desks, floors and office equipment are clean, with no dust, stains or food scraps"),
    ("O10", "S3", "ถังขยะไม่ล้น", "ถังเต็มถึงขอบ 1 ใบ", "ถังล้นจนขยะตกพื้น", 5, [], "Waste bins are not overflowing"),
    ("O11", "S3", "พื้นที่เตรียมอาหารและอ่างล้างสะอาด ไม่มีภาชนะใช้แล้วค้างอยู่", "มีภาชนะค้าง 1-2 ชิ้น หรือคราบเล็กน้อย",
     "มีภาชนะค้างจำนวนมาก คราบสกปรก หรือเศษอาหารค้าง", 5, ["ห้องเตรียมอาหาร"],
     "Pantry and sink are clean, with no used dishes left behind"),
    ("O12", "S4", "ถังดับเพลิง ทางหนีไฟ และตู้ไฟฟ้าไม่ถูกสิ่งของกีดขวาง", "มีของวางใกล้แต่ยังเข้าถึงได้", "มีของวางขวางจนเข้าถึงไม่ได้ทันที",
     10, [], "Fire extinguishers, fire exits and electrical panels are not blocked"),
    ("O13", "S4", "มีป้ายชื่อพื้นที่หรือป้ายผู้รับผิดชอบพื้นที่ติดอยู่และอ่านได้", "มีป้ายแต่ซีดจางหรือถูกบังบางส่วน",
     "ไม่มีป้าย หรือป้ายเสียหายจนอ่านไม่ได้", 5, [], "An area name or area-owner sign is posted and readable"),
    ("O14", "S4", "บอร์ดประกาศและป้ายบนผนังจัดเป็นระเบียบ ไม่มีเอกสารเก่าฉีกขาดหรือติดซ้อนทับกัน", "มีเอกสารติดซ้อนหรือเอียง 1-2 จุด",
     "บอร์ดรก เอกสารฉีกขาดหรือติดซ้อนทับกันหลายจุด", 5, [], "Notice boards and wall signs are orderly, with no torn or overlapping old notices"),
]

_COMMON = dict(scoring_mode="checklist", verify_required=True, auto_actions=True, action_due_days=7, action_due_days_major=3,
               after_replaces=True, allow_free_area=True, require_coverage=False, ai_passes=1, band_good=80, band_mid=60,
               storage_budget_mb=350, host_limit_mb=500, storage_warn_pct=80, retention_days=90, purge_requires_backup=True,
               backup_remind_days=7, rounds_repeat="monthly", rounds_repeat_prefix="ตรวจ 5ส", cam_grace_min=20,
               gallery_max_age_h=48, gallery_stale="flag", dept_remind_days=3,
               tv_slides=["ranking", "trend", "ranks", "history", "category", "actions"])
_NO_DOUBLE = ("ถ้าสิ่งของชิ้นเดียวกันทำให้ไม่ผ่านได้มากกว่าหนึ่งข้อ ให้นับที่ข้อที่ตรงที่สุดเพียงข้อเดียว และให้ข้ออื่นเป็น ok ถ้าไม่มีสิ่งอื่นที่ทำให้ไม่ผ่าน\n"
              "อย่าตัดสินจากสิ่งที่เห็นไม่ชัดหรืออยู่ไกลจนแยกไม่ออก ให้ตอบ na สำหรับข้อนั้น")

PRESETS = {
    "factory": dict(
        name="โรงงาน", name_en="Factory",
        summary="พื้นที่ผลิต คลังสินค้า ซ่อมบำรุง ทางเดิน และสำนักงานที่อยู่ในโรงงาน",
        area_types=["สายการผลิต", "คลังสินค้า", "ซ่อมบำรุง", "ทางเดินและพื้นที่ส่วนกลาง", "สำนักงานในโรงงาน", "บอร์ดและเอกสาร 5ส"],
        checks=FACTORY_CHECKS + BOARD_CHECKS,
        settings=dict(_COMMON, cam_schedule={"times": [], "random": 2, "between": "08:30-16:30", "days": [0, 1, 2, 3, 4]},
                      ai_extra="เส้นสีเหลืองบนพื้นคือเส้นแบ่งทางเดินและพื้นที่วางของ\n"
                               "ของที่วางทับหรือคร่อมเส้นแต่ไม่อยู่ในทางเดิน ให้นับที่ข้อ C05 ของที่วางอยู่ในทางเดิน ให้นับที่ข้อ C04\n"
                               "รถเข็น รถยก และพาเลทที่มีคนกำลังใช้งานอยู่ในภาพ ไม่นับเป็นของวางกีดขวาง\n" + _NO_DOUBLE)),
    "office": dict(
        name="สำนักงาน", name_en="Office",
        summary="โต๊ะทำงาน ห้องประชุม ห้องเก็บเอกสาร พื้นที่ส่วนกลาง และห้องเตรียมอาหาร",
        area_types=["โต๊ะทำงาน", "ห้องประชุม", "ห้องเก็บเอกสาร", "พื้นที่ส่วนกลาง", "ห้องเตรียมอาหาร", "บอร์ดและเอกสาร 5ส"],
        checks=OFFICE_CHECKS + BOARD_CHECKS,
        settings=dict(_COMMON, cam_schedule={"times": [], "random": 1, "between": "09:00-16:00", "days": [0, 1, 2, 3, 4]},
                      ai_extra="ภาพเป็นพื้นที่สำนักงาน ของใช้ส่วนตัวจำนวนเล็กน้อยบนโต๊ะ เช่น แก้วน้ำ 1 ใบ หรือรูปถ่าย 1 กรอบ ไม่นับเป็นของที่ไม่จำเป็น\n"
                               "เอกสารที่กำลังใช้งานและวางเป็นตั้งเรียบร้อย 1 ตั้ง ไม่นับเป็นของกองสะสม\n" + _NO_DOUBLE)),
}
SETTING_LABELS = [
    ("scoring_mode", "วิธีให้คะแนนของรอบใหม่: รายการตรวจ (AI ตรวจทีละข้อ ระบบคิดคะแนน)"),
    ("verify_required", "นับคะแนนเฉพาะภาพที่หัวหน้าหรือกรรมการยืนยันแล้ว"),
    ("auto_actions", "สร้างงานแก้ไขจากข้อที่ไม่ผ่านเมื่อยืนยันผล กำหนดเสร็จ 3 วันสำหรับบกพร่องมาก และ 7 วันสำหรับเล็กน้อย"),
    ("after_replaces", "จุดที่ส่งภาพหลังแก้ไข นับเฉพาะภาพล่าสุด"),
    ("band_good", "สีของคะแนน: เขียวตั้งแต่ 80% เหลือง 60 ถึง 79% แดงต่ำกว่า 60%"),
    ("rounds_repeat", "สร้างรอบถัดไปเองทุกเดือน เมื่อรอบก่อนมีวันสิ้นสุดและปิดแล้ว"),
    ("cam_schedule", "ตารางค่ากลางของกล้อง IP: สุ่มเวลาถ่ายในเวลางาน วันจันทร์ถึงศุกร์"),
    ("retention_days", "พื้นที่จัดเก็บ: งบ 350 MB ลบภาพเต็มที่เก่ากว่า 90 วัน เฉพาะรอบที่สำรองแล้ว"),
    ("ai_extra", "คำแนะนำเพิ่มเติมถึง AI: วิธีตีความสิ่งที่เห็น และไม่หักซ้ำสองข้อจากของชิ้นเดียว"),
    ("gallery_max_age_h", "ภาพจากคลังภาพที่ถ่ายไว้เกิน 48 ชั่วโมงถูกติดป้ายให้กรรมการเห็น"),
    ("dept_remind_days", "เตือนแผนกที่ภาพยังไม่ครบ 3 วันก่อนปิดรอบ"),
]


def preview(key: str) -> dict:
    p = PRESETS[key]
    by = {}
    for c in p["checks"]:
        by.setdefault(c[1], []).append(dict(code=c[0], text=c[2], minor=c[3], major=c[4], points=c[5], types=c[6]))
    return dict(key=key, name=p["name"], summary=p["summary"], area_types=p["area_types"], n_checks=len(p["checks"]),
                categories=[dict(code=code, name=name, max=mx, kind=kind, checks=by.get(code, [])) for code, name, mx, kind in CATEGORIES],
                ai_extra=p["settings"]["ai_extra"])


def apply_rules(db, key: str) -> int:
    """ตั้งหมวด 5ส และรายการตรวจทั่วไปตามชุดที่เลือก โซนของจุดตรวจ (ข้อที่ผูกกับจุดตรวจ) ไม่ถูกแตะ"""
    p = PRESETS[key]
    levels = {c["code"]: c for c in DEFAULT_CRITERIA}
    standard = {code for code, _, _, _ in CATEGORIES}
    for crit in db.query(Criterion).all():
        if crit.code not in standard:
            crit.active = False                          # หมวดที่เพิ่มเองไม่ถูกลบ แต่ปิดใช้ เปิดกลับได้ที่หน้า หมวดและคะแนนเต็ม
    for i, (code, name, mx, kind) in enumerate(CATEGORIES):
        crit = db.query(Criterion).filter(Criterion.code == code).first()
        if crit is None:
            base = dict(levels.get(code, {}))
            base.update(code=code)
            crit = Criterion(**{k: v for k, v in base.items() if k not in ("name", "max_score", "kind")}, name=name, max_score=mx)
            db.add(crit)
        crit.name, crit.max_score, crit.kind, crit.active, crit.sort_order = name, mx, kind, True, (i + 1) * 10
    db.query(Checkpoint).filter(Checkpoint.area_id.is_(None)).delete(synchronize_session=False)
    db.flush()
    for i, (code, crit, text, minor, major, points, types, en) in enumerate(p["checks"]):
        db.add(Checkpoint(code=code, crit_code=crit, text=text, minor_hint=minor, major_hint=major, points=points,
                          minor_points=round(points * 0.6, 1), area_types=list(types), allow_na=True, sort_order=(i + 1) * 10,
                          text_en=en))
    return len(p["checks"])


def apply_settings(db, key: str) -> dict:
    """ตั้งค่าที่แนะนำของชุดที่เลือก ประเภทพื้นที่เดิมที่ไม่อยู่ในชุดยังอยู่ต่อท้าย เพื่อไม่ให้จุดตรวจและกล้องเดิมเสียประเภท"""
    p = PRESETS[key]
    current = [str(t) for t in (settings_store.load().get("area_types") or [])]
    types = list(p["area_types"]) + [t for t in current if t not in p["area_types"]]
    values = dict(p["settings"], area_types=types)
    settings_store.save(db, values)
    return values


def apply(db, key: str, user, rules: bool = True, settings: bool = True) -> dict:
    if key not in PRESETS:
        raise KeyError(key)
    out = dict(checks=0, settings=0)
    if rules:
        out["checks"] = apply_rules(db, key)
        rev = int(settings_store.load().get("_rule_rev", 0) or 0) + 1
        settings_store.save(db, {"_rule_rev": rev})
        log(db, user, "reset_checkpoints", f"ฉบับที่ {rev}: ใช้ชุดตั้งค่า {PRESETS[key]['name']} {out['checks']} ข้อ")
    if settings:
        out["settings"] = len(apply_settings(db, key))
    settings_store.save(db, {"preset": key})
    log(db, user, "apply_preset", f"{PRESETS[key]['name']}: " + ", ".join(
        x for x, on in (("หมวดและรายการตรวจ", rules), ("การตั้งค่าที่แนะนำ", settings)) if on))
    db.commit()
    return out

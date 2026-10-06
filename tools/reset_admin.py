#!/usr/bin/env python3
"""ตั้งรหัสผ่านใหม่ให้บัญชีผู้ดูแล เมื่อลืมรหัสและไม่มีผู้ดูแลคนอื่นช่วยตั้งให้

รันจากโฟลเดอร์หลักของโปรแกรม (โฟลเดอร์ที่มี app/)
    ฐานข้อมูลในเครื่อง:   python tools/reset_admin.py admin รหัสใหม่
    ฐานข้อมูลบน Neon:     ตั้งตัวแปร DATABASE_URL เป็น connection string ของ Neon ก่อน แล้วรันคำสั่งเดียวกัน
                          Windows:  set DATABASE_URL=postgresql://...   (ในหน้าต่าง cmd เดียวกัน)
ระบบจะบังคับให้ตั้งรหัสใหม่อีกครั้งตอนเข้าใช้ครั้งแรก
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    os.environ["DISABLE_WORKER"] = "1"
    from app import db, security
    username, password = argv[1].strip().lower(), argv[2]
    problem = security.password_problem(password)
    if problem:
        print(problem)
        return 2
    db.init_db()
    with db.SessionLocal() as s:
        user = s.query(db.User).filter(db.User.username == username).first()
        if user is None:
            print(f"ไม่พบผู้ใช้ {username}")
            return 1
        user.password_hash = security.hash_password(password)
        user.must_change, user.active = True, True
        db.log(s, "system", "reset_password", f"ตั้งรหัสผ่านใหม่ให้ {username} ด้วย tools/reset_admin.py")
        s.commit()
    print(f"ตั้งรหัสผ่านใหม่ให้ {username} แล้ว เข้าสู่ระบบแล้วระบบจะให้ตั้งรหัสของคุณเองอีกครั้ง")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

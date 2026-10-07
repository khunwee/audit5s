"""ตรวจและบีบอัดภาพก่อนเก็บ: หมุนตาม EXIF, ลบข้อมูล EXIF/GPS, ย่อขนาด, ทำภาพย่อ"""
import hashlib
import io

from PIL import Image, ImageDraw, ImageFont, ImageOps

Image.MAX_IMAGE_PIXELS = 60_000_000
_ALLOWED = {"JPEG", "PNG", "WEBP", "MPO"}

# ภาพ HEIC/HEIF ของมือถือ: iPhone แปลงเป็น JPEG ให้เองตอนส่งผ่านเว็บ และหน้าเว็บของระบบแปลงซ้ำอีกชั้นก่อนส่ง
# ไฟล์ HEIC จึงมาถึงเซิร์ฟเวอร์เฉพาะเมื่อเบราว์เซอร์แปลงไม่ได้ (เช่น เลือกภาพ HEIF จากคลังภาพบน Android)
# ถ้าติดตั้งแพ็กเกจ pillow-heif ไว้ เซิร์ฟเวอร์จะเปิดไฟล์แบบนี้ได้เอง ถ้าไม่ได้ติดตั้ง จะบอกผู้ใช้ว่าต้องทำอย่างไร
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF = True
    _ALLOWED |= {"HEIF", "HEIC"}
except Exception:                                    # ไม่ได้ติดตั้ง หรือเครื่องนี้ใช้ไม่ได้: ระบบทำงานต่อได้ตามปกติ
    HEIF = False
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"heif")


def looks_heif(raw: bytes) -> bool:
    return len(raw) > 12 and raw[4:8] == b"ftyp" and raw[8:12] in _HEIF_BRANDS


HEIF_HINT = ("ภาพนี้เป็นไฟล์ HEIC ซึ่งเบราว์เซอร์ของเครื่องนี้แปลงเป็น JPEG ไม่ได้ ใช้ปุ่ม ถ่ายภาพ แทนการเลือกจากคลังภาพ "
             "หรือตั้งกล้องของมือถือให้บันทึกเป็น JPEG")


class ImageError(Exception):
    pass


def _jpeg(img: Image.Image, side: int, quality: int) -> bytes:
    im = img.copy()
    im.thumbnail((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def process(raw: bytes, max_side: int = 1280, quality: int = 78) -> dict:
    if not raw:
        raise ImageError("ไฟล์ว่าง")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        probe.verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        if looks_heif(raw) and not HEIF:
            raise ImageError(HEIF_HINT)
        raise ImageError("เปิดไฟล์ภาพไม่ได้ รองรับ JPG, PNG และ WEBP")
    if fmt not in _ALLOWED:
        raise ImageError(HEIF_HINT if looks_heif(raw) else "รองรับเฉพาะไฟล์ภาพ JPG, PNG และ WEBP")
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    if min(img.size) < 200:
        raise ImageError("ภาพเล็กเกินไป ถ่ายใหม่ให้เห็นพื้นที่ชัดเจน")
    max_side = max(640, min(int(max_side), 2400))
    quality = max(50, min(int(quality), 92))
    full = _jpeg(img, max_side, quality)
    thumb = _jpeg(img, 360, 62)
    out = Image.open(io.BytesIO(full))
    return dict(image=full, thumb=thumb, width=out.width, height=out.height,
                sha256=hashlib.sha256(raw).hexdigest())


def draw_zones(image: bytes, zones: list) -> bytes:
    """วาดกรอบโซนพร้อมป้ายชื่อลงบนสำเนาของภาพที่จะส่งให้ AI (ภาพที่เก็บไว้เป็นหลักฐานไม่ถูกแก้)

    zones = [(ป้าย, [x1, y1, x2, y2])] พิกัด 0-1000 เทียบกับความกว้างและความสูงของภาพ
    """
    if not zones:
        return image
    im = Image.open(io.BytesIO(image)).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    line = max(3, w // 260)
    try:
        font = ImageFont.load_default(size=max(18, w // 42))
    except TypeError:
        font = ImageFont.load_default()
    for label, (x1, y1, x2, y2) in zones:
        box = [x1 * w / 1000, y1 * h / 1000, x2 * w / 1000, y2 * h / 1000]
        d.rectangle(box, outline=(0, 90, 255), width=line)
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0] + 12, tb[3] - tb[1] + 10
        d.rectangle([box[0], box[1], box[0] + tw, box[1] + th], fill=(0, 90, 255))
        d.text((box[0] + 6, box[1] + 3 - tb[1] / 2), label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()

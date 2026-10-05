"""ตรวจและบีบอัดภาพก่อนเก็บ: หมุนตาม EXIF, ลบข้อมูล EXIF/GPS, ย่อขนาด, ทำภาพย่อ"""
import hashlib
import io

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 60_000_000
_ALLOWED = {"JPEG", "PNG", "WEBP", "MPO"}


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
        raise ImageError("เปิดไฟล์ภาพไม่ได้ รองรับ JPG, PNG และ WEBP")
    if fmt not in _ALLOWED:
        raise ImageError("รองรับเฉพาะไฟล์ภาพ JPG, PNG และ WEBP")
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

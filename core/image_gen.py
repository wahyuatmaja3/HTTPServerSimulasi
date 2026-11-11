import io
import os
from datetime import datetime
from PIL import Image, ImageDraw


def generate_attendance_jpeg(kode: str, user_id: str, timestamp: datetime, custom_image_path: str = "") -> bytes:
    """Generate or load a compressed 480x640 JPEG similar to Flutter TakePicture.dart (quality=40)."""
    if custom_image_path and os.path.isfile(custom_image_path):
        try:
            with Image.open(custom_image_path) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail((480, 640))
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=40)
                return buf.getvalue()
        except Exception:
            pass

    img = Image.new("RGB", (480, 640), color=(34, 42, 54))
    draw = ImageDraw.Draw(img)
    # Draw simple avatar silhouette & simulation badge
    draw.rectangle([20, 20, 460, 620], outline=(76, 175, 145), width=3)
    draw.ellipse([170, 140, 310, 280], fill=(95, 115, 140))
    draw.pieslice([110, 290, 370, 520], start=180, end=360, fill=(95, 115, 140))
    draw.rectangle([30, 535, 450, 605], fill=(20, 25, 33))
    label = f"SIM | Kode: {kode} | User: {user_id}\n{timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
    draw.text((44, 550), label, fill=(230, 240, 250))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=40)
    return buf.getvalue()

"""
画像処理ユーティリティ
- HEIC/PNG/JPEG等あらゆる形式をJPEGに変換
- 長辺1200px以内にリサイズ
- JPEG品質80%で圧縮
"""
import base64
import io
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image

# アップロードディレクトリ（claude_client.py / main.py から共用）
_UPLOAD_DIR = Path(__file__).parent / "uploads" / "meal_images"

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # pillow-heif未インストール時はHEIC非対応のまま

MAX_LONG_SIDE = 1200
JPEG_QUALITY = 80


def save_image_to_fs(image_bytes: bytes) -> str:
    """JPEG画像バイト列をファイルシステムに保存し、プロジェクトルート相対パスを返す。

    保存先: uploads/meal_images/YYYY/MM/<uuid>.jpg
    戻り値例: "uploads/meal_images/2026/03/abcd1234-....jpg"
    """
    now = datetime.now()
    sub_dir = _UPLOAD_DIR / f"{now.year:04d}" / f"{now.month:02d}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4()}.jpg"
    file_path = sub_dir / filename
    file_path.write_bytes(image_bytes)
    return file_path.relative_to(_UPLOAD_DIR.parent.parent).as_posix()


def process_image_b64(b64_data: str) -> str:
    """
    Base64エンコードされた画像を受け取り、
    JPEG変換・リサイズ後のBase64文字列を返す。
    """
    raw = base64.b64decode(b64_data)
    img = Image.open(io.BytesIO(raw))

    # RGBに変換（HEIC・RGBA・P等に対応）
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 長辺が1200pxを超える場合はリサイズ
    w, h = img.size
    if max(w, h) > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # JPEG圧縮してBase64に変換
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode()

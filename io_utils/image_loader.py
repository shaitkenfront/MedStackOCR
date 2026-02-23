from __future__ import annotations

from pathlib import Path

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}


def list_images(input_dir: str) -> list[Path]:
    base = Path(input_dir)
    if not base.exists():
        return []
    files = [p for p in base.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS]
    return sorted(files)


def get_image_size(image_path: str) -> tuple[int, int]:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return (1000, 1000)

    try:
        with Image.open(image_path) as image:
            width, height = image.size
        return int(width), int(height)
    except Exception:
        return (1000, 1000)

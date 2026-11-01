"""LINE のリッチメニュー画像（2500x843、横4分割）を作る。

    python infra/cdk/rich_menu/make_image.py [--font C:/Windows/Fonts/BIZ-UDGothicB.ttc]

ボタンの並びは handler.py の MENU_AREAS と同じ順にすること。
フォントの既定値は BIZ UDゴシック（SIL Open Font License）。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 2500, 843
COLUMNS = 4
TILE = WIDTH // COLUMNS
OUTPUT = Path(__file__).resolve().parent / "rich_menu.png"

BACKGROUND = "#FFFFFF"
DIVIDER = "#D8DDE3"
ACCENT = "#06C755"
TEXT = "#1F2328"
MUTED = "#5F6B7A"

TILES = (
    ("明細を見る", "確認・修正・手入力", "list"),
    ("今年の医療費", "合計を返信します", "yen"),
    ("申告資料", "確定申告用のExcel", "document"),
    ("使い方", "ヘルプ", "help"),
)


def draw_icon(draw: ImageDraw.ImageDraw, kind: str, cx: int, cy: int, font_path: str) -> None:
    size = 150
    stroke = 16
    if kind == "list":
        for index in range(3):
            y = cy - 55 + index * 55
            draw.ellipse((cx - 85, y - 12, cx - 61, y + 12), fill=ACCENT)
            draw.rounded_rectangle((cx - 40, y - 9, cx + 85, y + 9), radius=9, fill=ACCENT)
    elif kind == "yen":
        draw.ellipse((cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2), outline=ACCENT, width=stroke)
        font = ImageFont.truetype(font_path, 100)
        draw.text((cx, cy + 4), "¥", font=font, fill=ACCENT, anchor="mm")
    elif kind == "document":
        left, top, right, bottom = cx - 58, cy - 75, cx + 58, cy + 75
        fold = 38
        draw.polygon(
            [(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)],
            outline=ACCENT,
            width=stroke,
        )
        draw.line((right - fold, top, right - fold, top + fold, right, top + fold), fill=ACCENT, width=stroke // 2)
        for index in range(3):
            y = cy - 15 + index * 32
            draw.line((left + 28, y, right - 28, y), fill=ACCENT, width=10)
    elif kind == "help":
        draw.ellipse((cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2), outline=ACCENT, width=stroke)
        font = ImageFont.truetype(font_path, 100)
        draw.text((cx, cy + 2), "?", font=font, fill=ACCENT, anchor="mm")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", default="C:/Windows/Fonts/BIZ-UDGothicB.ttc")
    args = parser.parse_args()

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(args.font, 104)
    sub_font = ImageFont.truetype(args.font, 50)
    for index, (title, sub, icon) in enumerate(TILES):
        left = index * TILE
        cx = left + TILE // 2
        if index:
            draw.line((left, 90, left, HEIGHT - 90), fill=DIVIDER, width=6)
        draw_icon(draw, icon, cx, 250, args.font)
        size = title_font.getlength(title)
        font = title_font if size <= TILE - 60 else ImageFont.truetype(args.font, math.floor(104 * (TILE - 60) / size))
        draw.text((cx, 520), title, font=font, fill=TEXT, anchor="mm")
        draw.text((cx, 640), sub, font=sub_font, fill=MUTED, anchor="mm")
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=DIVIDER, width=4)
    image.save(OUTPUT, optimize=True)
    print(f"saved {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

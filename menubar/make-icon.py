#!/usr/bin/env python3
"""生成 ZUsage.app 的应用图标（蓝色圆角方块 + 白色闪电，macOS 风格）。

输出 menubar/AppIcon.icns（build.sh 会拷进 bundle 并在 Info.plist 引用）。
需要 Pillow；改完图标重新生成后跑一次 menubar/build.sh 即可。
用法：python3 menubar/make-icon.py
"""
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "AppIcon.icns"
S = 1024          # 画布
INSET = 100       # macOS 图标四周留白
RADIUS = 185      # squircle 圆角
TOP = (110, 168, 254)    # 渐变顶色
BOTTOM = (10, 98, 204)   # 渐变底色

# 闪电多边形（居中偏上，视觉重心校正）
BOLT = [(568, 150), (300, 585), (465, 585), (415, 880),
        (735, 425), (565, 425), (690, 150)]


def rounded_gradient() -> Image.Image:
    grad = Image.new("RGBA", (S, S))
    px = grad.load()
    y0, y1 = INSET, S - INSET
    for y in range(y0, y1):
        t = (y - y0) / (y1 - y0)
        r = int(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
        g = int(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
        b = int(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
        for x in range(INSET, S - INSET):
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (INSET, INSET, S - INSET, S - INSET), radius=RADIUS, fill=255)
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.paste(grad, (0, 0), mask)
    return canvas


def main() -> None:
    icon = rounded_gradient()
    d = ImageDraw.Draw(icon)
    d.polygon(BOLT, fill=(255, 255, 255, 255))
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale, name in ((1, f"icon_{size}x{size}.png"),
                                (2, f"icon_{size}x{size}@2x.png")):
                icon.resize((size * scale, size * scale), Image.LANCZOS).save(
                    iconset / name)
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(OUT)], check=True)
    print("saved:", OUT)


if __name__ == "__main__":
    main()

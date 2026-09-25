"""灵动岛的逐帧绘制，移植自 voice-dictate 的 Look（dictate.cs）。

视觉取自 claude-directory 的 Lovable 输入框（3d-games/lovable-webgl-hero）：深色玻璃
胶囊 + 三层投影 + 旋转渐变圆片；聆听时的音量条移植自 react-bits 的 SlicedWaves，
「识别中…」的扫光移植自 react-bits 的 ShinyText。常量与 dictate.cs 一一对应，
改动时两边对照着看。

性能：胶囊底板（投影 + 玻璃面 + 描边）只按最大宽度渲染一次，其它宽度用
「左帽 + 中段拉伸 + 右帽」切出来 —— 渐变只沿垂直方向，切片与重画等价。
每帧只画圆片、音量条和文字。
"""
import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

H = 48                      # 胶囊高度（96dpi 逻辑像素）
M = 36                      # 四周透明留白，放投影
CHIP_X, CHIP_R = 24, 16
CONTENT_X, GAP, PAD_RIGHT = 52, 14, 20
METER_W, METER_H, SLAT_GAP = 120, 26, 1.5
TEXT_MAX = 560              # 识别结果最多显示这么宽，超出截断
W_MAX = CONTENT_X + TEXT_MAX + PAD_RIGHT
SS = 3                      # 圆片和音量条的超采样倍数

FONT_UI = (r"C:\Windows\Fonts\msyh.ttc", 1)          # Microsoft YaHei UI
FONT_ICON = (r"C:\Windows\Fonts\SegoeIcons.ttf", 0)  # Segoe Fluent Icons
MIC, CHECK, WARN = "\ue720", "\ue73e", "\ue7ba"
TITLE_PX, HINT_PX, ICON_PX = 14, 12, 14

ORANGE, INDIGO = np.array([255, 102, 14]), np.array([100, 106, 237])


@lru_cache(maxsize=16)
def font(spec: tuple, px: int):
    try:
        return ImageFont.truetype(spec[0], px, index=spec[1])
    except OSError:
        return ImageFont.load_default()


def text_width(s: str, px: int) -> float:
    return ImageDraw.Draw(Image.new("L", (1, 1))).textlength(s, font=font(FONT_UI, px))


def width_for(kind: str, text: str, hint: str) -> float:
    """各状态的胶囊宽度（逻辑像素），公式同 Look.WidthFor。"""
    hint_w = text_width(hint, HINT_PX) if hint else 0
    if kind == "listening":
        return CONTENT_X + METER_W + GAP + hint_w + PAD_RIGHT
    text_w = min(text_width(text, TITLE_PX), TEXT_MAX) if text else 0
    if kind == "busy":
        return CONTENT_X + text_w + GAP + hint_w + PAD_RIGHT
    return CONTENT_X + text_w + PAD_RIGHT


# ---------------------------------------------------------------- 底板
def _capsule(w: int, h: int, inset: float = 0.0) -> Image.Image:
    big = Image.new("L", (w * SS, h * SS), 0)
    i = inset * SS
    ImageDraw.Draw(big).rounded_rectangle([i, i, w * SS - 1 - i, h * SS - 1 - i],
                                          radius=(h * SS - 2 * i) / 2, fill=255)
    return big.resize((w, h), Image.LANCZOS)


def _vgrad(h: int, stops: list) -> np.ndarray:
    """垂直渐变，stops = [(位置, (r,g,b,a)), ...]，返回 h×4 的 float 数组。"""
    ys = (np.arange(h) + 0.5) / h
    pos = [p for p, _ in stops]
    return np.stack([np.interp(ys, pos, [c[k] for _, c in stops]) for k in range(4)], 1)


@lru_cache(maxsize=4)
def _shell_full(scale: float) -> Image.Image:
    w, h, m = round(W_MAX * scale), round(H * scale), round(M * scale)
    cw, ch = w + 2 * m, h + 2 * m
    mask = _capsule(w, h)

    # Lovable 的三层投影，只落在胶囊外面
    outside = ImageChops.invert(Image.new("L", (cw, ch), 0))
    hole = Image.new("L", (cw, ch), 0)
    hole.paste(mask, (m, m))
    outside = ImageChops.subtract(outside, hole)
    keep = np.ones((ch, cw))
    for alpha, blur, dy in ((0.16, 8, 2), (0.14, 16, 6), (0.12, 24, 10)):
        layer = Image.new("L", (cw, ch), 0)
        layer.paste(mask, (m, m + round(dy * scale)))
        layer = layer.filter(ImageFilter.GaussianBlur(blur * scale / 2))
        keep *= 1 - alpha * np.asarray(layer) / 255
    shadow_a = (1 - keep) * np.asarray(outside) / 255
    canvas = np.zeros((ch, cw, 4))
    canvas[..., 3] = shadow_a * 255

    # 玻璃面：底色 + 垂直着色 + 上亮下暗的描边
    def over(dst, rgba, where):
        a = rgba[..., 3:4] / 255 * where[..., None]
        out_a = a + dst[..., 3:4] / 255 * (1 - a)
        rgb = (rgba[..., :3] * a + dst[..., :3] * (dst[..., 3:4] / 255) * (1 - a)) / np.maximum(out_a, 1e-6)
        return np.concatenate([rgb, out_a * 255], -1)

    body = np.zeros((h, w, 4))
    body = over(body, np.broadcast_to(np.array([43, 38, 38, 171.0]), (h, w, 4)), np.ones((h, w)))
    tint = _vgrad(h, [(0, (118, 100, 50, 59)), (0.6634, (53, 53, 56, 179)), (1, (38, 38, 39, 179))])
    body = over(body, np.broadcast_to(tint[:, None, :], (h, w, 4)), np.ones((h, w)))
    body[..., 3] *= np.asarray(mask) / 255
    ring = np.asarray(ImageChops.subtract(mask, _capsule(w, h, inset=scale))) / 255
    rim = _vgrad(h, [(0, (255, 255, 255, 82)), (1, (255, 255, 255, 13))])
    body = over(body, np.broadcast_to(rim[:, None, :], (h, w, 4)), ring)

    canvas[m:m + h, m:m + w] = over(canvas[m:m + h, m:m + w], body, np.ones((h, w)))
    return Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGBA")


def shell(width: float, scale: float) -> Image.Image:
    full = _shell_full(scale)
    m, h = round(M * scale), round(H * scale)
    w = max(h, round(width * scale))
    if w >= full.width - 2 * m:
        return full.copy()
    cap = m + h // 2 + 1
    out_w = w + 2 * m
    out = Image.new("RGBA", (out_w, full.height), (0, 0, 0, 0))
    out.paste(full.crop((0, 0, cap, full.height)), (0, 0))
    out.paste(full.crop((full.width - cap, 0, full.width, full.height)), (out_w - cap, 0))
    if out_w - 2 * cap > 0:
        strip = full.crop((cap, 0, cap + 8, full.height))
        out.paste(strip.resize((out_w - 2 * cap, full.height), Image.BILINEAR), (cap, 0))
    return out


# ---------------------------------------------------------------- 圆片
def _conic_color(deg: np.ndarray) -> np.ndarray:
    """Lovable 发送键的锥形光环：透明白 -> 白 -> #9EC7FF -> 透明。"""
    white, blue = np.array([255, 255, 255, 0.0]), np.array([158, 199, 255, 0.0])
    t1 = np.clip(deg / 60, 0, 1)[..., None]
    t2 = np.clip((deg - 60) / 60, 0, 1)[..., None]
    t3 = np.clip((deg - 120) / 80, 0, 1)[..., None]
    rgb = np.where(deg[..., None] < 60, white[:3], np.where(deg[..., None] < 120,
                   white[:3] + (blue[:3] - white[:3]) * t2, blue[:3]))
    a = np.where(deg < 60, t1[..., 0], np.where(deg < 120, 1.0, 1 - t3[..., 0])) * 255
    return np.concatenate([rgb, a[..., None]], -1)


CHIP_BOX = 44                # 圆片小图的边长（逻辑像素），留出外圈和光环


def _rgba(rgb, alpha: np.ndarray) -> Image.Image:
    h, w = alpha.shape
    arr = np.empty((h, w, 4), np.uint8)
    arr[..., :3] = rgb
    arr[..., 3] = np.clip(alpha, 0, 255)
    return Image.fromarray(arr, "RGBA")


@lru_cache(maxsize=8)
def _chip_parts(kind: str, scale: float) -> dict:
    """圆片里不随时间变化的部分，每个缩放比例只算一次：超采样画好再缩小。"""
    size = round(CHIP_BOX * scale)
    n, u = size * SS, scale * SS
    ys, xs = (np.mgrid[0:n, 0:n] + 0.5 - n / 2) / u
    d = np.hypot(xs, ys)
    r = CHIP_R

    def down(rgb, alpha):
        return _rgba(rgb, alpha * 255).resize((size, size), Image.LANCZOS)

    parts = {"size": size}
    if kind == "message":
        parts["under"] = down((111, 111, 111), (d <= r) * 38 / 255)
    else:
        parts["under"] = down((95, 126, 167), (d <= 20) * 38 / 255)
        parts["disc"] = Image.fromarray(((d <= r) * 255).astype(np.uint8)).resize((size, size), Image.LANCZOS)
        over = down((173, 208, 255), (d <= r) * np.clip((d / r - 0.55) / 0.45, 0, 1) * 51 / 255)
        over.alpha_composite(down((222, 236, 255), (np.abs(d - (r - 1.5)) <= 0.75) * np.interp(ys, [-r, r], [204, 0]) / 255))
        over.alpha_composite(down((158, 199, 255), (np.abs(d - (r - 0.5)) <= 0.5) * 1.0))
        parts["over"] = over
        fy, fx = (np.mgrid[0:size, 0:size] + 0.5 - size / 2) / scale      # 最终尺寸上的坐标
        parts["xy"] = (fx, fy)
        parts["ring"] = np.asarray(Image.fromarray(((np.abs(d - 18) <= 1) * 255).astype(np.uint8))
                                   .resize((size, size), Image.LANCZOS)) / 255
        parts["phi"] = np.degrees(np.arctan2(fy, fx))
    return parts


@lru_cache(maxsize=8)
def _glyph(kind: str, scale: float) -> Image.Image:
    size = round(CHIP_BOX * scale)
    glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ch = {"message": WARN, "notice": CHECK}.get(kind, MIC)
    color = (254, 123, 2, 255) if kind == "message" else (255, 255, 255, 255)
    ImageDraw.Draw(glyph).text((size / 2, size / 2), ch, font=font(FONT_ICON, round(ICON_PX * scale)),
                               fill=color, anchor="mm")
    return glyph


def chip(kind: str, scale: float, t: float, state_t: float) -> Image.Image:
    """以圆片中心为中心的 RGBA 小图。每帧只算旋转渐变（4s 一圈）和识别中的光环。"""
    p = _chip_parts("message" if kind == "message" else "chip", scale)
    im = p["under"].copy()
    if kind != "message":
        fx, fy = p["xy"]
        ang = math.radians(t / 4.0 % 1.0 * 360.0 - 90)
        c, s = math.cos(ang), math.sin(ang)
        k = np.clip(0.5 + (fx * c + fy * s) / (2 * CHIP_R * (abs(c) + abs(s))), 0, 1)[..., None]
        im.alpha_composite(_rgba(ORANGE + (INDIGO - ORANGE) * k, np.asarray(p["disc"], float)))
        im.alpha_composite(p["over"])
        if kind == "busy":
            rot = state_t / 1.5 % 1.0 * 360.0
            phi = (p["phi"] - (rot - 90)) % 360
            col = _conic_color(phi)
            im.alpha_composite(_rgba(col[..., :3], p["ring"] * (phi < 200) * col[..., 3]))
    im.alpha_composite(_glyph(kind, scale))
    return im


# ---------------------------------------------------------------- 音量条
def meter(scale: float, t: float, level: float) -> Image.Image:
    """react-bits SlicedWaves 单行版：每一列的横条沿正弦起伏，起伏幅度跟着音量，
    安静时是一条平静的线。"""
    columns, thickness, speed, spread = 20, 0.14, 3.2, 0.9
    c1, c2, c3 = np.array([160, 228, 255]), np.array([156, 164, 251]), np.array([255, 102, 244])
    u = scale * SS
    w, h = round(METER_W * u), round(METER_H * u)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dc, dg = ImageDraw.Draw(im), ImageDraw.Draw(glow)
    travel = 0.10 + 0.90 * level
    start, end = (0.5 - thickness / 2) * travel, (-0.5 + thickness / 2) * travel
    cell, th = METER_W / columns, thickness * METER_H
    for i in range(columns):
        mv = math.sin(t * speed + i * spread + 1.0) * 0.5 + 0.5
        cy = METER_H / 2 + (start + (end - start) * mv) * METER_H
        col = c2 + (c1 - c2) * mv
        col = col + (c3 - col) * ((i + 0.5) / columns * 0.45)
        rgb = tuple(int(v) for v in col)
        left, right = i * cell + SLAT_GAP / 2, (i + 1) * cell - SLAT_GAP / 2
        dg.rectangle([left * u, (cy - th / 2 - 2.5) * u, right * u, (cy + th / 2 + 2.5) * u], fill=rgb + (46,))
        dc.rectangle([left * u, (cy - th / 2) * u, right * u, (cy + th / 2) * u], fill=rgb + (242,))
    glow.alpha_composite(im)
    return glow.resize((round(METER_W * scale), round(METER_H * scale)), Image.LANCZOS)


# ---------------------------------------------------------------- 文字
def label(img: Image.Image, x: float, cy: float, s: str, px: int, alpha: int, maxw=None):
    f = font(FONT_UI, px)
    d = ImageDraw.Draw(img)
    if maxw and d.textlength(s, font=f) > maxw:
        while s and d.textlength(s + "…", font=f) > maxw:
            s = s[:-1]
        s += "…"
    d.text((x, cy), s, font=f, fill=(255, 255, 255, alpha), anchor="lm")
    return d.textlength(s, font=f)


def shiny(img: Image.Image, x: float, cy: float, s: str, px: int, state_t: float) -> float:
    """react-bits ShinyText：120deg 渐变 #b5b5b5 -> #fff(50%) -> #b5b5b5，每 2s 扫过一次。"""
    f = font(FONT_UI, px)
    tw = ImageDraw.Draw(img).textlength(s, font=f)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text((x, cy), s, font=f, fill=255, anchor="lm")
    x0, y0, x1, y1 = mask.getbbox() or (0, 0, 1, 1)
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(float)
    p = state_t / 2.0 % 1.0
    band_x = x - tw * (1.5 - 2 * p)
    ang = math.radians(30)
    span = 2 * tw * math.cos(ang) + (y1 - y0) * math.sin(ang)
    k = (((xs - band_x) * math.cos(ang) + (ys - y0) * math.sin(ang)) / max(span, 1)) % 1.0
    white = np.interp(k, [0, 0.35, 0.5, 0.65, 1], [0, 0, 1, 0, 0])
    v = 181 + (255 - 181) * white
    rgba = np.stack([v, v, v, np.asarray(mask)[y0:y1, x0:x1]], -1).astype(np.uint8)
    img.alpha_composite(Image.fromarray(rgba, "RGBA"), (x0, y0))
    return tw


def clip_to(img: Image.Image, width: float, scale: float) -> Image.Image:
    """宽度动画期间内容可能伸出胶囊，按胶囊形状裁掉。只用在内容层上，
    底板的投影本来就在胶囊外面。"""
    m, h = round(M * scale), round(H * scale)
    w = max(h, round(width * scale))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([m, m, m + w - 1, m + h - 1], radius=h / 2, fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return img


# ---------------------------------------------------------------- 整帧
def frame(kind: str, text: str, hint: str, width: float, scale: float,
          t: float, state_t: float, level: float) -> Image.Image:
    """kind: listening / busy / notice / message，对应 dictate.cs 的 Kind。"""
    img = shell(width, scale)
    m = M * scale
    cy = m + H * scale / 2
    content = Image.new("RGBA", img.size, (0, 0, 0, 0))
    c = chip(kind, scale, t, state_t)
    content.alpha_composite(c, (round(m + CHIP_X * scale - c.width / 2), round(cy - c.height / 2)))
    x = m + CONTENT_X * scale
    title, hint_px = round(TITLE_PX * scale), round(HINT_PX * scale)
    if kind == "listening":
        mt = meter(scale, t, level)
        content.alpha_composite(mt, (round(x), round(cy - mt.height / 2)))
        label(content, x + (METER_W + GAP) * scale, cy, hint, hint_px, 128)
    elif kind == "busy":
        w = shiny(content, x, cy, text, title, state_t)
        label(content, x + w + GAP * scale, cy, hint, hint_px, 128)
    else:
        label(content, x, cy, text, title, 235, maxw=TEXT_MAX * scale)
    img.alpha_composite(clip_to(content, width, scale))
    return img

#!/usr/bin/env python3
"""Gerador de captchas sintéticos no estilo PROEIS (6 chars hex, coloridos, com
bolinhas e linhas finas de ruído). Calibrado nas imagens reais (250x100, fundo
branco). Usado para treinar a CRNN antes/junto com os captchas reais confirmados.
"""
import os, io, math, random, glob
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 250, 100
CHARS = "0123456789ABCDEF"
FONT_DIR = "/usr/share/fonts"

def _valid_fonts():
    ok = []
    for f in glob.glob(FONT_DIR + "/**/*.ttf", recursive=True):
        if any(x in f.lower() for x in ("japanese", "mono", "emoji", "symbol")):
            continue
        try:
            ImageFont.truetype(f, 48)  # confirma que escala
            ok.append(f)
        except Exception:
            pass
    return ok

_FONTS = _valid_fonts()


def _rand_color(rng, smin=90, smax=210, vmin=120, vmax=220):
    # cor de saturação média (como os caracteres reais), via HSV->RGB
    import colorsys
    h = rng.random()
    s = rng.uniform(smin, smax) / 255.0
    v = rng.uniform(vmin, vmax) / 255.0
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def gen_captcha(rng=None):
    rng = rng or random
    text = "".join(rng.choice(CHARS) for _ in range(6))
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def draw_dots(n_lo, n_hi):
        for _ in range(rng.randint(n_lo, n_hi)):
            cx, cy = rng.uniform(0, W), rng.uniform(8, H - 8)
            r = rng.uniform(2, 10)
            col = _rand_color(rng, smin=90, smax=220, vmin=160, vmax=245)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)

    # 1) camada de bolinhas ATRÁS dos caracteres (esparsas, pequenas — como no real)
    draw_dots(12, 24)

    # 2) caracteres: SEMPRE os 6, em slots igualmente espaçados que cabem na
    #    largura (com jitter). Cada um centralizado no seu slot -> nunca some char.
    margin = 10
    slot_w = (W - 2 * margin) / 6.0  # ~38px por caractere
    for i, ch in enumerate(text):
        fsize = rng.randint(40, 54)
        font = ImageFont.truetype(rng.choice(_FONTS), fsize)
        color = _rand_color(rng)
        layer = Image.new("RGBA", (fsize + 24, fsize + 30), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        if rng.random() < 0.45:  # contorno (stroke), como muitos chars do PROEIS
            ld.text((12, 5), ch, font=font, fill=(255, 255, 255, 0),
                    stroke_width=2, stroke_fill=color + (255,))
        else:                    # preenchido, semi-translúcido
            ld.text((12, 5), ch, font=font, fill=color + (rng.randint(180, 255),))
        layer = layer.rotate(rng.uniform(-22, 22), expand=True, resample=Image.BICUBIC)
        # centro do slot i, com pequeno jitter horizontal/vertical
        cx = margin + slot_w * (i + 0.5) + rng.uniform(-4, 4)
        cy = H / 2 + rng.uniform(-6, 6)
        img.paste(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)), layer)

    # 3) poucas bolinhas por cima (algumas cobrem parcialmente os chars, como no real)
    draw_dots(8, 18)

    # 4) linhas finas cinza (retas e curvas), esparsas
    for _ in range(rng.randint(4, 10)):
        col = (rng.randint(95, 155),) * 3
        if rng.random() < 0.5:
            d.line([rng.uniform(0, W), rng.uniform(0, H), rng.uniform(0, W), rng.uniform(0, H)],
                   fill=col, width=1)
        else:
            pts = [(rng.uniform(0, W), rng.uniform(0, H)) for _ in range(3)]
            d.line(pts, fill=col, width=1, joint="curve")

    if rng.random() < 0.25:
        img = img.filter(ImageFilter.GaussianBlur(0.4))
    return img, text


def to_gray_arr(img):
    g = img.convert("L").resize((W, H))
    return np.asarray(g, dtype=np.float32) / 255.0


if __name__ == "__main__":
    os.makedirs("captcha_tests/train/samples", exist_ok=True)
    rng = random.Random(1)
    for i in range(8):
        im, t = gen_captcha(rng)
        im.save(f"captcha_tests/train/samples/syn_{i}_{t}.png")
        print("syn", t)
    print("fontes usadas:", len(_FONTS))

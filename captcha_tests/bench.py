#!/usr/bin/env python3
"""Laboratório de captcha: testa modelos Gemini × pré-processamentos × configs
contra um gabarito, medindo ACERTO REAL (string exata de 6 chars hex).

Uso:
  python captcha_tests/bench.py panel        # painel forte p/ rotular (consenso)
  python captcha_tests/bench.py grid         # grade completa modelos×preproc
  python captcha_tests/bench.py consensus N   # votação de N leituras do melhor cfg

Chama a API Gemini direto (generativelanguage). Chave em captcha_tests/gemini_key
ou env GEMINI_API_KEY.
"""
import os, sys, io, json, base64, time, re, threading
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageOps, ImageFilter

HERE = Path(__file__).parent
IMAGES = HERE / "images"
KEY = (os.getenv("GEMINI_API_KEY") or (HERE / "gemini_key").read_text().strip()
       if (HERE / "gemini_key").exists() else os.getenv("GEMINI_API_KEY", ""))

import urllib.request

HEX = set("0123456789ABCDEF")


def norm_answer(raw: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]", "", raw or "").upper()
    # confusões comuns
    trans = {"O": "0", "Q": "0", "I": "1", "L": "1", "S": "5", "G": "6", "Z": "2"}
    s = "".join(trans.get(c, c) for c in s)
    return s


def valid(ans: str) -> bool:
    return len(ans) == 6 and all(c in HEX for c in ans)


# ─────────────────────── Pré-processamentos ───────────────────────
def pp_off(b: bytes) -> bytes:
    return b


def _pil(b):
    img = Image.open(io.BytesIO(b))
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3]); img = bg
    return img.convert("RGB")


def pp_upscale(b: bytes) -> bytes:
    img = _pil(b); w, h = img.size
    img = img.resize((w * 3, h * 3), Image.LANCZOS)
    out = io.BytesIO(); img.save(out, "PNG"); return out.getvalue()


def pp_gray_contrast(b: bytes) -> bytes:
    img = _pil(b).convert("L")
    img = ImageOps.autocontrast(img, cutoff=2)
    img = img.filter(ImageFilter.SHARPEN)
    w, h = img.size; img = img.resize((w * 2, h * 2), Image.LANCZOS)
    out = io.BytesIO(); img.save(out, "PNG"); return out.getvalue()


def pp_cv2_clean(b: bytes, sat_thr=110) -> bytes:
    arr = np.frombuffer(b, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    img[sat > sat_thr] = (255, 255, 255)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.medianBlur(gray, 3)
    ok, buf = cv2.imencode(".png", gray); return buf.tobytes()


def pp_remove_lines(b: bytes) -> bytes:
    """Remove linhas finas (traços) por morfologia e mantém cor dos caracteres."""
    arr = np.frombuffer(b, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # detecta linhas escuras finas
    _, dark = cv2.threshold(gray, 130, 255, cv2.THRESH_BINARY_INV)
    horiz = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1)))
    vert = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 25)))
    lines = cv2.bitwise_or(horiz, vert)
    lines = cv2.dilate(lines, np.ones((2, 2), np.uint8))
    img[lines > 0] = (255, 255, 255)
    ok, buf = cv2.imencode(".png", img); return buf.tobytes()


def pp_sat_white_bg(b: bytes) -> bytes:
    """Bolinhas coloridas (blobs grandes saturados) viram branco; texto preservado
    por ser fino. Depois realça contraste em cinza."""
    arr = np.frombuffer(b, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    mask = (sat > 90).astype(np.uint8) * 255
    # blobs grandes = bolinhas; abre para remover traços finos de texto da máscara
    big = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    img[big > 0] = (255, 255, 255)
    ok, buf = cv2.imencode(".png", img); return buf.tobytes()


PREPROCS = {
    "off": pp_off,
    "upscale3x": pp_upscale,
    "gray_contrast": pp_gray_contrast,
    "cv2_clean": pp_cv2_clean,
    "remove_lines": pp_remove_lines,
    "sat_white_bg": pp_sat_white_bg,
}

PROMPT = (
    "Read this PROEIS CAPTCHA. It has EXACTLY 6 characters, each a hexadecimal "
    "digit: only 0 1 2 3 4 5 6 7 8 9 A B C D E F (uppercase).\n"
    "The characters are colored and overlaid with random colored dots and thin "
    "gray lines — ignore the dots and lines, read only the 6 aligned characters "
    "left to right.\n"
    "Common confusions: O/Q->0, I/L->1, S->5, G->6, Z->2, B vs 8, D vs 0.\n"
    "Answer with ONLY the 6 characters, no spaces or punctuation."
)


import subprocess, tempfile

def call_gemini(img: bytes, model: str, thinking: int = None, prompt: str = PROMPT, timeout=60):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={KEY}"
    gen = {"temperature": 0.0, "maxOutputTokens": 2048}
    if thinking is not None:
        gen["thinkingConfig"] = {"thinkingBudget": thinking}
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}},
            {"text": prompt},
        ]}],
        "generationConfig": gen,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f); path = f.name
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            ["curl", "-sS", "-X", "POST", url, "-H", "Content-Type: application/json", "--data", "@" + path],
            capture_output=True, text=True, timeout=timeout,
        )
        j = json.loads(p.stdout) if p.stdout.strip() else {}
        if "error" in j:
            return {"raw": "", "ans": "", "s": round(time.monotonic() - t0, 2), "err": j["error"].get("status", "ERR")}
        cands = j.get("candidates", [])
        txt = ""
        if cands:
            for part in cands[0].get("content", {}).get("parts", []):
                if "text" in part:
                    txt += part["text"]
        return {"raw": txt.strip(), "ans": norm_answer(txt), "s": round(time.monotonic() - t0, 2), "err": None}
    except Exception as e:
        return {"raw": "", "ans": "", "s": round(time.monotonic() - t0, 2), "err": str(e)[:120]}
    finally:
        try: os.unlink(path)
        except OSError: pass


def load_images():
    return sorted([p for p in IMAGES.glob("*.png")])


def run_configs(configs, images, label):
    """configs: list of dict(model, preproc, thinking, name). Roda em paralelo por imagem."""
    results = {}  # name -> {img: result}
    lock = threading.Lock()

    def work(cfg, imgpath):
        b = imgpath.read_bytes()
        pb = PREPROCS[cfg["preproc"]](b)
        res = call_gemini(pb, cfg["model"], cfg.get("thinking"), cfg.get("prompt", PROMPT))
        with lock:
            results.setdefault(cfg["name"], {})[imgpath.name] = res

    threads = []
    for cfg in configs:
        for imgpath in images:
            t = threading.Thread(target=work, args=(cfg, imgpath))
            threads.append(t)
    # limita concorrência
    running = []
    for t in threads:
        t.start(); running.append(t)
        while sum(1 for x in running if x.is_alive()) >= 12:
            time.sleep(0.05)
    for t in threads:
        t.join()
    return results


if __name__ == "__main__":
    print("chave len:", len(KEY), "| imagens:", len(load_images()))

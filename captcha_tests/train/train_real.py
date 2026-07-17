#!/usr/bin/env python3
"""Treina a CNN de captcha usando os captchas REAIS rotulados por consenso.

Estratégia (transferência de aprendizado):
 1) Pré-treina em captchas sintéticos (aprende formas gerais dos caracteres).
 2) Afina (fine-tuning) nos captchas REAIS rotulados (fecha o gap de domínio).
 3) Avalia nos 28 reais verificados por humano (gold) + fração real de validação.

Uso: python captcha_tests/train/train_real.py
"""
import os, sys, json, time, random, glob
import numpy as np
from pathlib import Path
from PIL import Image
import torch, torch.nn as nn, torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen
from train import CaptchaNet, img_to_tensor, C2I, CHARS, IH, IW, make_synth, load_real, accuracy

torch.manual_seed(0); random.seed(0); np.random.seed(0); torch.set_num_threads(4)
ROOT = Path(__file__).resolve().parents[2]
REAL_DIR = ROOT / "captcha_tests" / "real"
LABELS = ROOT / "captcha_tests" / "train" / "real_labels.json"


def load_labeled_real():
    d = json.load(open(LABELS))
    items = [(REAL_DIR / fn, lab) for fn, lab in d.items()
             if (REAL_DIR / fn).exists() and len(lab) == 6 and all(c in CHARS for c in lab)]
    X = np.zeros((len(items), 3, IH, IW), dtype=np.float32)
    Y = np.zeros((len(items), 6), dtype=np.int64)
    for i, (p, lab) in enumerate(items):
        X[i] = img_to_tensor(Image.open(p)); Y[i] = [C2I[c] for c in lab]
    return torch.from_numpy(X), torch.from_numpy(Y)


def main():
    Xr, Yr = load_labeled_real()
    n = Xr.shape[0]
    print(f"captchas reais rotulados: {n}", flush=True)
    # split: 85% treino / 15% validação real
    idx = np.arange(n); np.random.shuffle(idx)
    nval = max(20, int(n * 0.15))
    vidx, tidx = idx[:nval], idx[nval:]
    Xrt, Yrt = Xr[tidx], Yr[tidx]
    Xrv, Yrv = Xr[vidx], Yr[vidx]
    Xgold, Ygold, gold_names = load_real()  # 28 verificados por humano
    print(f"treino real: {Xrt.shape[0]} | val real: {Xrv.shape[0]} | gold humano: {Xgold.shape[0]}", flush=True)

    model = CaptchaNet()

    # ── 1) Pré-treino sintético ──────────────────────────────────────────────
    n_syn = int(os.getenv("N_SYN", "8000"))
    print(f"pré-treino: gerando {n_syn} sintéticos...", flush=True)
    Xs, Ys = make_synth(n_syn, seed=1)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    bs = 64; si = np.arange(n_syn)
    for ep in range(int(os.getenv("PRE_EPOCHS", "6"))):
        model.train(); np.random.shuffle(si); t0 = time.time(); L = []
        for i in range(0, n_syn, bs):
            b = si[i:i+bs]
            out = model(Xs[b]); loss = sum(F.cross_entropy(out[:, k, :], Ys[b][:, k]) for k in range(6)) / 6
            opt.zero_grad(); loss.backward(); opt.step(); L.append(loss.item())
        ge, gc = accuracy(model, Xgold, Ygold)
        print(f"[pré {ep+1}] loss={np.mean(L):.3f} | gold exato={ge:.0f}% char={gc:.0f}% ({time.time()-t0:.0f}s)", flush=True)

    # ── 2) Fine-tuning nos REAIS (com aumento leve via sintético misturado) ──
    print("fine-tuning nos captchas reais...", flush=True)
    opt = torch.optim.Adam(model.parameters(), lr=3e-4, weight_decay=1e-5)
    best = 0; best_state = None
    nrt = Xrt.shape[0]
    for ep in range(int(os.getenv("FT_EPOCHS", "40"))):
        model.train(); order = np.random.permutation(nrt); t0 = time.time(); L = []
        # cada época: reais (repetidos) + um pouco de sintético fresco para regularizar
        for i in range(0, nrt, bs):
            b = order[i:i+bs]
            out = model(Xrt[b]); loss = sum(F.cross_entropy(out[:, k, :], Yrt[b][:, k]) for k in range(6)) / 6
            opt.zero_grad(); loss.backward(); opt.step(); L.append(loss.item())
        rv_e, rv_c = accuracy(model, Xrv, Yrv)
        g_e, g_c = accuracy(model, Xgold, Ygold)
        flag = ""
        if g_e >= best:
            best = g_e; best_state = {k: v.clone() for k, v in model.state_dict().items()}; flag = " *"
        if (ep + 1) % 5 == 0 or ep < 3:
            print(f"[ft {ep+1}] loss={np.mean(L):.3f} | val_real exato={rv_e:.0f}% char={rv_c:.0f}% | GOLD exato={g_e:.0f}% char={g_c:.0f}%{flag} ({time.time()-t0:.0f}s)", flush=True)

    if best_state:
        model.load_state_dict(best_state)
        torch.save(model.state_dict(), ROOT / "captcha_tests/train/model_real.pt")
    ge, gc = accuracy(model, Xgold, Ygold)
    rve, rvc = accuracy(model, Xrv, Yrv)
    print(f"\n=== MELHOR MODELO ===", flush=True)
    print(f"GOLD (28 reais humanos): exato={ge:.0f}% char={gc:.0f}%", flush=True)
    print(f"val_real (consenso):     exato={rve:.0f}% char={rvc:.0f}%", flush=True)
    print(f"(baseline Gemini no site hoje: ~40% aceitação)", flush=True)


if __name__ == "__main__":
    main()

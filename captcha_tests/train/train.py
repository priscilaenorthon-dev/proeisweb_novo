#!/usr/bin/env python3
"""Treina uma CNN para ler os captchas do PROEIS (6 chars hex).

Estratégia: pré-treina em captchas SINTÉTICOS (gen.py, estilo PROEIS) e valida nos
captchas REAIS confirmados (ground_truth*.json). Conforme o dataset real cresce
(coletado em produção via /api/captcha-dataset), pode-se re-treinar afinando neles.

CPU-friendly. Salva o modelo em captcha_tests/train/model.pt e exporta ONNX.
"""
import os, sys, json, time, random, io
import numpy as np
from pathlib import Path
from PIL import Image
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen

torch.manual_seed(0); random.seed(0); np.random.seed(0)
torch.set_num_threads(4)
ROOT = Path(__file__).resolve().parents[2]
IH, IW = 64, 160
CHARS = gen.CHARS
C2I = {c: i for i, c in enumerate(CHARS)}


def img_to_tensor(pil):
    pil = pil.convert("RGB").resize((IW, IH), Image.BILINEAR)
    a = np.asarray(pil, dtype=np.float32) / 255.0
    return np.transpose(a, (2, 0, 1))  # C,H,W


def make_synth(n, seed):
    rng = random.Random(seed)
    X = np.zeros((n, 3, IH, IW), dtype=np.float32)
    Y = np.zeros((n, 6), dtype=np.int64)
    for i in range(n):
        im, txt = gen.gen_captcha(rng)
        X[i] = img_to_tensor(im)
        Y[i] = [C2I[c] for c in txt]
        if (i + 1) % 2000 == 0:
            print(f"  gerados {i+1}/{n}", flush=True)
    return torch.from_numpy(X), torch.from_numpy(Y)


def load_real():
    """Carrega os 28 captchas reais rotulados (gabarito de consenso)."""
    gts = {}
    for gf, folder in [("ground_truth.json", "images"), ("ground_truth_val.json", "images_val")]:
        p = ROOT / "captcha_tests" / gf
        if not p.exists():
            continue
        d = json.load(open(p))
        for fn, meta in d.items():
            label = meta["label"] if isinstance(meta, dict) else meta
            img = ROOT / "captcha_tests" / folder / fn
            if img.exists() and len(label) == 6 and all(c in CHARS for c in label):
                gts[str(img)] = label
    X = np.zeros((len(gts), 3, IH, IW), dtype=np.float32)
    Y = np.zeros((len(gts), 6), dtype=np.int64)
    names = []
    for i, (path, label) in enumerate(gts.items()):
        X[i] = img_to_tensor(Image.open(path))
        Y[i] = [C2I[c] for c in label]
        names.append((os.path.basename(path), label))
    return torch.from_numpy(X), torch.from_numpy(Y), names


class CaptchaNet(nn.Module):
    """CNN que preserva a posição horizontal: as 6 colunas do mapa de features
    viram os 6 caracteres (cada coluna -> 1 char). Generaliza muito melhor que
    average pooling global."""
    def __init__(self, nchars=6, nclasses=16):
        super().__init__()
        def blk(i, o):
            return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o),
                                 nn.ReLU(), nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o),
                                 nn.ReLU(), nn.MaxPool2d(2))
        self.features = nn.Sequential(blk(3, 32), blk(32, 64), blk(64, 128), blk(128, 256))
        # 64x160 -> /16 -> 4x10. Reduz altura a 1 e largura a 6 (6 slots de char).
        self.col_pool = nn.AdaptiveAvgPool2d((1, nchars))       # -> B,256,1,6
        self.classifier = nn.Sequential(
            nn.Conv2d(256, 256, 1), nn.BatchNorm2d(256), nn.ReLU(), nn.Dropout(0.3),
            nn.Conv2d(256, nclasses, 1),                        # -> B,16,1,6
        )
        self.nchars, self.nclasses = nchars, nclasses

    def forward(self, x):
        x = self.features(x)
        x = self.col_pool(x)
        x = self.classifier(x)              # B,16,1,6
        x = x.squeeze(2).permute(0, 2, 1)   # B,6,16
        return x


def accuracy(model, X, Y, bs=64):
    model.eval()
    exact = 0; charhit = 0; total = X.shape[0]
    with torch.no_grad():
        for i in range(0, total, bs):
            xb = X[i:i+bs]; yb = Y[i:i+bs]
            out = model(xb)                # B,6,16
            pred = out.argmax(-1)          # B,6
            charhit += (pred == yb).sum().item()
            exact += (pred == yb).all(1).sum().item()
    return exact / total * 100, charhit / (total * 6) * 100


def main():
    n_train = int(os.getenv("N_TRAIN", "12000"))
    n_val = 1500
    epochs = int(os.getenv("EPOCHS", "16"))
    print(f"Gerando {n_train} sintéticos de treino...", flush=True)
    Xtr, Ytr = make_synth(n_train, seed=1)
    print(f"Gerando {n_val} sintéticos de validação...", flush=True)
    Xva, Yva = make_synth(n_val, seed=999)
    Xreal, Yreal, real_names = load_real()
    print(f"Reais rotulados p/ teste: {Xreal.shape[0]}", flush=True)

    model = CaptchaNet()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    bs = 64
    idx = np.arange(n_train)
    best_real = 0
    for ep in range(epochs):
        model.train(); np.random.shuffle(idx)
        t0 = time.time(); losses = []
        for i in range(0, n_train, bs):
            b = idx[i:i+bs]
            xb = Xtr[b]; yb = Ytr[b]
            out = model(xb)
            loss = sum(F.cross_entropy(out[:, k, :], yb[:, k]) for k in range(6)) / 6
            opt.zero_grad(); loss.backward(); opt.step()
            losses.append(loss.item())
        sched.step()
        va_exact, va_char = accuracy(model, Xva, Yva)
        re_exact, re_char = accuracy(model, Xreal, Yreal)
        print(f"ep {ep+1:2d}/{epochs} loss={np.mean(losses):.3f} "
              f"| synth val: exato={va_exact:.0f}% char={va_char:.0f}% "
              f"| REAL: exato={re_exact:.0f}% char={re_char:.0f}% "
              f"({time.time()-t0:.0f}s)", flush=True)
        if re_exact >= best_real:
            best_real = re_exact
            torch.save(model.state_dict(), ROOT / "captcha_tests/train/model.pt")

    print(f"\nMelhor acerto EXATO em captchas reais: {best_real:.0f}%")
    # detalhe por imagem real
    model.load_state_dict(torch.load(ROOT / "captcha_tests/train/model.pt"))
    model.eval()
    with torch.no_grad():
        pred = model(Xreal).argmax(-1)
    print("\nPredições reais (modelo próprio):")
    for i, (fn, label) in enumerate(real_names):
        p = "".join(CHARS[c] for c in pred[i].tolist())
        print(f"  {fn:18s} gabarito={label} previu={p} {'OK' if p==label else 'x'}")


if __name__ == "__main__":
    main()

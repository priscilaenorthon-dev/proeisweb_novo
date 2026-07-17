#!/usr/bin/env python3
"""Exporta o modelo treinado (model.pt) para ONNX, para servir em produção com
onnxruntime (leve, sem depender do PyTorch no container)."""
import sys, os
from pathlib import Path
import torch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train import CaptchaNet, IH, IW

ROOT = Path(__file__).resolve().parents[2]
model = CaptchaNet()
model.load_state_dict(torch.load(ROOT / "captcha_tests/train/model_real.pt", map_location="cpu"))
model.eval()
dummy = torch.zeros(1, 3, IH, IW)
out = ROOT / "captcha_tests/train/model.onnx"
torch.onnx.export(
    model, dummy, str(out),
    input_names=["image"], output_names=["logits"],
    dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
    opset_version=17,
    dynamo=True,
)
print("ONNX exportado:", out, f"({out.stat().st_size//1024} KB)")

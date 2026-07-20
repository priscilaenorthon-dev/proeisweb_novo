import os, sys, json, glob
import numpy as np
from pathlib import Path
from PIL import Image
import torch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from train import CaptchaNet, img_to_tensor, C2I, CHARS, IH, IW, load_real, accuracy
ROOT = Path(__file__).resolve().parents[2]
REAL_DIR = ROOT/"captcha_tests"/"real"; LABELS=ROOT/"captcha_tests"/"train"/"real_labels.json"
d=json.load(open(LABELS))
items=[(REAL_DIR/fn,lab) for fn,lab in d.items() if (REAL_DIR/fn).exists() and len(lab)==6 and all(c in CHARS for c in lab)]
n=len(items); X=np.zeros((n,3,IH,IW),np.float32); Y=np.zeros((n,6),np.int64)
for i,(p,lab) in enumerate(items):
    X[i]=img_to_tensor(Image.open(p)); Y[i]=[C2I[c] for c in lab]
X=torch.from_numpy(X); Y=torch.from_numpy(Y)
# mesmo split do treino (seed 0)
np.random.seed(0); idx=np.arange(n); np.random.shuffle(idx)
nval=max(20,int(n*0.15)); vidx=idx[:nval]
Xv,Yv=X[vidx],Y[vidx]
Xg,Yg,_=load_real()
def ev(path):
    m=CaptchaNet(); m.load_state_dict(torch.load(path,map_location="cpu")); m.eval()
    ge,gc=accuracy(m,Xg,Yg); ve,vc=accuracy(m,Xv,Yv)
    return ge,gc,ve,vc
for name,path in [("ANTIGO (prev)","captcha_tests/train/model_real.prev.pt"),("NOVO","captcha_tests/train/model_real.pt")]:
    if not os.path.exists(path): print(name,"-> ausente"); continue
    ge,gc,ve,vc=ev(path)
    print(f"{name:16s} | GOLD exato={ge:.1f}% char={gc:.1f}% | val_real({len(vidx)}) exato={ve:.1f}% char={vc:.1f}%")

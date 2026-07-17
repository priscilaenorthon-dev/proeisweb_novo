#!/usr/bin/env python3
"""Rotula captchas REAIS coletados usando consenso de modelos Gemini diversos.
Onde >=3 de 4 leituras concordam na mesma string de 6 hex, o rótulo é aceito
(quase certamente correto). Gera real_labels.json para treinar a CNN nos reais.
"""
import os, sys, json, time, threading, glob
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # captcha_tests
import bench  # usa call_gemini via curl + PREPROCS + valid/norm

REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "real")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_labels.json")

# painel diverso de modelos CONFIÁVEIS (evita 503 dos preview/pro)
PANEL = [
    ("gemini-2.5-flash", "off"),
    ("gemini-2.5-flash-lite", "off"),
    ("gemini-3.1-flash-lite", "off"),
    ("gemini-2.5-flash", "upscale3x"),
]
MIN_AGREE = 3
CONC = 6

def read_one(path, model, preproc):
    b = open(path, "rb").read()
    pb = bench.PREPROCS[preproc](b)
    for _ in range(4):
        r = bench.call_gemini(pb, model, 0, timeout=40)
        if not r["err"]:
            return r["ans"] if bench.valid(r["ans"]) else None
        time.sleep(8)
    return None

def label_image(path):
    votes = []
    for model, pp in PANEL:
        a = read_one(path, model, pp)
        if a:
            votes.append(a)
    if not votes:
        return None
    best, n = Counter(votes).most_common(1)[0]
    return best if n >= MIN_AGREE else None

def main():
    files = sorted(glob.glob(REAL + "/*.png"))
    # retoma: nao re-rotula o que ja tem rotulo salvo
    labels = {}
    if os.path.exists(OUT):
        try:
            labels = json.load(open(OUT))
        except Exception:
            labels = {}
    before = len(labels)
    files = [f for f in files if os.path.basename(f) not in labels]
    print(f"ja rotulados: {before} | novas imagens a rotular: {len(files)}", flush=True)
    lock = threading.Lock()
    done = [0]
    def work(path):
        lab = label_image(path)
        with lock:
            done[0] += 1
            if lab:
                labels[os.path.basename(path)] = lab
            if done[0] % 40 == 0:
                print(f"  {done[0]}/{len(files)} processados | rotulados: {len(labels)}", flush=True)
                json.dump(labels, open(OUT, "w"), indent=1)
    threads = []
    for p in files:
        t = threading.Thread(target=work, args=(p,)); threads.append(t)
    running = []
    for t in threads:
        t.start(); running.append(t)
        while sum(1 for x in running if x.is_alive()) >= CONC:
            time.sleep(0.05)
    for t in threads:
        t.join()
    json.dump(labels, open(OUT, "w"), indent=1)
    print(f"\nCONCLUÍDO: {len(labels)}/{len(files)} captchas rotulados com consenso (>= {MIN_AGREE}/4).", flush=True)

if __name__ == "__main__":
    main()

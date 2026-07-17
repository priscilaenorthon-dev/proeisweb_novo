#!/usr/bin/env python3
"""Coleta N imagens de captcha da tela de login do PROEIS e imprime em base64.

Rodar dentro da imagem do app (tem proeis_http + requests + PIL) via Cloud Build.
Cada imagem sai numa linha marcada: CAPTCHA_IMG <indice> <base64>
"""
import sys, base64, os
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proeis_http import ProeisHTTP, DEFAULT_URL, load_env_file

load_env_file()
N = int(os.getenv("COLLECT_N", "10"))

client = ProeisHTTP(
    login=os.getenv("PROEIS_LOGIN", ""),
    password=os.getenv("PROEIS_PASSWORD", ""),
    gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
    debug=False,
)

base_soup = client.request("GET", DEFAULT_URL)
payload = client.form_payload(base_soup)
payload["ddlTipoAcesso"] = "ID"
payload["__EVENTTARGET"] = "ddlTipoAcesso"
soup = client.post_form(payload, DEFAULT_URL)

count = 0
for i in range(N):
    try:
        img = client.extract_captcha_image(soup)
        print(f"CAPTCHA_IMG {i} {base64.b64encode(img).decode('ascii')}", flush=True)
        count += 1
    except Exception as exc:
        print(f"CAPTCHA_ERR {i} {exc}", flush=True)
    if i < N - 1:
        refreshed = client.refresh_page_captcha(soup)
        if refreshed:
            soup = refreshed
        else:
            base_soup = client.request("GET", DEFAULT_URL)
            payload = client.form_payload(base_soup)
            payload["ddlTipoAcesso"] = "ID"
            payload["__EVENTTARGET"] = "ddlTipoAcesso"
            soup = client.post_form(payload, DEFAULT_URL)

print(f"CAPTCHA_DONE {count}", flush=True)

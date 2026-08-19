#!/usr/bin/env python3
"""Autentica no Google Cloud a partir da variavel de ambiente CLAUDINHO_SA_KEY.

Uso:
    python3 scripts/gcp_auth.py            # imprime o token
    TOKEN=$(python3 scripts/gcp_auth.py)   # usa em curl

A variavel guarda a chave da conta de servico claudinho@ em base64 (ou JSON puro).
NUNCA commitar a chave em si — so este script, que a le do ambiente.
Papeis da conta: run.admin, cloudscheduler.admin, datastore.user,
storage.objectAdmin, cloudbuild.builds.editor, iam.serviceAccountUser.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def load_key() -> dict:
    raw = os.environ.get("CLAUDINHO_SA_KEY", "").strip()
    if not raw:
        sys.exit(
            "CLAUDINHO_SA_KEY nao definida.\n"
            "Configure-a nas variaveis de ambiente do ambiente do Claude Code "
            "(claude.ai/code -> icone de nuvem -> engrenagem), e abra uma sessao NOVA "
            "— sessoes ja em execucao nao releem a configuracao."
        )
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def access_token() -> str:
    key = load_key()
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({
        "iss": key["client_email"], "scope": SCOPE,
        "aud": TOKEN_URI, "iat": now, "exp": now + 3600,
    }).encode())
    signing_input = header + b"." + claims

    # A chave privada nunca toca o disco do repo: vai para um arquivo temporario.
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(key["private_key"])
        pem = fh.name
    try:
        os.chmod(pem, 0o600)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem],
            input=signing_input, capture_output=True,
        )
        if proc.returncode != 0:
            sys.exit(f"falha ao assinar o JWT: {proc.stderr.decode()[:200]}")
        assertion = signing_input + b"." + b64url(proc.stdout)
    finally:
        os.unlink(pem)

    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion.decode(),
    }).encode()
    with urllib.request.urlopen(TOKEN_URI, data=body, timeout=30) as resp:
        return json.load(resp)["access_token"]


if __name__ == "__main__":
    print(access_token())

# -*- coding: utf-8 -*-
"""Reproduz a perda de informacao de 29/10 e prova que o recorte novo a evita.

Naquela quinta a rodada gerou 23.901 linhas. O corte guardava so a CAUDA, e o
log salvo comecava as 6h05 — exatamente as 4 marcacoes (6h00 a 6h05) ficaram
de fora, tornando impossivel provar o horario de cada vaga marcada.

Agora o recorte guarda: bloco de linhas-chave + comeco + fim.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.index import _log_excerpt  # noqa: E402

# Log sintetico do mesmo tamanho do real: marcacoes no inicio, tela vazia no fim.
LINHAS = []
for n in range(23901):
    if n == 120:
        LINHAS.append("[09:00:00.123] +120.0s [VAGA] *** MARCACAO CONFIRMADA PELO SITE ***")
    elif n == 121:
        LINHAS.append("[09:00:00.200] +120.1s [VAGA] [auditoria] OK: site gravou 'RAS SAO FIDELIS' em 04/11/2026 22:00:00 (data pedida: 2026-11-04).")
    elif n == 400:
        LINHAS.append("[09:00:58.000] +178.0s [VAGA] Clicando em 'Eu Vou' (acao=postback)...")
    elif n == 23890:
        LINHAS.append("[09:08:55.816] +649.9s [INFO] Evento 12/28 [confirmado] tentativas=2 data='2026-11-04' hora='22:00'")
    else:
        LINHAS.append(f"[09:0{n % 9}:00.000] +{n}.0s [FORM] linha de enchimento numero {n} " + "x" * 40)

MAX_LINES = 8000
MAX_BYTES = 800000
out = _log_excerpt(LINHAS, MAX_LINES, MAX_BYTES)
tam = len(out.encode("utf-8"))

print(f"log original : {len(LINHAS)} linhas")
print(f"recorte      : {out.count(chr(10)) + 1} linhas, {tam / 1024:.1f} KB")
print()

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


check(tam <= MAX_BYTES, f"cabe no teto de bytes do Firestore ({tam} <= {MAX_BYTES})")
check("MARCACAO CONFIRMADA PELO SITE" in out,
      "a marcacao das 6h00 (linha 120) SOBREVIVEU ao corte")
check("[auditoria] OK" in out, "a auditoria da data sobreviveu ao corte")
check("Clicando em 'Eu Vou'" in out, "o clique em Eu Vou sobreviveu ao corte")
check("Evento 12/28 [confirmado]" in out, "o resumo final sobreviveu ao corte")
check("LINHAS-CHAVE" in out, "existe o bloco de linhas-chave no topo")
check("linha de enchimento numero 5\n" in out or "linha de enchimento numero 5 " in out,
      "o COMECO do log cru foi preservado (nao so a cauda)")
check(f"linha de enchimento numero {len(LINHAS) - 2} " in out,
      "o FIM do log cru foi preservado")
check("omitida(s) no meio" in out, "o corte foi no MEIO e esta sinalizado no log")

# Log pequeno nao deve ser recortado nem ganhar marcador de corte.
curto = ["linha a", "linha b", "*** MARCACAO CONFIRMADA PELO SITE ***"]
out2 = _log_excerpt(curto, MAX_LINES, MAX_BYTES)
check("omitida(s) no meio" not in out2, "log pequeno nao leva marcador de corte")
check(all(line in out2 for line in curto), "log pequeno sai inteiro")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

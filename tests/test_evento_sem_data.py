# -*- coding: utf-8 -*-
"""Reproduz o que foi encontrado em 03/11 e prova que agora o robo AVISA.

Naquele dia os 7 eventos cadastrados estavam com `data_evento` em branco (o
dono limpou as datas em 31/10). Como `_scheduled_run_requests()` expande a data
por virgula, data em branco gera ZERO sub-eventos e o evento desaparecia da
rodada em SILENCIO. A rodada de quinta ia responder `ok: true` com
status "sem-eventos", sem nem fazer login — parecendo normal.

Pior: naquele caminho o endpoint saia sem gravar log nenhum, entao o aviso das
6h09 lia o `operation_logs` e achava o log da semana ANTERIOR.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api.index as api  # noqa: E402

HOJE = datetime.now(timezone(timedelta(hours=-3))).date()
ONTEM = (HOJE - timedelta(days=5)).isoformat()
AMANHA = (HOJE + timedelta(days=6)).isoformat()


def ev(nome, data, hora="22:00"):
    return {"nome_evento": nome, "convenio": "08 BPM - RAS", "cpa": "8o BPM - 6o CPA",
            "data_evento": data, "hora_evento": hora, "turno": "", "endereco": "",
            "disponivel": "nao-reserva"}


ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


# ─── cenario 1: o de 03/11 — TODOS sem data ─────────────────────────────────
api._stored_events = lambda: [ev("RAS SÃO FIDÉLIS", ""), ev("MOTO PATRULHA SÃO FIDÉLIS", "", "13:00")]
reqs, ignorados = api._scheduled_run_requests()
print(f"sem data        -> {len(reqs)} sub-eventos, {len(ignorados)} aviso(s)")
check(reqs == [], "nenhum sub-evento (comportamento do site, inalterado)")
check(len(ignorados) == 2, "os 2 eventos sem data foram REPORTADOS (antes: silencio)")
check(all("SEM DATA" in m for m in ignorados), "o aviso diz que o problema e a falta de data")
check(all(n in " ".join(ignorados) for n in ("RAS SÃO FIDÉLIS", "MOTO PATRULHA")),
      "o aviso nomeia cada evento, para dar pra corrigir no app")

# ─── cenario 2: datas do lote velho (o que acontece se nao atualizar) ───────
api._stored_events = lambda: [ev("RAS SÃO FIDÉLIS", f"{ONTEM},{ONTEM}")]
reqs, ignorados = api._scheduled_run_requests()
print(f"datas passadas  -> {len(reqs)} sub-eventos, {len(ignorados)} aviso(s)")
check(reqs == [] and len(ignorados) == 1, "evento com todas as datas vencidas e reportado")
check("ja passaram" in ignorados[0], f"o aviso explica que as datas venceram: '{ignorados[0][:70]}...'")

# ─── cenario 3: cadastro correto nao gera aviso ─────────────────────────────
api._stored_events = lambda: [ev("RAS SÃO FIDÉLIS", f"{AMANHA},{AMANHA}")]
reqs, ignorados = api._scheduled_run_requests()
print(f"cadastro certo  -> {len(reqs)} sub-eventos, {len(ignorados)} aviso(s)")
check(len(reqs) == 2, "evento valido continua gerando seus sub-eventos")
check(ignorados == [], "cadastro correto NAO gera aviso (sem alarme falso)")

# ─── cenario 4: misto — o valido roda, o furado aparece no aviso ────────────
api._stored_events = lambda: [
    ev("RAS SÃO FIDÉLIS", AMANHA),
    ev("RAS SÃO FIDÉLIS", "", "06:00"),
    ev("MOTO PATRULHA SÃO FIDÉLIS", ONTEM, "13:00"),
]
reqs, ignorados = api._scheduled_run_requests()
print(f"misto           -> {len(reqs)} sub-eventos, {len(ignorados)} aviso(s)")
check(len(reqs) == 1, "o evento bom nao e prejudicado pelos furados")
check(len(ignorados) == 2, "os 2 furados aparecem no aviso")

# ─── cenario 5: o log passa a ser gravado quando nada roda ──────────────────
# Antes, o caminho "sem-eventos" saia sem gravar log, e o aviso das 6h09 lia o
# log da semana anterior como se fosse o de hoje.
GRAVADOS = []
api._save_operation_log = lambda op_id, kind, status, lines, name, result: GRAVADOS.append(
    {"kind": kind, "status": status, "linhas": lines, "result": result})
check("_save_operation_log" in open(
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api/index.py")
      ).read().split("if not reqs:")[1].split("return {")[0],
      "o caminho 'sem-eventos' agora grava log proprio")

fonte = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api/index.py")).read()
check('r"|\\[AVISO\\]"' in fonte, "as linhas [AVISO] estao protegidas do corte do log")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

# -*- coding: utf-8 -*-
"""Reproduz o cenario REAL de 06/08 e prova que a correcao evita a perda da vaga.

Cenario: 2 eventos.
  A = RAS SAO FIDELIS -> a vaga EXISTE, mas o site recusa a confirmacao
      (site sobrecarregado). Antes da correcao virava "sem vaga (data sem
      plantao)" e saia da fila -> vaga perdida.
  B = evento de data realmente morta -> deve continuar sendo descartado.
"""
import os, sys
os.environ["BATCH_DEAD_GRACE_SECONDS"] = "0"   # sem grace: corte agressivo
os.environ["BATCH_DEAD_DROP_AFTER"] = "2"
os.environ["BATCH_ALIVE_ATTEMPTS"] = "6"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proeis_http as p

CHAMADAS = {"A": 0, "B": 0}
ALIVE_FLAG = {"A": [], "B": []}

def fake_run_one(client, event, index, total, dry_run, scan_rounds,
                 recovery_window_seconds, last_group, known_alive=False):
    nome = event["nome_evento"]
    CHAMADAS[nome] += 1
    ALIVE_FLAG[nome].append(known_alive)
    if nome == "A":
        # vaga existe; site recusa a confirmacao (exatamente o log de hoje)
        return True, False, ("", "", ""), "Clique executado, mas o site nao confirmou a marcacao."
    # data morta de verdade
    return True, False, ("", "", ""), "Nao encontrei a linha exata do evento solicitado."

p.run_one_batch_event = fake_run_one
# pausa real de 1s entre rodadas (igual producao)

def ev(nome):
    return {"nome_evento": nome, "convenio": "08 BPM - RAS", "cpa": "8o BPM - 6o CPA",
            "data_evento": "2026-08-16", "hora_evento": "", "turno": "", "endereco": "",
            "disponivel": "nao-reserva"}

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")

class FakeClient:
    seen_vaga_labels = []
p.run_batch_events(client=FakeClient(), events=[ev("A"), ev("B")], dry_run=False,
                   scan_rounds=1, batch_window_seconds=6,
                   batch_repeat_pause_seconds=1)

txt = "\n".join(LOG)
print("tentativas no evento A (vaga real):", CHAMADAS["A"])
print("tentativas no evento B (data morta):", CHAMADAS["B"])
print()

ok = True
def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond: ok = False

check("vaga TITULAR existe de verdade" in txt,
      "A foi reconhecido como vaga real (nao data morta)")
check("descartado da fila" not in txt.split("Evento 2")[0],
      "A NUNCA foi descartado da fila")
check(any("Evento 2" in l and "descartado da fila" in l for l in LOG),
      "B (data morta) continua sendo descartado corretamente")
check(CHAMADAS["A"] > CHAMADAS["B"],
      f"A sobreviveu a mais rodadas que B ({CHAMADAS['A']} vs {CHAMADAS['B']})")
check(ALIVE_FLAG["A"][1:] and all(ALIVE_FLAG["A"][1:]),
      f"A passou a receber known_alive=True (ativa as 6 tentativas): {ALIVE_FLAG['A'][:6]}...")
check(not any(ALIVE_FLAG["B"]),
      f"B nunca recebeu known_alive (segue com 3 tentativas): {ALIVE_FLAG['B'][:6]}")
check(any("Priorizando" in l for l in LOG),
      "A foi priorizado na frente da fila")
print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

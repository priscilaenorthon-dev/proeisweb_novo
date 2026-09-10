# -*- coding: utf-8 -*-
"""Reproduz a perda de 10/09 e prova que a correcao a evita.

Cenario real daquela quinta:
  A = 35 ZE em 15/09 -> a vaga EXISTIA (4 postos), mas o site, engasgado depois
      de um erro 500, devolveu a tela SEM NENHUMA linha 'Eu Vou'. O robo contou
      como "data morta", descartou em 2 rodadas e perdeu a vaga com 5 minutos de
      janela sobrando.
  B = data realmente sem plantao -> a tela vem com vagas de OUTROS eventos, so
      nao com a nossa. Esse continua sendo descartado.
"""
import os
import sys

os.environ["BATCH_DEAD_GRACE_SECONDS"] = "0"   # corte agressivo: pior caso
os.environ["BATCH_DEAD_DROP_AFTER"] = "2"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proeis_http as p

ERRO_TELA_VAZIA = f"Nao encontrei a linha exata do evento solicitado. {p.PAGINA_VAZIA_MARCA}"
ERRO_SEM_PLANTAO = ("Nao encontrei a linha exata do evento solicitado. Nenhuma vaga desse "
                    "evento nessa data (esgotada ou nao publicada); o cadastro pode estar certo.")

CHAMADAS = {"A": 0, "B": 0}


def fake_run_one(client, event, index, total, dry_run, scan_rounds,
                 recovery_window_seconds, last_group, known_alive=False):
    nome = event["nome_evento"]
    CHAMADAS[nome] += 1
    erro = ERRO_TELA_VAZIA if nome == "A" else ERRO_SEM_PLANTAO
    return True, False, ("", "", ""), erro


p.run_one_batch_event = fake_run_one

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")


def ev(nome):
    return {"nome_evento": nome, "convenio": "08 BPM - RAS", "cpa": "8o BPM - 6o CPA",
            "data_evento": "2026-09-15", "hora_evento": "", "turno": "", "endereco": "",
            "disponivel": "nao-reserva"}


class FakeClient:
    seen_vaga_labels = []


# batch_max_no_action_rounds=0 e exatamente o que o robo das 6h usa
# (/api/run-scheduled): "nao desiste" enquanto houver janela.
p.run_batch_events(client=FakeClient(), events=[ev("A"), ev("B")], dry_run=False,
                   scan_rounds=1, batch_window_seconds=6, batch_repeat_pause_seconds=1,
                   batch_max_no_action_rounds=0)

txt = "\n".join(LOG)
print("rodadas no evento A (tela vazia)     :", CHAMADAS["A"])
print("rodadas no evento B (sem plantao)    :", CHAMADAS["B"])
print()

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


check("tela vazia" in txt, "A foi reconhecido como tela vazia do site")
check(not any("Evento 1" in l and "descartado da fila" in l for l in LOG),
      "A NUNCA foi descartado como data morta")
check(any("Evento 2" in l and "descartado da fila" in l for l in LOG),
      "B (data realmente sem plantao) continua sendo descartado")
check(CHAMADAS["A"] > CHAMADAS["B"],
      f"A insistiu ate o fim da janela ({CHAMADAS['A']} vs {CHAMADAS['B']} rodadas)")
print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

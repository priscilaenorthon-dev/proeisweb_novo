# -*- coding: utf-8 -*-
"""Reproduz a perda de 29/10 e prova que a re-admissao de datas a evita.

Naquela quinta o robo:
  - cortou a data 02/11 as ~6h03 como "data sem plantao" (so 6 tentativas);
  - depois ficou 5 MINUTOS batendo em 06/11 e 07/11, que o site devolvia em
    branco (20 tentativas cada, 170 telas vazias no log);
  - e a vaga TITULAR de MOTO PATRULHA 02/11 apareceu no site DEPOIS do corte.
    Ninguem voltou nela. Vaga perdida com janela sobrando.

Cenario do teste:
  A = data que o site publica atrasado -> deve ser re-admitida e marcada;
  B = data travada em tela vazia       -> nao pode monopolizar as tentativas.
"""
import os
import sys

os.environ["BATCH_DEAD_GRACE_SECONDS"] = "0"            # corte agressivo: pior caso
os.environ["BATCH_DEAD_DROP_AFTER"] = "2"
os.environ["BATCH_DEAD_REVIVE_MIN_REMAINING"] = "1"     # janela curta de teste
os.environ["BATCH_DEAD_REVIVE_MAX"] = "3"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proeis_http as p  # noqa: E402

ERRO_SEM_PLANTAO = ("Nao encontrei a linha exata do evento solicitado. Nenhuma vaga desse "
                    "evento nessa data (esgotada ou nao publicada); o cadastro pode estar certo.")
ERRO_TELA_VAZIA = f"Nao encontrei a linha exata do evento solicitado. {p.PAGINA_VAZIA_MARCA}"

CHAMADAS = {"A": 0, "B": 0}
# Em que chamada de A a vaga finalmente aparece no site (publicacao atrasada).
A_PUBLICA_NA_CHAMADA = 3


def fake_run_one(client, event, index, total, dry_run, scan_rounds,
                 recovery_window_seconds, last_group, known_alive=False):
    nome = event["nome_evento"]
    CHAMADAS[nome] += 1
    if nome == "A":
        if CHAMADAS["A"] >= A_PUBLICA_NA_CHAMADA:
            return True, True, ("", "", ""), ""       # vaga publicada: marca
        return True, False, ("", "", ""), ERRO_SEM_PLANTAO
    return True, False, ("", "", ""), ERRO_TELA_VAZIA  # B: site em branco sempre


p.run_one_batch_event = fake_run_one

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")


def ev(nome, data):
    return {"nome_evento": nome, "convenio": "08 BPM - RAS", "cpa": "8o BPM - 6o CPA",
            "data_evento": data, "hora_evento": "", "turno": "", "endereco": "",
            "disponivel": "nao-reserva"}


class FakeClient:
    seen_vaga_labels = []


# batch_max_no_action_rounds=0 e exatamente o que o robo das 6h usa.
confirmadas = p.run_batch_events(
    client=FakeClient(), events=[ev("A", "2026-11-02"), ev("B", "2026-11-06")],
    dry_run=False, scan_rounds=1, batch_window_seconds=8,
    batch_repeat_pause_seconds=1, batch_max_no_action_rounds=0,
)

txt = "\n".join(LOG)
print("confirmadas:", confirmadas)
print("chamadas em A (data publicada atrasada):", CHAMADAS["A"])
print("chamadas em B (tela vazia):", CHAMADAS["B"])
print()

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


check("descartado da fila" in txt, "A foi cortado como data morta (corte segue existindo)")
check("Re-admitindo" in txt, "A data cortada FOI RE-ADMITIDA na fila")
check(confirmadas == 1, f"a vaga publicada atrasado foi marcada (confirmadas={confirmadas})")
check(CHAMADAS["A"] >= A_PUBLICA_NA_CHAMADA,
      f"A voltou a ser tentado depois do corte ({CHAMADAS['A']} chamadas)")
check(any("Evento 1/2 [confirmado]" in l for l in LOG),
      "o resumo final mostra A como confirmado, nao como 'data sem plantao'")
check("para o fim da fila" in txt,
      "B (tela vazia) foi mandado para o FIM da fila em vez de monopolizar a rodada")
check(not any("Evento 2/2" in l and "descartado da fila" in l for l in LOG),
      "B (tela vazia) nunca foi tratado como data sem plantao")

# A re-admissao nao pode virar laco infinito.
check(txt.count("Re-admitindo") <= 3,
      f"re-admissao respeita o teto de 3 (ocorreu {txt.count('Re-admitindo')}x)")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

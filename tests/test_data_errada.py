# -*- coding: utf-8 -*-
"""Reproduz o bug de 29/10: log disse 08/11 22:00, site gravou 06/11 22:00.

O robo clicou em 'Eu Vou' numa tela que NAO era da data pedida e registrou no
log a data pedida. Resultado: 4 vagas marcadas, mas uma num dia diferente do
que o relatorio afirmava.

Duas defesas, testadas aqui:
  1. travessa de data  -> nao clica se a tela aberta nao for da data pedida;
  2. auditoria         -> confere na lista oficial de marcados a data real.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bs4 import BeautifulSoup  # noqa: E402
import proeis_http as p  # noqa: E402

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


# ─── 1. leitura da data selecionada na tela ──────────────────────────────────
def pagina(data_selecionada):
    opts = "".join(
        f"<option value='{266 + i}'{' selected' if d == data_selecionada else ''}>{d}</option>"
        for i, d in enumerate(["2026-11-02", "2026-11-06", "2026-11-07", "2026-11-08"])
    )
    return BeautifulSoup(
        f"<html><body><select id='ddlDataEvento' name='ddlDataEvento'>{opts}</select>"
        f"<table><tr><td>RAS SÃO FIDÉLIS 22:00:00 8 h AVENIDA EMYGDIO MAIA SANTOS 1 "
        f"- curso: N <a href='#'>Eu Vou</a></td></tr></table></body></html>",
        "html.parser",
    )


client = p.ProeisHTTP(login="x", password="y", gemini_api_key="", debug=False)

check(client.page_selected_date(pagina("2026-11-06")) == "2026-11-06",
      "le a data que o site mostra como selecionada")
check(client.page_selected_date(BeautifulSoup("<html></html>", "html.parser")) == "",
      "sem dropdown devolve vazio (nunca inventa data)")

check(p.same_site_date("06/11/2026", "2026-11-06") is True,
      "compara datas em formatos diferentes (dd/mm/yyyy vs ISO)")
check(p.same_site_date("2026-11-08", "2026-11-06") is False,
      "datas diferentes nao sao consideradas iguais")

# ─── 2. travessa: nao clicar na tela errada ──────────────────────────────────
CLIQUES = []
FAKE = p.Candidate(
    "RAS SÃO FIDÉLIS 22:00:00 8 h AVENIDA EMYGDIO MAIA SANTOS 1 - curso: N",
    "postback", {"target": "btn1", "argument": ""}, 10,
)
client.available_candidates = lambda soup, prefer: [FAKE]
client.form_payload = lambda soup: {}
client.fill_page_captcha = lambda soup, payload: None
client.set_reserva_checkbox = lambda soup, payload, on: None
client.report_good_captcha = lambda: None
client.post_form = lambda payload: (CLIQUES.append(payload) or pagina("2026-11-08"))
client.confirm_if_needed = lambda soup, alvo_label="": True
client.audit_marking = lambda nome, data="", hora="": "08/11/2026 22:00:00"

# Cenario do bug: pedimos 08/11, mas a tela carregada e de 06/11.
client.soup = pagina("2026-11-06")
erro = ""
try:
    client.choose_target_event(
        "nao-reserva", False, data_evento="2026-11-08",
        nome_evento="RAS SÃO FIDÉLIS", hora_evento="22:00", turno="", endereco="",
    )
except p.AutomationError as exc:
    erro = str(exc)

check(not CLIQUES, "NAO clicou em Eu Vou na tela de data errada (vaga nao vai pro dia errado)")
check("data errada" in erro and "2026-11-06" in erro and "2026-11-08" in erro,
      f"o erro diz as duas datas para dar pra entender no log: '{erro[:90]}...'")
check(not p.is_no_action_available_error(erro),
      "o erro NAO e 'sem vaga': o robo vai refazer o filtro e tentar de novo")

# Caminho normal: tela da data certa -> clica.
client.soup = pagina("2026-11-08")
sucesso = client.choose_target_event(
    "nao-reserva", False, data_evento="2026-11-08",
    nome_evento="RAS SÃO FIDÉLIS", hora_evento="22:00", turno="", endereco="",
)
check(sucesso is True and len(CLIQUES) == 1,
      "na tela da data CERTA a travessa nao atrapalha: clicou e confirmou")

# Sem data cadastrada (varredura livre) a travessa nao pode travar nada.
CLIQUES.clear()
client.soup = pagina("2026-11-06")
check(client.choose_target_event(
          "nao-reserva", False, data_evento="",
          nome_evento="RAS SÃO FIDÉLIS", hora_evento="22:00", turno="", endereco="",
      ) is True and len(CLIQUES) == 1,
      "evento sem data fixa segue funcionando (varredura livre)")

# ─── 3. auditoria: descobre a data que o site REALMENTE gravou ───────────────
LISTA_REAL = (
    "Convenio: 08 BPM - RAS\nEvento: RAS SÃO FIDÉLIS\nData/Hora: 06/11/2026 22:00:00\n"
    "Tipo da Vaga: TITULAR\n==========\n"
    "Convenio: 08 BPM - RAS\nEvento: RAS SÃO FIDÉLIS\nData/Hora: 07/11/2026 14:00:00\n"
    "Tipo da Vaga: TITULAR\n"
)
auditor = p.ProeisHTTP(login="x", password="y", gemini_api_key="", debug=False)
auditor.request = lambda method, url, **kw: BeautifulSoup(
    f"<html><body><textarea id='txtEveVoluntario'>{LISTA_REAL}</textarea></body></html>",
    "html.parser",
)

LOG.clear()
real = auditor.audit_marking("RAS SÃO FIDÉLIS", "2026-11-08", "22:00")
txt = "\n".join(LOG)
check("DIVERGENCIA" in txt, "a auditoria GRITA quando o site gravou outra data")
check("06/11/2026" in txt, "a auditoria mostra a data real que o site gravou")
check(real.startswith("06/11/2026"), f"a auditoria devolve a data real ('{real}')")

LOG.clear()
real_ok = auditor.audit_marking("RAS SÃO FIDÉLIS", "2026-11-07", "14:00")
txt_ok = "\n".join(LOG)
check("[auditoria] OK" in txt_ok and "DIVERGENCIA" not in txt_ok,
      "quando a data confere, a auditoria so registra OK")
check(real_ok.startswith("07/11/2026"), "a auditoria casa a data certa entre varias marcadas")

LOG.clear()
os.environ["AUDIT_MARKING"] = "0"
check(auditor.audit_marking("RAS SÃO FIDÉLIS", "2026-11-08", "22:00") == "" and not LOG,
      "auditoria pode ser desligada por env (AUDIT_MARKING=0)")
os.environ.pop("AUDIT_MARKING")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

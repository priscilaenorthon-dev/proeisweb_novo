# -*- coding: utf-8 -*-
"""Reproduz o que foi medido em 12/11 e 19/11: o PROEIS aceita UMA marcacao por login.

Nas duas quintas: 6 cliques aceitos, todos o 1o clique depois de um login; 37
recusados com "Acao nao permitida", todos numa sessao que ja tinha marcado —
inclusive com captcha de confianca 1,00. Em 19/11 o robo gastava ~5s em 8
recusas ate chegar na tentativa que refaz o login; as 4 vagas sairam em 19s e a
de 29/11 se perdeu nesse intervalo.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bs4 import BeautifulSoup  # noqa: E402
import proeis_http as p  # noqa: E402

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")
p.emit_vaga = lambda *a, **k: None
p.time.sleep = lambda s: None

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


def tela(data, alerta=False, sucesso=False):
    linha = ("<tr><td>MOTO PATRULHA SÃO FIDÉLIS</td><td>13:00:00</td><td>8 h</td>"
             "<td>AVENIDA EMYDIO MAIA SANTOS</td><td>2 - curso: N</td>"
             "<td><a href=\"javascript:__doPostBack('gv$ctl02$lnk','')\">Eu Vou</a></td></tr>")
    script = "<script>alert('Ação não permitida');</script>" if alerta else ""
    msg = "<span>Confirmacao no evento foi incluida com sucesso</span>" if sucesso else ""
    return BeautifulSoup(
        "<html><body><select id='ddlDataEvento' name='ddlDataEvento'>"
        f"<option value='1' selected>{data}</option></select>{msg}<table>{linha}</table>{script}</body></html>",
        "html.parser")


class Site:
    """Modelo do comportamento medido: so o 1o clique de cada login e aceito."""
    def __init__(self, ja_marcou=False):
        self.marcou_nesta_sessao = ja_marcou
        self.logins = 0
        self.cliques = 0
        self.recusas = 0
        self.data = ""


def novo_cliente(site):
    c = p.ProeisHTTP(login="x", password="y", gemini_api_key="", debug=False)

    def login_flow():
        site.logins += 1
        site.marcou_nesta_sessao = False

    def fill_filters(convenio, data, cpa, prefer=""):
        site.data = data
        c.soup = tela(data)

    def fill(soup, payload):
        c.last_captcha_conf = 1.0
        c._pending_captcha = {"image_b64": "aW1n", "answer": "ABC123", "model": "ocr-local"}

    def post(payload):
        site.cliques += 1
        if site.marcou_nesta_sessao:
            site.recusas += 1
            return tela(site.data, alerta=True)
        site.marcou_nesta_sessao = True
        return tela(site.data, sucesso=True)

    c.login_flow = login_flow
    c.reset_navigation_state = lambda: None
    c.navigate_to_service_page = lambda: None
    c.fill_filters = fill_filters
    c.form_payload = lambda soup: {}
    c.fill_page_captcha = fill
    c.set_reserva_checkbox = lambda soup, payload, on: None
    c.post_form = post
    c.audit_marking = lambda *a, **k: ""
    c.verify_marking_in_menu = lambda *a, **k: False
    return c


def ev(data):
    return {"nome_evento": "MOTO PATRULHA SÃO FIDÉLIS", "convenio": "08 BPM - RAS",
            "cpa": "8o BPM - 6o CPA", "data_evento": data, "hora_evento": "13:00",
            "turno": "", "endereco": "", "disponivel": "nao-reserva"}


DATAS = ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-28"]

# ─── 1. quatro vagas seguidas (a rodada de 19/11) ───────────────────────────
site = Site()
c = novo_cliente(site)
grupo = ("", "", "")
marcadas = 0
for i, d in enumerate(DATAS, 1):
    _ok, sucesso, grupo, erro = p.run_one_batch_event(c, ev(d), i, len(DATAS), False, 1, 0, grupo)
    marcadas += 1 if sucesso else 0
print(f"4 vagas: marcadas={marcadas} cliques={site.cliques} recusas={site.recusas} logins={site.logins}")
check(marcadas == 4, "as 4 vagas foram marcadas")
check(site.recusas == 0, f"NENHUM clique recusado (em 19/11 foram 24 recusas nessas mesmas 4 vagas; agora {site.recusas})")
check(site.cliques == 4, f"um clique por vaga ({site.cliques})")
check(site.logins == 3, f"relogou entre uma marcacao e a seguinte ({site.logins} logins para 4 vagas)")
check(any("uma marcacao por login" in l for l in LOG), "o log explica por que relogou")

# ─── 2. sessao ja gasta sem o robo saber -> 1 recusa, reloga, marca ─────────
LOG.clear()
site = Site(ja_marcou=True)
c = novo_cliente(site)
_ok, sucesso, grupo, erro = p.run_one_batch_event(c, ev("2026-11-29"), 1, 1, False, 1, 0, ("", "", ""))
print(f"sessao gasta: sucesso={sucesso} cliques={site.cliques} recusas={site.recusas} logins={site.logins}")
check(sucesso is True, "vaga marcada mesmo com a sessao ja gasta")
check(site.recusas == 1, f"so UMA recusa antes de relogar (antes eram 8; agora {site.recusas})")
check(site.logins == 1 and site.cliques == 2, "recusou -> relogou -> marcou no clique seguinte")
check(any("nao marca mais" in l for l in LOG), "o log registra que a sessao nao marca mais")
check(not any("CONTINUA na tela" in l for l in LOG), "nao insistiu na mesma tela apos 'Acao nao permitida'")

# ─── 3. evento sem vaga nao dispara login a toa ─────────────────────────────
site = Site()
c = novo_cliente(site)
c.fill_filters = lambda convenio, data, cpa, prefer="": setattr(c, "soup", BeautifulSoup(
    "<html><body><table><tr><td>RAS CAMPOS 12H</td><td>07:00:00</td><td>2 - curso: N</td>"
    "<td><a href=\"javascript:__doPostBack('x','')\">Eu Vou</a></td></tr></table></body></html>", "html.parser"))
_ok, sucesso, grupo, erro = p.run_one_batch_event(c, ev("2026-11-23"), 1, 1, False, 1, 0, ("", "", ""))
check(sucesso is False and site.logins == 0, "sem marcacao previa nao ha relogin extra")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

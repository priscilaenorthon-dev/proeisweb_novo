# -*- coding: utf-8 -*-
"""Prova que o diagnostico enxerga o que o log antigo escondia."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bs4 import BeautifulSoup
import proeis_http as p

# Boilerplate igual ao real: o dropdown de convenios que ocupava os 1200 chars do log
DROPDOWN = "<select id='ddlConvenios'>" + "".join(
    f"<option>{i:02d} BPM - RAS</option>" for i in range(1, 60)) + "</select>"

ALVO = "RAS SÃO FIDÉLIS 14:00:00 8 h AVENIDA EMYGDIO MAIA SANTOS 2 - curso: N Eu Vou"

CENARIOS = {
 "site recusou (vaga continua la)": f"""
   <html><body><h3>Eventos &gt; Associar Voluntario</h3>{DROPDOWN}
   <span id='lblMensagem' class='erro'>Nao foi possivel concluir a operacao. Tente novamente.</span>
   <table><tr><td>{ALVO}</td></tr><tr><td>OUTRA VAGA 10:00 Eu Vou</td></tr></table>
   </body></html>""",
 "outro voluntario pegou (alvo sumiu)": f"""
   <html><body><h3>Eventos &gt; Associar Voluntario</h3>{DROPDOWN}
   <table><tr><td>OUTRA VAGA 10:00 Eu Vou</td></tr></table>
   </body></html>""",
 "limite/cota do decreto": f"""
   <html><body>{DROPDOWN}
   <script>alert('Voce ja atingiu o limite de servicos permitidos no ciclo semanal.');</script>
   <table><tr><td>{ALVO}</td></tr></table>
   </body></html>""",
}

ok = True
for nome, html in CENARIOS.items():
    fb = p.ProeisHTTP.extract_site_feedback(BeautifulSoup(html, "html.parser"), ALVO)
    print(f"=== {nome} ===")
    print("  mensagens :", fb["mensagens"])
    print("  alertas   :", fb["alertas"])
    print("  alvo_presente:", fb["alvo_presente"], "| linhas Eu Vou:", fb["linhas_eu_vou"])
    print("  texto util:", fb["texto_util"][:100])
    # o texto util NAO pode estar tomado pelo dropdown
    poluido = fb["texto_util"].count("BPM - RAS") > 2
    print("  -> dropdown removido:", "SIM" if not poluido else "NAO (FALHOU)")
    if poluido: ok = False
    print()

fb1 = p.ProeisHTTP.extract_site_feedback(BeautifulSoup(CENARIOS["site recusou (vaga continua la)"], "html.parser"), ALVO)
fb2 = p.ProeisHTTP.extract_site_feedback(BeautifulSoup(CENARIOS["outro voluntario pegou (alvo sumiu)"], "html.parser"), ALVO)
fb3 = p.ProeisHTTP.extract_site_feedback(BeautifulSoup(CENARIOS["limite/cota do decreto"], "html.parser"), ALVO)

def chk(c, m):
    global ok
    print(("PASSOU  " if c else "FALHOU  ") + m)
    if not c: ok = False

chk(fb1["mensagens"] and "nao foi possivel" in fb1["mensagens"][0].lower(), "captura a mensagem de erro do site")
chk(fb1["alvo_presente"] is True, "detecta que a vaga CONTINUA la (site recusou)")
chk(fb2["alvo_presente"] is False, "detecta que a vaga SUMIU (outro pegou)")
chk(fb2["linhas_eu_vou"] < fb1["linhas_eu_vou"], "conta corretamente as linhas restantes")
chk(any("limite" in a.lower() for a in fb3["alertas"]), "captura alert() de limite/cota")
chk(fb1["texto_util"] != fb2["texto_util"], "recusa e 'vaga tomada' agora ficam DIFERENTES no log")
print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)

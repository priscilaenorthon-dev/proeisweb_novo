import unittest

from bs4 import BeautifulSoup

from proeis_http import ProeisHTTP


class NoClickClient(ProeisHTTP):
    def __init__(self):
        super().__init__("login", "password", "captcha-key", debug=False)

    def postback(self, target: str, argument: str = ""):
        raise AssertionError("simulate_mark must not postback")

    def post_form(self, payload, url=None):
        raise AssertionError("simulate_mark must not submit forms")

    def request(self, method: str, url: str, **kwargs):
        raise AssertionError("simulate_mark must not click GET actions")


class SimulateMarkTests(unittest.TestCase):
    def setUp(self):
        self.client = NoClickClient()

    def test_simulate_mark_selects_requested_reservas_without_clicking(self):
        html = """
        <table>
          <tr><td>RAS A 06:00:00 8 h RUA A RESERVA - curso: N</td><td><a href="javascript:__doPostBack('a1','')">Eu Vou</a></td></tr>
          <tr><td>RAS B 07:00:00 8 h RUA B RESERVA - curso: N</td><td><a href="javascript:__doPostBack('a2','')">Eu Vou</a></td></tr>
          <tr><td>RAS C 08:00:00 8 h RUA C RESERVA - curso: N</td><td><a href="javascript:__doPostBack('a3','')">Eu Vou</a></td></tr>
          <tr><td>RAS D 09:00:00 8 h RUA D RESERVA - curso: N</td><td><a href="javascript:__doPostBack('a4','')">Eu Vou</a></td></tr>
        </table>
        """
        self.client.soup = BeautifulSoup(html, "html.parser")

        selected = self.client.simulate_target_events("reserva", quantidade=3, data_evento="2026-05-01")

        self.assertEqual(len(selected), 3)
        self.assertTrue(all("RESERVA" in candidate.label for candidate in selected))

    def test_simulate_mark_ignores_reserva_when_filter_is_normal(self):
        html = """
        <table>
          <tr><td>RAS RESERVA 06:00:00 8 h RUA A RESERVA - curso: N</td><td><a href="javascript:__doPostBack('a1','')">Eu Vou</a></td></tr>
          <tr><td>RAS NORMAL 07:00:00 8 h RUA B 1 - curso: N</td><td><a href="javascript:__doPostBack('a2','')">Eu Vou</a></td></tr>
        </table>
        """
        self.client.soup = BeautifulSoup(html, "html.parser")

        selected = self.client.simulate_target_events("nao-reserva", quantidade=1, data_evento="2026-05-01")

        self.assertEqual(len(selected), 1)
        self.assertIn("NORMAL", selected[0].label)


if __name__ == "__main__":
    unittest.main()

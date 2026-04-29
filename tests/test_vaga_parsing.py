import unittest

from proeis_gui import parse_vaga_output


class VagaParsingTests(unittest.TestCase):
    def test_parses_json_vaga_with_date_and_action(self):
        row = parse_vaga_output(
            '{"data":"30/04/2026","acao":"Visualizacao","label":"RAS TESTE 06:00:00 8 h RUA A RESERVA - curso: N Eu Vou"}'
        )

        self.assertEqual(row, ("30/04/2026", "RAS TESTE", "06:00:00", "8 h", "RUA A", "RESERVA - curso: N", "Visualizacao"))

    def test_parses_legacy_vaga_line(self):
        row = parse_vaga_output("RAS TESTE 06:00:00 8 h RUA A RESERVA - curso: N Eu Vou")

        self.assertEqual(row, ("", "RAS TESTE", "06:00:00", "8 h", "RUA A", "RESERVA - curso: N", "Visualizacao"))

    def test_malformed_json_prefix_falls_back_without_exception(self):
        row = parse_vaga_output('{"data":"30/04/2026", invalid-json')

        self.assertEqual(row, ("", '{"data":"30/04/2026", invalid-json', "", "", "", "", "Visualizacao"))


if __name__ == "__main__":
    unittest.main()

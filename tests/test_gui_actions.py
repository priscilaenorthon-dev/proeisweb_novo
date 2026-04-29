import unittest
from unittest.mock import Mock, patch

from proeis_gui import ProeisApp


class _Flag:
    def __init__(self, value: bool):
        self._value = value

    def get(self) -> bool:
        return self._value


class GuiActionTests(unittest.TestCase):
    def test_run_test_starts_dry_run(self):
        app = Mock()
        ProeisApp.run_test(app)
        app.start_process.assert_called_once_with(dry_run=True)

    def test_run_list_all_enables_list_all_dates(self):
        app = Mock()
        ProeisApp.run_list_all(app)
        app.start_process.assert_called_once_with(dry_run=True, list_all_dates=True)

    @patch("proeis_gui.messagebox.askyesno", return_value=True)
    def test_run_real_confirms_and_starts(self, _askyesno):
        app = Mock()
        app.agendamento = _Flag(False)
        ProeisApp.run_real(app)
        app.start_process.assert_called_once_with(dry_run=False)

    @patch("proeis_gui.messagebox.askyesno", return_value=False)
    def test_run_real_does_not_start_when_user_cancels(self, _askyesno):
        app = Mock()
        app.agendamento = _Flag(False)
        ProeisApp.run_real(app)
        app.start_process.assert_not_called()

    def test_run_real_uses_schedule_when_enabled(self):
        app = Mock()
        app.agendamento = _Flag(True)
        ProeisApp.run_real(app)
        app._schedule_execution.assert_called_once_with()
        app.start_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()

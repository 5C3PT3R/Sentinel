from datetime import date

from typer.testing import CliRunner

from sentinel.cli import app
from sentinel.models import Report


def test_report_defaults_reconcile():
    r = Report(run_id="r1", date=date(2026, 5, 14), status="normal", headline="ok")
    assert r.unexplained_pct == 100.0 and r.primary_cause is None


def test_cli_stub():
    res = CliRunner().invoke(app, ["run", "2026-05-14"])
    assert res.exit_code == 1 and "not implemented" in res.output

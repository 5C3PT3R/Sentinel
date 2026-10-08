import typer

app = typer.Typer(help="Sentinel: autonomous root-cause analysis.")


@app.command()
def simulate(scenarios: str = "scenarios/", out: str | None = None, days: int | None = None, sessions: int | None = None,
             seed: int | None = None, plot: bool = True):
    """Generate synthetic data + ground_truth.json (+ revenue.png)."""
    from sentinel.simulator.generator import simulate as sim

    typer.echo(sim(scenarios, out, days, sessions, seed, plot))


@app.command()
def run(date: str):
    """Analyze one day (M2-M5)."""
    raise typer.Exit(_todo("run (M2-M5)"))


@app.command()
def eval(scenarios: str = "scenarios/", systems: str = "sentinel", runs: int = 3):
    """Benchmark systems against scenarios (M6)."""
    raise typer.Exit(_todo("eval (M6)"))


def _todo(name: str) -> int:
    typer.echo(f"{name}: not implemented yet")
    return 1


if __name__ == "__main__":
    app()

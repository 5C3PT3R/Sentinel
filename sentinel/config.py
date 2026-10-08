from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_settings(path: Path = ROOT / "config" / "settings.yaml") -> dict:
    return yaml.safe_load(path.read_text())

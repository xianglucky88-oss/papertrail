from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
SOURCES_FIXTURE = ROOT / "fixtures" / "sources.json"
DB_PATH = ROOT / "papertrail.db"

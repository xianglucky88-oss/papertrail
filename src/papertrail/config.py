from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
PAPER_FIXTURES = ROOT / "fixtures" / "papers.json"
DB_PATH = ROOT / "papertrail.db"

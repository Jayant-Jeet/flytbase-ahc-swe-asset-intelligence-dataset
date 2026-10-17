"""Pin the unit tests to the bundled sample dataset regardless of what .env points at."""
import os

os.environ["DATA_DIR"] = "sample-data"
os.environ["DB_PATH"] = "data/asset.db"
os.environ["RULES_PATH"] = "rules/rules.json"
os.environ["TODAY"] = "2026-10-17"

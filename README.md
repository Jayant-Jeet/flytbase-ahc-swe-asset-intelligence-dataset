# Swayam- The Asset Intelligence Copilot

Challenge repository for the FlytBase Hack Day (Hacktoberfest 2026).

Build a chat copilot that answers operators' plain-English questions about an oil field, correctly and with evidence, using records, sensor readings, manuals, repair reports and notes.

## Quick start

```powershell
python -m venv .venv; .venv\Scripts\activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                             # then set GOOGLE_API_KEY (never commit .env)
python -m app.ingest --data-dir sample-data        # build the SQLite DB, rules, report facts, BM25 index
python -m app.server                               # chat UI at http://localhost:8000
```

Design: the LLM only picks tools and writes language; every number, date and status comes from code tools
(see [`docs/DESIGN.md`](docs/DESIGN.md)).

### Environment variables (`.env`)

| Variable | Meaning |
| :--- | :--- |
| `DATA_DIR` | Dataset folder(s); separate several with `;` (Windows) or `:` (Linux/macOS), e.g. the sample set plus the hidden test set |
| `DB_PATH`, `RULES_PATH` | SQLite file and the reviewable extracted-rules JSON (`rules/rules.json`) |
| `TODAY` | "Today" for relative periods (default `2026-10-17`) |
| `LLM_PROVIDER` | `gemini` (Google AI Studio), `openrouter`, or `ollama` |
| `LLM_MODEL` | default `gemma-4-31b-it` (OpenRouter: `google/gemma-4-31b-it:free`) |
| `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL` | credentials / endpoints |

### Ingest a new dataset (no code changes)

```powershell
python -m app.ingest --data-dir path\to\test-dataset
```

Re-runnable; nothing is hardcoded to well IDs, areas, dates or thresholds. Rules are read from the manuals/SOPs
into `rules/rules.json` (review it) and tools read thresholds from the `rules` table.

### Tests and evaluation

```powershell
python -m pytest tests -q                          # unit tests
python -m tests.eval_questions -v                  # all 18 sheet questions, deterministic tool-level checks
python -m tests.eval_questions --llm               # end to end through the model (needs an API key)
python -m tests.eval_questions --only 12 14 --data-dir path\to\other
```

Each run uses a throwaway DB and resets memory. Q11-Q18 are multi-turn / multi-user flows
(including a simulated "next day").

## Start here

| Document | What it is |
| :--- | :--- |
| [`01-problem-statement.md`](01-problem-statement.md) | The challenge, what to build, what to submit and how we judge |
| [`02-dataset-guide.md`](02-dataset-guide.md) | Every file and field in the sample dataset |
| [`03-evaluation-queries.md`](03-evaluation-queries.md) | Example questions with expected answers, to check your progress |
| [`04-prerequisites.md`](04-prerequisites.md) | What to set up before the event: coding agent and model access |

## Sample data

`sample-data/` holds a fictional oil field: 200 wells in 4 areas, with 4 years of history.

- `sample-data/fleet/`: areas, teams and people, well-type manuals, and operating procedures
- `sample-data/assets/WELL-0001/` to `WELL-0200/`: one folder per well

All data is synthetic and created for this challenge. Manuals and procedures are simplified, fictional documents, not real operating or legal guidance.

## Test data

`test-data/` holds the test dataset (`test-data/dataset/`), its release notes (`test-data/README.md`) and the final questions (`test-data/final-questions.md`).

## License

[MIT](LICENSE)

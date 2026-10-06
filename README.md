# Document Knowledge Assistant - Proof of Capability

FastAPI backend + Python-driven NiceGUI frontend/admin, hybrid RAG (dense + BM25), user memory across
chats and restarts, a bounded agent, MFA, and Python test suites (pytest + Playwright). Runs directly on Windows.

* Client-facing scope and evidence: [docs/POC_PROPOSAL.md](docs/POC_PROPOSAL.md)
* Architecture, retrieval, memory, deletion and AWS network path: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Quick start (Windows, PowerShell)

Prerequisites: Python 3.12 (python.org installer, "Add to PATH"), internet for the first install.

```powershell
cd D:\workspace\POC
.\scripts\setup.ps1            # venv, pinned deps, .env with random secret, admin password prompt, sample docs
.\scripts\run.ps1              # http://127.0.0.1:8080  (first start downloads the 384-d embedding model, ~70 MB)
```

In a second PowerShell window, load demo users and the shared sample corpus:

```powershell
.\.venv\Scripts\python scripts\seed_demo.py --users alice,bob,user01,user02,user03,user04,user05
# prompts for the admin password and a password for the demo users (typing is invisible)
```

Then open http://127.0.0.1:8080 and log in as `admin` or `alice`.

## Tests

```powershell
.\scripts\test.ps1                    # 63 backend + security tests (offline embedder, ~10 s)
.\scripts\test.ps1 -RealEmbeddings    # same suite with the real local embedding model
.\scripts\test.ps1 -E2E               # Playwright browser dialogues against the running app
$env:E2E_USERS=5; .\.venv\Scripts\python -m pytest tests/e2e -m e2e -k parallel -s   # parallel users
```

Dialogues are editable JSON: `tests/dialogues/knowledge_assistant.json` (prompts, expected facts, expected sources).
Reports/transcripts are written to `reports/`.

## Configuration

All settings come from `.env` (template: `.env.example`, no secrets in the repo). Retrieval/chunking
settings are edited in **Admin -> Profiles & chunking**, not in Python.

| Setting | Meaning |
|---|---|
| `LLM_PROVIDER` | `extractive` (offline, free), `openai`, `anthropic`, `bedrock` |
| `LLM_MODEL` | override the provider's default model |
| `MAX_UPLOAD_MB`, `MAX_DOCS_PER_USER`, `DAILY_QUESTION_QUOTA`, `MAX_CONCURRENT_LLM` | quotas & overload control |
| `HTTPS_ONLY_COOKIES` | `true` behind HTTPS (secure cookies + HSTS) |

## Project layout

```
app/            application (see docs/ARCHITECTURE.md for each module)
tests/          pytest: test_rag, test_memory, test_security; e2e/ Playwright; dialogues/ JSON
scripts/        setup.ps1, run.ps1, test.ps1, seed_demo.py, make_sample_docs.py, capture_screenshots.py
sample_docs/    demo corpus (.md .txt .pdf .docx) + a prompt-injection document used by security tests
docs/           proposal, architecture, PostgreSQL target schema, screenshots
```

## Known POC limitations

* SQLite instead of PostgreSQL/pgvector (same schema design; see `docs/postgres_schema.sql`).
* No OCR: scanned PDFs are rejected with a clear message.
* Token counts use a regex word/punctuation tokenizer, not a model tokenizer.
* The extractive answerer quotes sentences; natural-language answers need an LLM provider.
* AWS deployment, CloudWatch, Windows service and the patchright-enhanced assessment are designed, not built, in the POC.

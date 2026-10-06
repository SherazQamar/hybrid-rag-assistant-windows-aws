# Proof of Capability - Document Knowledge Assistant

**Scope:** a focused, working proof that we can deliver this project, not the full fixed-price delivery.
It runs on a Windows PC today with one command and covers the hardest technical parts of every
requirement section. Section 3 lists what moves to the full project.

## 1. What the client can see working (live demo, ~15 min)

| # | Demo step | Requirement covered |
|---|---|---|
| 1 | Log in as `alice`, ask policy questions, open **Sources** -> file, page/section, chunk, score | §1 UI, §3 references |
| 2 | Follow-up "and its capacity?" is answered from chat context | §2 within-chat memory |
| 3 | "Remember that ..." -> approval card -> approve -> new chat recalls it; restart the app -> still recalled | §2 memory across chats/restart, bounded agent action |
| 4 | Ask "capital of Mongolia?" -> refusal, no sources | §3 unsupported questions |
| 5 | Upload .md / .txt / .pdf / .docx -> live status queued -> processing -> ready; a fake PDF is rejected | §3 validation, extraction, status, errors |
| 6 | `bob` cannot see alice's private document, chats or memory, even with edited IDs (404) | §2/§3/§5 isolation |
| 7 | Admin -> **Retrieval inspector**: dense cosine + BM25 + fused rank per chunk | §3 admin inspection |
| 8 | Admin -> **Profiles**: change chunk size/unit/overlap 0, preview chunks, export/import JSON, 3 examples | §3 settings without Python |
| 9 | Admin -> **Index versions**: simulated failed rebuild stays inactive; real rebuild switches atomically | §3 versioned reindexing |
| 10 | Admin -> **Users**: deactivate -> that user's session dies instantly; MFA (TOTP + QR) | §5 auth |
| 11 | `pytest` -> 63 backend/security tests; Playwright dialogues + 5 parallel browser users | §5, §6 testing |

## 2. Evidence produced by this POC (measured on this PC)

* **Backend + security suite:** 63 passed (offline embedder) and 63 passed with the real `bge-small` model
  (`.\scripts\test.ps1` / `-RealEmbeddings`).
* **Browser (Python Playwright, real UI):** 3 JSON dialogues asserting facts **and** sources, plus login/role/redirect checks: 6 passed; parallel-users test: passed.
* **Parallel users:** 5 separate browsers × 10 exchanges, 1 s think time -> 50 requests, 0 failures,
  median 0.5-1.5 s, p95 1.1-2.3 s across runs (first run after a restart includes model warm-up), all 5 users active together for 21-35 s (`reports/parallel_5_users.json`).
  These numbers are for the offline extractive answerer; with a hosted LLM the latency is dominated
  by the model and will be agreed as an acceptance threshold before contracting.
* Screenshots of every page: `docs/screenshots/`.

## 3. POC vs full project (honest boundary)

| Area | In this POC | Full project adds |
|---|---|---|
| Database | SQLite (WAL) with FTS5 BM25 + vector BLOBs, same table design | PostgreSQL 16 + pgvector + full-text on EC2 t4g.small / Lightsail, migrations, backups/restore (`docs/postgres_schema.sql`) |
| Model | Offline extractive answers (no key, $0) + OpenAI / Anthropic / Bedrock switch via `.env` | Chosen model, streaming, cost tracking per user |
| Network | Binds to 127.0.0.1 | CloudFront + WAF -> EC2 (origin-locked) -> **SSH-over-SSM reverse tunnel** to Windows; no router ports, no 3rd-party tunnels (`docs/ARCHITECTURE.md`) |
| Windows service | `scripts\run.ps1` | Non-admin service account, auto start/restart, tunnel watchdog, reboot/network-loss tests |
| Security tests | 40+ pytest HTTP tests + Playwright auth/role checks | Authorized testing with the pinned `patchright-enhanced` revision, Python orchestration, report with evidence and remaining risks |
| Load | 5 parallel browsers locally | 5/10/25/50 users through AWS, streaming first-token latency, resource use, load-generator limits |
| Monitoring | Usage & errors page, p50/p95 | CloudWatch logs/metrics/alarms, email alerts, retention |
| Docs | README, architecture, this proposal | Screenshot DOCX manual + narrated MP4s with matching step numbers |

## 4. Indicative recurring AWS cost (to verify with the AWS Pricing Calculator for the client's region)

| Item | Approx. USD / month |
|---|---|
| EC2 t4g.small (2 vCPU, 2 GB) on-demand, or Lightsail 2 GB plan | 10 - 13 |
| 20-30 GB gp3 storage | 2 - 3 |
| Public IPv4 address | ~3.6 |
| CloudFront (light traffic, within free allowances) | 0 - 1 |
| AWS WAF (1 web ACL + a few managed rules + requests) | 7 - 10 |
| SSM Session Manager | 0 |
| CloudWatch logs/metrics/alarms (small volume) | 2 - 5 |
| Backups (EBS snapshots / S3 pg_dump, 14-day retention) | 1 - 3 |
| **Infrastructure total** | **≈ 25 - 40** |
| LLM/embedding API | $0 with local embeddings + extractive; otherwise usage-based per provider |

Temporary free-tier benefits are excluded from the totals and will be listed separately in the bid.

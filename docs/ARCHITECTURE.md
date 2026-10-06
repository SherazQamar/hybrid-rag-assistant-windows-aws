# Architecture - Document Knowledge Assistant

## 1. What runs where

```
 Remote users ── HTTPS ──► CloudFront (+ AWS WAF managed rules, rate limit)
                               │  origin: custom header secret + HTTPS
                               ▼
                    EC2 t4g.small  (Amazon Linux 2023, private-ish: SG allows 443 ONLY from
                    │               the CloudFront managed prefix list; no SSH port open)
                    │  nginx :443  ──► 127.0.0.1:8443 (reverse-forwarded port)
                    │  PostgreSQL 16 + pgvector, listening on 127.0.0.1 only
                    │  SSM Agent
                    ▲
                    │  OUTBOUND-only from Windows: SSH session carried INSIDE an
                    │  AWS SSM Session Manager connection (AWS-StartSSHSession document)
                    │    ssh -N -R 127.0.0.1:8443:127.0.0.1:8080   (app  -> EC2, reverse)
                    │        -L 127.0.0.1:5432:127.0.0.1:5432       (DB   -> Windows, forward)
                    │
 Windows host ──────┘  FastAPI + NiceGUI (python -m app.main) on 127.0.0.1:8080
                       runs as a NON-admin service account (NSSM/WinSW service + scheduled task
                       that re-creates the tunnel; auto-restart on crash, reboot and network loss)
```

* **No inbound ports on the home/office router**, no third-party tunnels (ngrok/Cloudflare Tunnel):
  the Windows host only makes an outbound HTTPS connection to the SSM endpoint.
* **Forwarding direction:** `-R` publishes the Windows app on the EC2 loopback (users -> app);
  `-L` lets the app reach PostgreSQL on the EC2 loopback (app -> DB). Neither is exposed publicly.
* **Unattended credentials:** IAM user/role restricted to `ssm:StartSession` on that one instance +
  the `AWS-StartSSHSession` document, stored in the service account's AWS profile (or IAM Roles
  Anywhere certificate for no long-lived keys). SSH key is per-host, `restrict,port-forwarding` in
  `authorized_keys`.
* **Origin restriction:** EC2 security group accepts 443 only from the CloudFront prefix list and
  nginx rejects requests without the secret origin header -> WAF cannot be bypassed by hitting EC2.
* WAF/CloudFront is **one layer**; authentication, authorization, quotas and isolation are enforced
  in the application regardless of what passes the WAF.

## 2. Application components (this POC)

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, error mapping, security headers, health check, startup recovery |
| `app/ui.py` | NiceGUI pages: login, chat, documents, memory/history, MFA, admin console |
| `app/api.py` | REST API (bearer tokens) used by tests/automation |
| `app/security.py` | scrypt hashing, sessions, TOTP MFA, lockout, roles, quotas, concurrency gate |
| `app/ingestion.py` | upload validation (extension + magic bytes + size + zip-bomb/macro checks), extraction, 2 chunkers |
| `app/indexing.py` | document lifecycle, versioned reindex with atomic activation |
| `app/embeddings.py` | fastembed (local bge-small, 384-d), hash (offline), OpenAI, Bedrock Titan |
| `app/retrieval.py` | dense cosine, FTS5 BM25 keyword, RRF / weighted fusion, permission filter in SQL |
| `app/agent.py` | bounded agent: 2 tools, backend ToolPolicy, prompt assembly, chat persistence |
| `app/memory.py` | user memory (approved facts) - separate from history and RAG |
| `app/profiles.py` | admin-editable settings profiles, validation, import/export, 3 examples |

## 3. History vs memory vs RAG

| | Chat history | Long-term memory | RAG |
|---|---|---|---|
| What | Raw messages of one chat | Short approved facts about the user | Chunks of documents |
| Scope | That chat only | Every chat of that user | Docs the user may read (own + shared) |
| Into prompt | Last N messages (`history_messages`) | Top `max_memories` by relevance | Top `final_k` chunks up to `max_context_chars` |
| Written by | Every turn | Only after user approval (or manual add) | Upload + ingestion |
| User control | View / delete per chat / delete all | View / approve / delete / clear | Upload / delete |
| Survives restart | Yes (DB) | Yes (DB) | Yes (DB) |

## 4. Retrieval details (no marketing claims)

* **Dense:** cosine similarity of L2-normalised vectors (exact NumPy scan in the POC; pgvector HNSW in production).
* **Keyword:** SQLite FTS5 `bm25()` (real BM25, k1=1.2, b=0.75), Porter stemmer, stop-words removed, terms OR-ed
  and individually quoted (no query-syntax injection). On PostgreSQL the equivalent is `ts_rank_cd`, which is
  **not** BM25 - the proposal states which one is used.
* **Hybrid:** weighted RRF `w_d/(k+rank_d) + w_k/(k+rank_k)` (default k=60) or weighted min-max score fusion.
* **Unsupported questions:** answered with a refusal unless the best chunk has cosine >= `dense_min_score`
  or covers >= 60% of the query terms. No model call is made for refused questions.

## 5. When to re-chunk / re-embed

| Change | Action |
|---|---|
| chunk strategy / size / unit / overlap / boundaries | re-chunk **and** re-embed (new index version) |
| embedding model or dimension | re-embed everything (new index version); old vectors are never mixed |
| candidate counts, weights, fusion, final_k, thresholds, context sizes | immediate, no reindex |

A reindex builds a new version in the background; the live version keeps serving. The switch is one
transaction (old chunks deleted, new version active). Failed or interrupted builds (including an app crash)
are marked `failed` and their chunks deleted - they can never become active.

## 6. Deletion semantics

* Deleting a document removes all its chunks (dense + keyword rows) from **every** index version in one
  transaction -> it can no longer be retrieved.
* The original upload is deleted from disk (`DELETE_ORIGINALS=true`).
* Previous answers keep their stored citation snippet as part of chat history (the user can delete that chat).
* Backups taken before the deletion still contain it until they expire (retention policy, e.g. 14 days).

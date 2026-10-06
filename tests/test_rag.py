"""Hybrid RAG: file types, retrieval modes, references, unsupported questions, profiles, reindexing."""
import json

import pytest

FACTS = [  # question, expected fact (normalised), expected source file
    ("How many days of paid annual leave do employees receive?", "24 days", "employee_handbook.md"),
    ("What warranty does the SolarBox 3000 include?", "10 year warranty", "product_faq.txt"),
    ("What is the hotel reimbursement limit per night?", "180 us dollars", "travel_policy.docx"),
    ("Where will Northwind open the new engineering office?", "austin", "quarterly_report.pdf"),
]


def test_all_file_types_ingested(env):
    docs = env["admin"].get("/api/documents").json()
    by_name = {d["filename"]: d for d in docs}
    for name in ("employee_handbook.md", "product_faq.txt", "travel_policy.docx", "quarterly_report.pdf"):
        assert by_name[name]["status"] == "ready", by_name[name]
        assert by_name[name]["n_chunks"] > 0


@pytest.mark.parametrize("mode", ["dense", "keyword", "hybrid"])
@pytest.mark.parametrize("question,fact,source", FACTS)
def test_answers_contain_fact_and_source(env, mode, question, fact, source):
    if mode == "dense" and env["settings"].extra["embedding"]["provider"] == "hash":
        pytest.skip("hash embedder is lexical; dense quality is asserted with TEST_EMBEDDING=fastembed")
    alice = env["alice"]
    chat = alice.post("/api/chats").json()["id"]
    res = alice.ask(chat, question, mode)
    assert res["supported"]
    assert fact in res["answer"].lower().replace(",", "")
    assert any(s["ref"].startswith(source) for s in res["sources"]), res["sources"]


def test_pdf_reference_has_page_number(env):
    alice = env["alice"]
    res = alice.ask(alice.post("/api/chats").json()["id"], "When does the Austin office open?", "hybrid")
    assert any(s["ref"].startswith("quarterly_report.pdf") and s["page"] == 2 for s in res["sources"])


def test_unsupported_question_is_refused(env):
    alice = env["alice"]
    res = alice.ask(alice.post("/api/chats").json()["id"], "What is the capital city of Mongolia?", "hybrid")
    assert res["supported"] is False
    assert res["sources"] == []
    assert "could not find" in res["answer"].lower()


def test_admin_sees_component_scores(env):
    r = env["admin"].post("/api/search", json={"query": "SolarBox warranty", "mode": "hybrid"}).json()
    dbg = r["debug"]
    assert dbg["dense_candidates"] and dbg["keyword_candidates"]
    top = dbg["final"][0]
    assert {"fused", "dense", "dense_rank", "bm25", "keyword_rank"} <= set(top)
    assert "product_faq.txt" in top["ref"]
    # normal users get results but not the internal debug data
    assert "debug" not in env["bob"].post("/api/search", json={"query": "SolarBox", "mode": "hybrid"}).json()


def test_profiles_export_import_and_validation(env):
    admin = env["admin"]
    exported = json.loads(admin.get("/api/admin/profiles/balanced-hybrid/export").text)
    exported["name"] = "imported-copy"
    exported["chunking"]["overlap"] = 0
    assert admin.post("/api/admin/profiles/import", json=exported).status_code == 200
    names = [p["name"] for p in admin.get("/api/admin/profiles").json()["profiles"]]
    assert {"balanced-hybrid", "precise-small-chunks", "large-chunks-no-overlap", "imported-copy"} <= set(names)
    bad = dict(exported, name="bad")
    bad["chunking"] = dict(exported["chunking"], overlap=5000)
    assert admin.post("/api/admin/profiles", json=bad).status_code == 400


def test_chunk_preview_token_units_zero_overlap(env):
    admin = env["admin"]
    doc = next(d for d in admin.get("/api/documents").json() if d["filename"] == "product_faq.txt")
    profile = admin.get("/api/admin/profiles").json()["profiles"][0]
    profile["chunking"] = {"strategy": "fixed", "unit": "tokens", "size": 30, "overlap": 0, "boundaries": []}
    prev = admin.post("/api/admin/chunk-preview", json={"doc_id": doc["id"], "profile": profile}).json()
    assert prev["total_chunks"] >= 3
    assert all(c["tokens"] <= 30 for c in prev["chunks"])
    # zero overlap -> consecutive chunks do not share characters
    assert all(a["end_char"] <= b["start_char"] for a, b in zip(prev["chunks"], prev["chunks"][1:]))


def test_reindex_failure_keeps_old_version_then_success_switches(env):
    admin = env["admin"]
    before = next(v for v in admin.get("/api/admin/index-versions").json() if v["status"] == "active")
    assert admin.post("/api/admin/profiles/precise-small-chunks/activate").json()["reindex_needed"] is True

    failed = admin.post("/api/admin/reindex?fail_after=2").json()["version_id"]
    versions = {v["id"]: v for v in admin.get("/api/admin/index-versions").json()}
    assert versions[failed]["status"] == "failed" and versions[failed]["n_chunks"] == 0
    assert versions[before["id"]]["status"] == "active"  # old index still serving

    new = admin.post("/api/admin/reindex").json()["version_id"]
    versions = {v["id"]: v for v in admin.get("/api/admin/index-versions").json()}
    assert versions[new]["status"] == "active"
    assert versions[before["id"]]["status"] == "retired" and versions[before["id"]]["n_chunks"] == 0
    assert admin.get("/api/admin/profiles").json()["reindex_needed"] is False
    alice = env["alice"]
    res = alice.ask(alice.post("/api/chats").json()["id"], "How many days of paid annual leave do employees get?")
    assert "24 days" in res["answer"]
    # restore the default profile for the remaining tests
    admin.post("/api/admin/profiles/balanced-hybrid/activate")
    admin.post("/api/admin/reindex")

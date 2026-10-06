"""Seeds a running instance with demo users and the shared sample corpus via the REST API.

Usage: python scripts/seed_demo.py [--base-url http://127.0.0.1:8080] [--users alice,bob]   (prompts for passwords)
Demo users are created with the password given by --user-password.
"""
import argparse
import getpass
import time
from pathlib import Path

import httpx

SAMPLES = Path(__file__).resolve().parent.parent / "sample_docs"
SHARED = ["employee_handbook.md", "product_faq.txt", "travel_policy.docx", "quarterly_report.pdf"]

ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://127.0.0.1:8080")
ap.add_argument("--admin-user", default="admin")
ap.add_argument("--admin-password", help="omit to be prompted (keeps it out of shell history)")
ap.add_argument("--user-password", help="omit to be prompted")
ap.add_argument("--users", default="alice,bob")
a = ap.parse_args()
a.admin_password = a.admin_password or getpass.getpass("Admin password: ")
a.user_password = a.user_password or getpass.getpass("Password for the demo users (min 10 chars): ")

c = httpx.Client(base_url=a.base_url, timeout=60)
tok = c.post("/api/auth/login", json={"username": a.admin_user, "password": a.admin_password}).raise_for_status().json()["token"]
h = {"Authorization": f"Bearer {tok}"}
for u in a.users.split(","):
    r = c.post("/api/admin/users", headers=h, json={"username": u, "password": a.user_password})
    print("user", u, r.status_code, r.json().get("detail", "created"))
existing = {d["filename"] for d in c.get("/api/documents", headers=h).json()}
for f in SHARED:
    if f not in existing:
        r = c.post("/api/documents", headers=h, files={"file": (f, (SAMPLES / f).read_bytes())}, data={"visibility": "shared"})
        print("upload", f, r.status_code)
for _ in range(60):
    docs = c.get("/api/documents", headers=h).json()
    if all(d["status"] in ("ready", "error") for d in docs):
        break
    time.sleep(1)
for d in docs:
    print(f"  {d['filename']:<28} {d['status']:<8} chunks={d['n_chunks']} {d['error'] or ''}")

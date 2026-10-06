"""REST API (bearer-token auth). Used by automation, tests and integrations; the NiceGUI UI calls the
same service functions in-process. Bearer tokens are not sent automatically by browsers -> no CSRF."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import admin_tools, agent, db, indexing, memory, profiles, retrieval, security
from .config import current
from .security import User

router = APIRouter(prefix="/api")


def auth_user(authorization: str | None = Header(default=None)) -> User:
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    return security.user_from_token(token)


def admin_user(user: User = Depends(auth_user)) -> User:
    security.require_admin(user)
    return user


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    totp: str | None = Field(default=None, max_length=10)


class AskIn(BaseModel):
    question: str = Field(max_length=4000)
    mode: str | None = None


class TextIn(BaseModel):
    content: str = Field(max_length=500)


class NewUserIn(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserPatch(BaseModel):
    active: bool | None = None
    role: str | None = None
    password: str | None = None


class SearchIn(BaseModel):
    query: str = Field(max_length=2000)
    mode: str = "hybrid"


class PreviewIn(BaseModel):
    doc_id: int
    profile: dict


# ---------- auth ----------

@router.post("/auth/login")
def login(body: LoginIn):
    return {"token": security.login(body.username, body.password, body.totp, current())}


@router.post("/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    security.logout(authorization[7:] if authorization else None)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(auth_user)):
    return {"id": user.id, "username": user.username, "role": user.role, "mfa_enabled": user.mfa_enabled}


# ---------- chats ----------

@router.get("/chats")
def chats(user: User = Depends(auth_user)):
    return agent.list_chats(user)


@router.post("/chats")
def new_chat(user: User = Depends(auth_user)):
    return {"id": agent.create_chat(user)}


@router.get("/chats/{chat_id}/messages")
def chat_messages(chat_id: int, user: User = Depends(auth_user)):
    return agent.messages(user, chat_id)


@router.post("/chats/{chat_id}/ask")
def ask(chat_id: int, body: AskIn, user: User = Depends(auth_user)):
    return agent.ask(user, chat_id, body.question, body.mode, current())


@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: int, user: User = Depends(auth_user)):
    agent.delete_chat(user, chat_id)
    return {"ok": True}


@router.delete("/history")
def delete_history(user: User = Depends(auth_user)):
    agent.delete_all_history(user)
    return {"ok": True}


# ---------- memory ----------

@router.get("/memories")
def memories(user: User = Depends(auth_user)):
    return memory.list_for(user)


@router.post("/memories")
def add_memory(body: TextIn, user: User = Depends(auth_user)):
    return {"id": memory.add(user, body.content, "active", "manual")}


@router.post("/memories/{memory_id}/approve")
def approve_memory(memory_id: int, user: User = Depends(auth_user)):
    memory.approve(user, memory_id)
    return {"ok": True}


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(auth_user)):
    memory.delete(user, memory_id)
    return {"ok": True}


# ---------- documents ----------

@router.get("/documents")
def documents(user: User = Depends(auth_user)):
    return indexing.visible_documents(user)


@router.post("/documents")
async def upload(file: UploadFile = File(...), visibility: str = Form("private"), user: User = Depends(auth_user)):
    s = current()
    data = await file.read(s.max_upload_mb * 1024 * 1024 + 1)
    return {"id": indexing.upload(user, file.filename or "upload", data, visibility, s)}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, user: User = Depends(auth_user)):
    indexing.delete_document(user, doc_id, current())
    return {"ok": True}


@router.post("/search")
def search(body: SearchIn, user: User = Depends(auth_user)):
    if body.mode not in profiles.MODES:
        raise ValueError("mode must be dense, keyword or hybrid")
    rr = retrieval.retrieve(body.query, user, body.mode, profiles.active()["retrieval"], current())
    out = {"supported": rr.supported,
           "results": [{"rank": r.final_rank, "ref": r.ref(), "doc_id": r.doc_id, "chunk_id": r.chunk_id,
                        "text": r.text} for r in rr.results]}
    if user.is_admin:
        out["debug"] = rr.debug
    return out


# ---------- admin ----------

@router.get("/admin/users")
def users(_: User = Depends(admin_user)):
    return security.list_users()


@router.post("/admin/users")
def create_user(body: NewUserIn, _: User = Depends(admin_user)):
    return {"id": security.create_user(body.username, body.password, body.role)}


@router.patch("/admin/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, admin: User = Depends(admin_user)):
    if user_id == admin.id and (body.active is False or body.role == "user"):
        raise ValueError("Admins cannot deactivate or demote themselves")
    if body.active is not None:
        security.set_active(user_id, body.active)
    if body.role:
        security.set_role(user_id, body.role)
    if body.password:
        security.reset_password(user_id, body.password)
    return {"ok": True}


@router.get("/admin/profiles")
def list_profiles(_: User = Depends(admin_user)):
    return {"active": profiles.active()["name"], "profiles": profiles.list_all(), "reindex_needed": indexing.reindex_needed()}


@router.post("/admin/profiles")
def save_profile(body: dict, _: User = Depends(admin_user)):
    return profiles.save(body)


@router.post("/admin/profiles/import")
def import_profile(body: dict, _: User = Depends(admin_user)):
    return profiles.save(body)  # same validation as the editor


@router.get("/admin/profiles/{name}/export", response_class=PlainTextResponse)
def export_profile(name: str, _: User = Depends(admin_user)):
    return profiles.export_json(name)


@router.post("/admin/profiles/{name}/activate")
def activate_profile(name: str, _: User = Depends(admin_user)):
    profiles.activate(name)
    return {"ok": True, "reindex_needed": indexing.reindex_needed()}


@router.post("/admin/reindex")
def reindex(fail_after: int | None = None, _: User = Depends(admin_user)):
    return {"version_id": indexing.start_reindex(current(), fail_after)}


@router.get("/admin/index-versions")
def versions(_: User = Depends(admin_user)):
    return indexing.list_versions()


@router.post("/admin/chunk-preview")
def chunk_preview(body: PreviewIn, admin: User = Depends(admin_user)):
    return admin_tools.chunk_preview(body.doc_id, body.profile, current())


@router.get("/admin/usage")
def usage(_: User = Depends(admin_user)):
    return db.query("SELECT l.*, u.username FROM usage_log l LEFT JOIN users u ON u.id = l.user_id "
                    "ORDER BY l.id DESC LIMIT 200")


@router.get("/admin/retrieval-log")
def retrieval_log(_: User = Depends(admin_user)):
    return db.query("SELECT r.id, u.username, r.query, r.mode, r.debug_json, r.created_at FROM retrieval_log r "
                    "LEFT JOIN users u ON u.id = r.user_id ORDER BY r.id DESC LIMIT 50")

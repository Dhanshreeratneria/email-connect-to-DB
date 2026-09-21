from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.models.email import AdminClient, AdminToken
from app.services import auth0
from app.services.admin_auth import MCP_PERMISSIONS, issue_token, revoke_token, set_permissions

router = APIRouter(prefix="/admin")

SESSION_COOKIE = "admin_session"
STATE_COOKIE = "admin_oauth_state"
VERIFIER_COOKIE = "admin_oauth_verifier"


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.token_encryption_key.encode()).digest())
    return Fernet(key)


def _issuer() -> str:
    issuer = settings.auth0_issuer.strip()
    if issuer:
        return issuer.rstrip("/")
    return f"https://{settings.auth0_domain.strip().rstrip('/')}"


def _redirect_uri() -> str:
    return settings.admin_auth0_redirect_uri.strip() or (
        f"{settings.public_base_url.rstrip('/')}/admin/auth/callback"
    )


def _session_claims(cookie: str | None) -> dict | None:
    if not cookie:
        return None
    try:
        claims = json.loads(_fernet().decrypt(cookie.encode()).decode())
        if claims.get("exp", 0) <= int(datetime.now(timezone.utc).timestamp()):
            return None
        return claims
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return None


class ClientInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    permissions: set[str] = set()


def require_admin(admin_session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> dict:
    claims = _session_claims(admin_session)
    if claims is None:
        raise HTTPException(status_code=401, detail="Admin login required")
    if not auth0.is_admin(claims):
        raise HTTPException(status_code=403, detail="Admin permission required")
    return claims


def serialize_client(client: AdminClient) -> dict:
    return {
        "id": client.id,
        "name": client.name,
        "enabled": client.enabled,
        "permissions": sorted(permission.permission for permission in client.permissions),
        "tokens": [
            {
                "id": token.id,
                "prefix": token.token_prefix,
                "revoked": token.revoked_at is not None,
                "created_at": token.created_at.isoformat() if token.created_at else None,
            }
            for token in client.tokens
        ],
        "created_at": client.created_at.isoformat() if client.created_at else None,
    }


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def admin_dashboard(admin_session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if _session_claims(admin_session) is None:
        return RedirectResponse("/admin/login", status_code=303)
    return ADMIN_HTML


@router.get("/login", include_in_schema=False)
def admin_login():
    if not settings.auth0_domain or not settings.auth0_client_id:
        raise HTTPException(status_code=503, detail="Auth0 admin login is not configured")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({
        "response_type": "code",
        "client_id": settings.auth0_client_id,
        "redirect_uri": _redirect_uri(),
        "scope": "openid profile email admin:manage",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "audience": settings.auth0_api_audience,
        "state": state,
    })
    redirect = RedirectResponse(f"{_issuer()}/authorize?{query}", status_code=303)
    redirect.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="lax", path="/admin")
    redirect.set_cookie(VERIFIER_COOKIE, verifier, max_age=600, httponly=True, secure=True, samesite="lax", path="/admin")
    return redirect


@router.get("/auth/callback", include_in_schema=False)
async def admin_callback(
    code: str,
    state: str,
    admin_oauth_state: str | None = Cookie(default=None, alias=STATE_COOKIE),
    admin_oauth_verifier: str | None = Cookie(default=None, alias=VERIFIER_COOKIE),
):
    if not admin_oauth_state or not admin_oauth_verifier or not secrets.compare_digest(state, admin_oauth_state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    form = {
        "grant_type": "authorization_code",
        "client_id": settings.auth0_client_id,
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": admin_oauth_verifier,
    }
    if settings.auth0_client_secret:
        form["client_secret"] = settings.auth0_client_secret
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(f"{_issuer()}/oauth/token", data=form)
        token_response.raise_for_status()
        claims = auth0.safe_validate_token(token_response.json().get("access_token"))
    except (httpx.HTTPError, ValueError, TypeError):
        claims = None
    if claims is None:
        raise HTTPException(status_code=401, detail="Unable to validate Auth0 admin login")
    if not auth0.is_admin(claims):
        raise HTTPException(status_code=403, detail="Admin permission required")

    session_claims = {
        "sub": claims.get("sub"),
        "scope": claims.get("scope", ""),
        "permissions": claims.get("permissions", []),
        "exp": int(datetime.now(timezone.utc).timestamp()) + settings.admin_session_ttl_seconds,
    }
    session = _fernet().encrypt(json.dumps(session_claims).encode()).decode()
    redirect = RedirectResponse("/admin", status_code=303)
    redirect.delete_cookie(STATE_COOKIE, path="/admin")
    redirect.delete_cookie(VERIFIER_COOKIE, path="/admin")
    redirect.set_cookie(SESSION_COOKIE, session, max_age=settings.admin_session_ttl_seconds, httponly=True, secure=True, samesite="lax", path="/admin")
    return redirect


@router.post("/logout", include_in_schema=False)
def admin_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return {"status": "logged_out"}


@router.get("/api/clients")
def list_clients(_: dict = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    clients = db.scalars(select(AdminClient).order_by(AdminClient.created_at.desc())).all()
    return [serialize_client(client) for client in clients]


@router.post("/api/clients", status_code=201)
def create_client(
    payload: ClientInput,
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    invalid = payload.permissions - MCP_PERMISSIONS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown permissions: {sorted(invalid)}")
    client = AdminClient(name=payload.name, enabled=payload.enabled)
    set_permissions(client, payload.permissions)
    db.add(client)
    db.flush()
    token, raw_token = issue_token(db, client)
    db.commit()
    db.refresh(client)
    return {"client": serialize_client(client), "token": raw_token, "token_id": token.id}


@router.patch("/api/clients/{client_id}")
def update_client(
    client_id: int,
    payload: ClientInput,
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    client = db.get(AdminClient, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    invalid = payload.permissions - MCP_PERMISSIONS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown permissions: {sorted(invalid)}")
    client.name = payload.name
    client.enabled = payload.enabled
    set_permissions(client, payload.permissions)
    db.commit()
    db.refresh(client)
    return serialize_client(client)


@router.post("/api/clients/{client_id}/tokens")
def create_token(
    client_id: int,
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    client = db.get(AdminClient, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    token, raw_token = issue_token(db, client)
    db.commit()
    return {"token": raw_token, "token_id": token.id, "client_id": client.id}


@router.post("/api/tokens/{token_id}/revoke")
def revoke_client_token(
    token_id: int,
    _: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    token = db.get(AdminToken, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")
    revoke_token(token)
    db.commit()
    return {"status": "revoked", "token_id": token.id}


ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gmail MCP Admin</title><style>
body{font:15px system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px;color:#17202a;background:#f5f3ed}
h1{font-size:32px;margin-bottom:6px}.muted{color:#667085}form,.client{background:white;border:1px solid #ddd8cd;padding:18px;margin:16px 0;border-radius:8px}
input,button{font:inherit;padding:9px;margin:4px 4px 4px 0}button{cursor:pointer;background:#17202a;color:white;border:0;border-radius:4px}
label{margin-right:14px}.client{display:grid;gap:8px}.token{background:#fff6d7;padding:10px;word-break:break-all}
</style></head><body><h1>MCP client access</h1><p class="muted">Create scoped bearer credentials for MCP consumers.</p>
<form id="create"><input name="name" placeholder="Client name" required><label><input name="read:emails" type="checkbox" checked> read:emails</label><label><input name="read:attachments" type="checkbox"> read:attachments</label><label><input name="download:attachments" type="checkbox"> download:attachments</label><button>Create client</button></form>
<div id="output"></div><main id="clients"></main><script>
const perms=['read:emails','read:attachments','download:attachments']; const out=document.querySelector('#output');
async function api(url,opt={}){const r=await fetch(url,{...opt,headers:{'Content-Type':'application/json',...(opt.headers||{})}});const x=await r.json();if(!r.ok)throw Error(x.detail||'Request failed');return x}
async function load(){const cs=await api('/admin/api/clients');document.querySelector('#clients').innerHTML=cs.map(c=>`<section class="client"><input id="name-${c.id}" value="${c.name.replaceAll('"','&quot;')}"><label><input id="enabled-${c.id}" type="checkbox" ${c.enabled?'checked':''}> enabled</label>${perms.map(p=>`<label><input id="${p}-${c.id}" type="checkbox" ${c.permissions.includes(p)?'checked':''}> ${p}</label>`).join('')}<button onclick="save(${c.id})">Save</button><span>Tokens: ${c.tokens.map(t=>t.prefix+' '+(t.revoked?'revoked':'active')+' <button onclick="revoke('+t.id+')">Revoke</button>').join(' ')||'none'}</span><button onclick="newToken(${c.id})">Generate token</button></section>`).join('')}
async function save(id){await api('/admin/api/clients/'+id,{method:'PATCH',body:JSON.stringify({name:document.querySelector('#name-'+id).value,enabled:document.querySelector('#enabled-'+id).checked,permissions:perms.filter(p=>document.querySelector('#'+p+'-'+id).checked)})});load()}
document.querySelector('#create').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{const x=await api('/admin/api/clients',{method:'POST',body:JSON.stringify({name:f.get('name'),enabled:true,permissions:perms.filter(p=>f.get(p))})});out.innerHTML='<div class="token">Copy this token now. It will not be shown again: <b>'+x.token+'</b></div>';e.target.reset();load()}catch(err){out.textContent=err.message}}
async function newToken(id){try{const x=await api('/admin/api/clients/'+id+'/tokens',{method:'POST'});out.innerHTML='<div class="token">Copy this token now. It will not be shown again: <b>'+x.token+'</b></div>';load()}catch(err){out.textContent=err.message}}
async function revoke(id){await api('/admin/api/tokens/'+id+'/revoke',{method:'POST'});load()} load();
</script></body></html>"""
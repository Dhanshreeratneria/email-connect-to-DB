from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import AdminClient, AdminToken
from app.services import auth0
from app.services.admin_auth import MCP_PERMISSIONS, issue_token, revoke_token, set_permissions

router = APIRouter(prefix="/admin")


class ClientInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    permissions: set[str] = set()


def require_admin(request: Request) -> dict:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    claims = auth0.safe_validate_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Missing or invalid Auth0 access token")
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
def admin_dashboard(_: dict = Depends(require_admin)) -> str:
    return ADMIN_HTML


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
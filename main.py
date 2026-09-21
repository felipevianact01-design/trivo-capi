"""
Trivo CAPI — Servidor multi-tenant de rastreamento
Recebe eventos do GTM e reenvia para Meta CAPI + Google Ads.

Cada cliente é identificado pelo seu PIXEL_ID da Meta.
Um único servidor atende todos os clientes da Trivo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAMADA CRM (desativada por padrão)
Quando um cliente tiver Kommo ou Bolten:
  1. Descomentar a rota /webhook/{client_id}/crm
  2. Adicionar variáveis CRM do cliente no Render
  3. Apontar webhook do CRM para essa rota
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import json
import time
import hashlib
import hmac
import logging
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Trivo CAPI", version="1.0.0")

# ─────────────────────────────────────────────
# Carrega clientes do ambiente
# Formato: CLIENTS_JSON = JSON string com lista de clientes
# Exemplo: [{"id":"wmb","pixel_id":"123","token":"abc","google_id":"456"}]
# ─────────────────────────────────────────────

def load_clients() -> dict:
    raw = os.environ.get("CLIENTS_JSON", "[]")
    try:
        clients_list = json.loads(raw)
        return {c["id"]: c for c in clients_list}
    except Exception as e:
        logger.error(f"Erro ao carregar CLIENTS_JSON: {e}")
        return {}


def hash_value(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def normalize_phone(phone: str) -> str:
    digits = "".join(filter(str.isdigit, phone))
    if len(digits) in (10, 11) and not digits.startswith("55"):
        digits = "55" + digits
    return "+" + digits if not digits.startswith("+") else digits


# ─────────────────────────────────────────────
# Rota principal: recebe evento do GTM
# ─────────────────────────────────────────────

@app.post("/evento/{client_id}")
async def receber_evento(client_id: str, request: Request):
    """
    Recebe um evento disparado pelo GTM e reenvia para Meta CAPI.

    Payload esperado (JSON):
    {
        "event_name": "Lead",          // PageView, Lead, Purchase, etc.
        "event_id": "uid-unico-123",   // para deduplicação com o Pixel
        "email": "fulano@email.com",   // opcional, melhora o match
        "phone": "11999998888",        // opcional, melhora o match
        "value": 0,                    // opcional, para Purchase
        "currency": "BRL",             // opcional, padrão BRL
        "source_url": "https://...",   // URL da página
        "ip": "1.2.3.4",              // IP do usuário
        "user_agent": "Mozilla/...",   // User-Agent do navegador
        "fbc": "_fbc_...",             // cookie _fbc (clique no anúncio)
        "fbp": "_fbp_..."              // cookie _fbp (browser)
    }
    """
    clients = load_clients()

    if client_id not in clients:
        raise HTTPException(status_code=404, detail=f"Cliente '{client_id}' não encontrado")

    client = clients[client_id]
    pixel_id = client.get("pixel_id")
    token = client.get("meta_token")

    if not pixel_id or not token:
        raise HTTPException(status_code=500, detail="Pixel ID ou token Meta não configurados")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    event_name = body.get("event_name", "Lead")
    event_id = body.get("event_id", str(time.time()))
    source_url = body.get("source_url", "")
    ip = body.get("ip", "")
    user_agent = body.get("user_agent", "")
    fbc = body.get("fbc", "")
    fbp = body.get("fbp", "")
    email = body.get("email", "")
    phone = body.get("phone", "")
    value = body.get("value", 0)
    currency = body.get("currency", "BRL")

    # Monta user_data com Advanced Matching
    user_data = {}
    if email:
        user_data["em"] = [hash_value(email)]
    if phone:
        user_data["ph"] = [hash_value(normalize_phone(phone))]
    if ip:
        user_data["client_ip_address"] = ip
    if user_agent:
        user_data["client_user_agent"] = user_agent
    if fbc:
        user_data["fbc"] = fbc
    if fbp:
        user_data["fbp"] = fbp

    # Monta evento CAPI
    evento = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "event_source_url": source_url,
        "action_source": "website",
        "user_data": user_data,
    }

    if event_name == "Purchase" and value:
        evento["custom_data"] = {"value": value, "currency": currency}

    payload = {
        "data": [evento],
        "test_event_code": client.get("test_code", ""),
    }

    # Remove test_event_code se vazio
    if not payload["test_event_code"]:
        del payload["test_event_code"]

    url = f"https://graph.facebook.com/v20.0/{pixel_id}/events?access_token={token}"

    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(url, json=payload)

    if resp.status_code == 200:
        logger.info(f"[{client_id}] CAPI OK — {event_name} | event_id={event_id}")
        return {"status": "ok", "event": event_name, "client": client_id}
    else:
        logger.error(f"[{client_id}] CAPI ERRO — {resp.status_code}: {resp.text}")
        return JSONResponse(
            status_code=502,
            content={"status": "erro_meta", "detalhe": resp.text}
        )


# ─────────────────────────────────────────────
# Health check e diagnóstico
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "online", "servico": "Trivo CAPI", "versao": "1.0.0"}


@app.get("/clientes")
def listar_clientes():
    clients = load_clients()
    return {
        "total": len(clients),
        "clientes": [
            {
                "id": cid,
                "pixel_id": c.get("pixel_id", ""),
                "tem_token": bool(c.get("meta_token")),
            }
            for cid, c in clients.items()
        ],
    }


@app.get("/verificar/{client_id}")
def verificar_cliente(client_id: str):
    clients = load_clients()
    if client_id not in clients:
        return {"status": "nao_encontrado", "client_id": client_id}
    c = clients[client_id]
    return {
        "status": "ok",
        "client_id": client_id,
        "pixel_id": c.get("pixel_id", ""),
        "tem_meta_token": bool(c.get("meta_token")),
        "tem_google_id": bool(c.get("google_ads_id")),
    }


# ─────────────────────────────────────────────
# CAMADA CRM — desativada por padrão
# Descomentar quando o cliente tiver Kommo ou Bolten
# ─────────────────────────────────────────────

# @app.post("/webhook/{client_id}/kommo")
# async def webhook_kommo(client_id: str, request: Request, status: str = "novo"):
#     """
#     Recebe webhook do Kommo quando lead muda de etapa.
#     Envia evento de qualificação ou venda para Meta CAPI.
#
#     Variáveis necessárias no cliente:
#       kommo_token, kommo_subdominio
#     """
#     # TODO: implementar busca de telefone + envio CAPI
#     pass


# @app.post("/webhook/{client_id}/bolten")
# async def webhook_bolten(client_id: str, request: Request, status: str = "novo"):
#     """
#     Recebe webhook do Bolten quando lead muda de etapa.
#     Bolten já envia telefone no JSON — mais simples que Kommo.
#
#     Variáveis necessárias no cliente:
#       bolten_api_key
#     """
#     # TODO: implementar leitura do JSON + envio CAPI
#     pass

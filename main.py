"""
Trivo CAPI — Servidor multi-tenant de rastreamento
Recebe eventos do GTM e reenvia para Meta CAPI e/ou Google Ads.

Cada cliente pode ter Meta, Google, ou ambos.
Um único servidor atende todos os clientes da Trivo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROVIDERS SUPORTADOS

Meta (CAPI):
  pixel_id + meta_token → envia via Meta Conversions API

Google Ads (Enhanced Conversions):
  google_ads_id + google_conv_id → sobe conversão via API
  Usa credenciais globais do Render (GOOGLE_*) ou por-cliente.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CLIENTES_JSON — campos por cenário

Só Meta:
  {"id":"wmb","pixel_id":"123","meta_token":"abc"}

Só Google:
  {"id":"ortoclub","google_ads_id":"6742417809","google_conv_id":"987654321"}

Meta + Google:
  {"id":"cliente","pixel_id":"123","meta_token":"abc",
   "google_ads_id":"456","google_conv_id":"987654321"}

CRM adicional (opcional):
  kommo_token + kommo_subdominio → Kommo
  bolten_api_key                 → Bolten

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VARIÁVEIS DE AMBIENTE DO RENDER

CLIENTS_JSON              — lista de clientes (JSON)
GOOGLE_CLIENT_ID          — OAuth do Google Ads (global)
GOOGLE_CLIENT_SECRET      — OAuth do Google Ads (global)
GOOGLE_REFRESH_TOKEN      — OAuth do Google Ads (global)
GOOGLE_DEVELOPER_TOKEN    — developer token Google Ads
GOOGLE_MCC_ID             — MCC gerenciadora (ex: 5061973846)
GOOGLE_ADS_API_VERSION    — versão da API (padrão: v21)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAMADA CRM (desativada por padrão)
Quando um cliente tiver Kommo ou Bolten:
  1. Descomentar a rota /webhook/{client_id}/kommo (ou /bolten)
  2. Adicionar variáveis CRM do cliente no CLIENTS_JSON
  3. Apontar webhook do CRM para essa rota
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import re
import json
import time
import hashlib
import logging
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Trivo CAPI", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

GOOGLE_ADS_API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION", "v21")
GTM_ACCOUNT_ID = os.environ.get("GTM_ACCOUNT_ID", "6378805007")
GA4_ACCOUNT_ID = os.environ.get("GA4_ACCOUNT_ID", "269067750")
ONBOARDING_KEY = os.environ.get("TRIVO_ONBOARDING_KEY", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
ONBOARDING_USER = os.environ.get("ONBOARDING_USER", "")
ONBOARDING_PASS = os.environ.get("ONBOARDING_PASS", "")
INVITE_CODE     = os.environ.get("INVITE_CODE", "")

# ─────────────────────────────────────────────
# Helpers
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


async def get_google_access_token(client: dict) -> str | None:
    """Obtém access_token via refresh_token. Usa credenciais por-cliente ou globais."""
    client_id = client.get("google_client_id") or os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = client.get("google_client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = client.get("google_refresh_token") or os.environ.get("GOOGLE_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        return None

    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code == 200:
        return resp.json().get("access_token")
    logger.error(f"Erro ao obter access_token Google: {resp.text}")
    return None


async def enviar_google_ads(client: dict, body: dict) -> dict:
    """
    Envia conversão para Google Ads API via uploadClickConversions.
    Requer: google_ads_id, google_conv_id (ID numérico da conversão).
    Opcional: gclid no body (para atribuição por clique).
    """
    google_ads_id = client.get("google_ads_id", "").replace("-", "")
    conv_id = client.get("google_conv_id", "")
    developer_token = os.environ.get("GOOGLE_DEVELOPER_TOKEN", "")
    mcc_id = client.get("google_mcc_id") or os.environ.get("GOOGLE_MCC_ID", "")

    if not google_ads_id or not conv_id:
        return {"status": "skip", "motivo": "google_ads_id ou google_conv_id ausentes"}

    if not developer_token:
        return {"status": "skip", "motivo": "GOOGLE_DEVELOPER_TOKEN não configurado"}

    access_token = await get_google_access_token(client)
    if not access_token:
        return {"status": "skip", "motivo": "credenciais Google OAuth não configuradas"}

    gclid = body.get("gclid", "")
    email = body.get("email", "")
    phone = body.get("phone", "")
    value = body.get("value", 0)
    currency = body.get("currency", "BRL")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")

    conversion = {
        "conversionAction": f"customers/{google_ads_id}/conversionActions/{conv_id}",
        "conversionDateTime": now,
        "conversionValue": float(value) if value else 0.0,
        "currencyCode": currency,
    }

    if gclid:
        conversion["gclid"] = gclid

    # Enhanced Conversions: hash email/phone se disponíveis
    user_identifiers = []
    if email:
        user_identifiers.append({"hashedEmail": hash_value(email)})
    if phone:
        user_identifiers.append({"hashedPhoneNumber": hash_value(normalize_phone(phone))})
    if user_identifiers:
        conversion["userIdentifiers"] = user_identifiers

    payload = {"conversions": [conversion], "partialFailure": True}

    url = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/customers/{google_ads_id}:uploadClickConversions"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }
    if mcc_id:
        headers["login-customer-id"] = str(mcc_id)

    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(url, json=payload, headers=headers)

    if resp.status_code == 200:
        return {"status": "ok"}
    else:
        return {"status": "erro", "detalhe": resp.text[:300]}


# ─────────────────────────────────────────────
# Onboarding — cria GTM + GA4 para novo cliente
# ─────────────────────────────────────────────

def _slug(nome: str) -> str:
    s = nome.lower()
    for src, dst in [("áàãâä","a"),("éèêë","e"),("íìîï","i"),("óòõôö","o"),("úùûü","u"),("ç","c")]:
        for c in src:
            s = s.replace(c, dst)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


async def _gtm_access_token() -> str:
    token = await get_google_access_token({})
    if not token:
        raise HTTPException(status_code=500, detail="Falha ao obter credenciais Google (GTM/GA4)")
    return token


async def _criar_gtm_container(token: str, nome: str, url: str, account_id: str) -> dict:
    domain = re.sub(r"https?://", "", url).rstrip("/")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(
            f"https://tagmanager.googleapis.com/tagmanager/v2/accounts/{account_id}/containers",
            json={"name": nome, "usageContext": ["WEB"], "domainName": [domain]},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    r.raise_for_status()
    c = r.json()
    return {"container_id": c["containerId"], "public_id": c["publicId"]}


async def _criar_ga4(token: str, nome: str, url: str, account_id: str) -> dict:
    domain = re.sub(r"https?://", "", url).rstrip("/")
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(
            "https://analyticsadmin.googleapis.com/v1beta/properties",
            json={"parent": f"accounts/{account_id}", "displayName": nome,
                  "timeZone": "America/Sao_Paulo", "currencyCode": "BRL", "industryCategory": "OTHER"},
            headers=h,
        )
        r.raise_for_status()
        property_id = r.json()["name"].split("/")[-1]
        r2 = await http.post(
            f"https://analyticsadmin.googleapis.com/v1beta/properties/{property_id}/dataStreams",
            json={"type": "WEB_DATA_STREAM", "displayName": f"{nome} — Web",
                  "webStreamData": {"defaultUri": f"https://{domain}"}},
            headers=h,
        )
        r2.raise_for_status()
        measurement_id = r2.json().get("webStreamData", {}).get("measurementId", "")
    return {"property_id": property_id, "measurement_id": measurement_id}


async def _criar_gtm_entities(token: str, account_id: str, container_id: str,
                               client_id: str, pixel_id: str, google_ads_tag: str,
                               label: str, measurement_id: str) -> str:
    """Cria workspace + todas as variáveis, acionadores e tags. Retorna workspace_id."""
    base = f"https://tagmanager.googleapis.com/tagmanager/v2/accounts/{account_id}/containers/{container_id}"
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as http:

        ws = await http.post(f"{base}/workspaces",
                             json={"name": "Trivo Setup", "description": "Criado automaticamente"},
                             headers=h)
        ws.raise_for_status()
        ws_id = ws.json()["workspaceId"]
        wb = f"{base}/workspaces/{ws_id}"

        # Built-in variables
        biv_types = ["CLICK_URL", "CLICK_TEXT", "CLICK_ELEMENT", "CLICK_CLASSES", "CLICK_ID", "CLICK_TARGET"]
        await http.post(wb + "/built_in_variables?" + "&".join(f"type={t}" for t in biv_types), headers=h)

        async def cv(body):
            r = await http.post(f"{wb}/variables", json=body, headers=h)
            if r.status_code not in (200, 201):
                logger.warning(f"variável '{body.get('name')}' retornou {r.status_code}: {r.text[:120]}")

        # Custom variables — Meta
        if pixel_id:
            await cv({"name": "Meta - Pixel ID", "type": "c",
                      "parameter": [{"type": "template", "key": "value", "value": pixel_id}]})
            await cv({"name": "Event ID", "type": "jsm",
                      "parameter": [{"type": "template", "key": "javascript",
                                     "value": "function(){return(Math.random().toString(36).substring(2)+Date.now().toString(36));}"}]})
            await cv({"name": "Cookie - _fbc", "type": "k",
                      "parameter": [{"type": "template", "key": "name", "value": "_fbc"},
                                    {"type": "boolean", "key": "decodeCookie", "value": "false"}]})
            await cv({"name": "Cookie - _fbp", "type": "k",
                      "parameter": [{"type": "template", "key": "name", "value": "_fbp"},
                                    {"type": "boolean", "key": "decodeCookie", "value": "false"}]})
            await cv({"name": "Trivo CAPI - Client ID", "type": "c",
                      "parameter": [{"type": "template", "key": "value", "value": client_id}]})
            await cv({"name": "Trivo CAPI - URL", "type": "c",
                      "parameter": [{"type": "template", "key": "value", "value": "https://trivo-capi.onrender.com"}]})

        # Custom variables — Google
        if google_ads_tag:
            await cv({"name": "Google Ads - Tag ID", "type": "c",
                      "parameter": [{"type": "template", "key": "value", "value": google_ads_tag}]})
            if label:
                await cv({"name": "Google Ads - Label Botão", "type": "c",
                          "parameter": [{"type": "template", "key": "value", "value": label}]})
        if measurement_id:
            await cv({"name": "GA4 - Measurement ID", "type": "c",
                      "parameter": [{"type": "template", "key": "value", "value": measurement_id}]})

        # Triggers
        pv = await http.post(f"{wb}/triggers",
                             json={"name": "All Pages", "type": "PAGEVIEW"}, headers=h)
        pv.raise_for_status()
        pv_id = pv.json()["triggerId"]

        wa = await http.post(f"{wb}/triggers", json={
            "name": "Clique - Botão WhatsApp", "type": "CLICK",
            "parameter": [
                {"type": "boolean", "key": "waitForTags", "value": "true"},
                {"type": "template", "key": "waitForTagsTimeout", "value": "2000"},
                {"type": "boolean", "key": "checkValidation", "value": "false"},
                {"type": "list", "key": "filters", "list": [{"type": "map", "map": [
                    {"type": "template", "key": "type", "value": "CONTAINS"},
                    {"type": "template", "key": "attribute", "value": "{{Click URL}}"},
                    {"type": "template", "key": "value", "value": "wa.me"},
                ]}]},
            ]}, headers=h)
        wa.raise_for_status()
        wa_id = wa.json()["triggerId"]

        sol = await http.post(f"{wb}/triggers", json={
            "name": "Clique - Botão SOLICITAR ORÇAMENTO", "type": "CLICK",
            "parameter": [
                {"type": "boolean", "key": "waitForTags", "value": "true"},
                {"type": "template", "key": "waitForTagsTimeout", "value": "2000"},
                {"type": "boolean", "key": "checkValidation", "value": "false"},
                {"type": "list", "key": "filters", "list": [{"type": "map", "map": [
                    {"type": "template", "key": "type", "value": "CONTAINS"},
                    {"type": "template", "key": "attribute", "value": "{{Click Text}}"},
                    {"type": "template", "key": "value", "value": "SOLICITAR"},
                ]}]},
            ]}, headers=h)
        sol.raise_for_status()
        sol_id = sol.json()["triggerId"]

        async def ct(body):
            r = await http.post(f"{wb}/tags", json=body, headers=h)
            if r.status_code not in (200, 201):
                logger.warning(f"tag '{body.get('name')}' retornou {r.status_code}: {r.text[:120]}")

        # Tags — Meta
        if pixel_id:
            pv_html = (
                "<script>\n"
                "!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?"
                "n.callMethod.apply(n,arguments):n.queue.push(arguments)};if(!f._fbq)f._fbq=n;"
                "n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;"
                "t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}"
                f"(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');\n"
                f"fbq('init','{pixel_id}');\nfbq('track','PageView');\n</script>"
            )
            await ct({"name": "Meta Pixel - PageView", "type": "html",
                      "parameter": [{"type": "template", "key": "html", "value": pv_html},
                                    {"type": "boolean", "key": "supportDocumentWrite", "value": "false"}],
                      "firingTriggerId": [str(pv_id)]})

            capi_pv_html = (
                "<script>\n(function(){\n"
                "var p={event_name:'PageView',event_id:'{{Event ID}}',source_url:window.location.href,"
                "user_agent:navigator.userAgent,fbc:'{{Cookie - _fbc}}',fbp:'{{Cookie - _fbp}}'};\n"
                "fetch('{{Trivo CAPI - URL}}/evento/{{Trivo CAPI - Client ID}}',"
                "{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p),keepalive:true})"
                ".catch(function(){});\n})();\n</script>"
            )
            await ct({"name": "Meta CAPI - PageView", "type": "html",
                      "parameter": [{"type": "template", "key": "html", "value": capi_pv_html},
                                    {"type": "boolean", "key": "supportDocumentWrite", "value": "false"}],
                      "firingTriggerId": [str(pv_id)]})

            lead_px_html = (
                "<script>\n"
                "if(typeof fbq!=='undefined'){fbq('track','Lead',{},{eventID:'{{Event ID}}'});}\n"
                "</script>"
            )
            await ct({"name": "Meta Pixel - Lead (Botão WhatsApp)", "type": "html",
                      "parameter": [{"type": "template", "key": "html", "value": lead_px_html},
                                    {"type": "boolean", "key": "supportDocumentWrite", "value": "false"}],
                      "firingTriggerId": [str(wa_id), str(sol_id)]})

            lead_capi_html = (
                "<script>\n(function(){\n"
                "var p={event_name:'Lead',event_id:'{{Event ID}}',source_url:window.location.href,"
                "user_agent:navigator.userAgent,fbc:'{{Cookie - _fbc}}',fbp:'{{Cookie - _fbp}}'};\n"
                "fetch('{{Trivo CAPI - URL}}/evento/{{Trivo CAPI - Client ID}}',"
                "{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p),keepalive:true})"
                ".catch(function(){});\n})();\n</script>"
            )
            await ct({"name": "Meta CAPI - Lead (Botão WhatsApp)", "type": "html",
                      "parameter": [{"type": "template", "key": "html", "value": lead_capi_html},
                                    {"type": "boolean", "key": "supportDocumentWrite", "value": "false"}],
                      "firingTriggerId": [str(wa_id), str(sol_id)]})

        # Tags — Google
        if google_ads_tag:
            await ct({"name": "Google Tag", "type": "googtag",
                      "parameter": [{"type": "template", "key": "tagId", "value": "{{Google Ads - Tag ID}}"}],
                      "firingTriggerId": [str(pv_id)]})
            await ct({"name": "Vinculador de Conversões", "type": "gclidw",
                      "parameter": [{"type": "boolean", "key": "enableCrossDomainLinking", "value": "false"},
                                    {"type": "boolean", "key": "enableUrlPassthrough", "value": "false"}],
                      "firingTriggerId": [str(pv_id)]})
            await ct({"name": "Google Ads - Conversão Botão WhatsApp", "type": "awct",
                      "parameter": [{"type": "template", "key": "conversionId", "value": "{{Google Ads - Tag ID}}"},
                                    {"type": "template", "key": "conversionLabel", "value": "{{Google Ads - Label Botão}}"}],
                      "firingTriggerId": [str(wa_id), str(sol_id)],
                      "tagFiringOption": "ONCE_PER_EVENT"})

        # Tags — GA4
        if measurement_id:
            await ct({"name": "GA4 - PageView", "type": "gaawc",
                      "parameter": [{"type": "template", "key": "measurementId", "value": "{{GA4 - Measurement ID}}"},
                                    {"type": "boolean", "key": "sendPageView", "value": "true"}],
                      "firingTriggerId": [str(pv_id)]})
            await ct({"name": "GA4 - Lead (Botão WhatsApp)", "type": "gaawc",
                      "parameter": [{"type": "template", "key": "measurementId", "value": "{{GA4 - Measurement ID}}"},
                                    {"type": "boolean", "key": "sendPageView", "value": "false"},
                                    {"type": "template", "key": "eventName", "value": "generate_lead"}],
                      "firingTriggerId": [str(wa_id), str(sol_id)]})

    logger.info(f"[onboarding] entities criadas no workspace {ws_id}")
    return ws_id


async def _atualizar_render_clients_json(novo_cliente: dict) -> dict:
    """
    Atualiza CLIENTS_JSON no Render via API, adicionando novo_cliente.
    O Render faz redeploy automático ao detectar mudança na env var.
    Requer RENDER_API_KEY e RENDER_SERVICE_ID configurados.
    """
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return {"status": "skip", "motivo": "RENDER_API_KEY ou RENDER_SERVICE_ID não configurados"}

    h = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json",
         "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=20) as http:
        # 1. Busca todas as env vars atuais
        r = await http.get(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers=h,
        )
        if r.status_code != 200:
            return {"status": "erro", "detalhe": f"GET env-vars: {r.status_code}"}

        items = r.json()  # [{"cursor": "...", "envVar": {"key": "...", "value": "..."}}, ...]
        env_map = {i["envVar"]["key"]: i["envVar"]["value"] for i in items if "envVar" in i}

        # 2. Atualiza CLIENTS_JSON
        try:
            clients = json.loads(env_map.get("CLIENTS_JSON", "[]"))
        except Exception:
            clients = []
        # Remove versão anterior se já existir (re-onboarding)
        clients = [c for c in clients if c.get("id") != novo_cliente.get("id")]
        clients.append(novo_cliente)
        env_map["CLIENTS_JSON"] = json.dumps(clients, ensure_ascii=False)

        # 3. Envia de volta
        payload = [{"key": k, "value": v} for k, v in env_map.items()]
        r2 = await http.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers=h,
            json=payload,
        )
        if r2.status_code not in (200, 201):
            return {"status": "erro", "detalhe": f"PUT env-vars: {r2.status_code} {r2.text[:120]}"}

    logger.info(f"[onboarding] Render CLIENTS_JSON atualizado — {len(clients)} clientes")
    return {"status": "ok", "clientes_total": len(clients)}


@app.post("/onboarding")
async def onboarding(request: Request):
    """
    Registra um cliente: salva no CLIENTS_JSON e retorna os próximos passos.

    Payload:
      nome, url
      gtm_id?       — Container ID existente (ex: GTM-XXXXXXX)
      ga4_id?       — Measurement ID existente (ex: G-XXXXXXXXX)
      pixel_id?     — Meta Pixel ID
      meta_token?   — Meta CAPI Access Token
      google_ads_tag? — AW-XXXXXXXXXX
    """
    if ONBOARDING_KEY:
        if request.headers.get("X-API-Key", "") != ONBOARDING_KEY:
            raise HTTPException(status_code=401, detail="API key inválida")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    nome = body.get("nome", "").strip()
    url  = body.get("url",  "").strip()
    if not nome or not url:
        raise HTTPException(status_code=400, detail="nome e url são obrigatórios")

    gtm_id        = body.get("gtm_id", "").strip()
    ga4_id        = body.get("ga4_id", "").strip()
    pixel_id      = body.get("pixel_id", "").strip()
    meta_token    = body.get("meta_token", "").strip()
    google_ads_tag = body.get("google_ads_tag", "").strip()
    client_id     = _slug(nome)

    logger.info(f"[onboarding] registrando '{nome}' ({client_id})")

    entry: dict = {"id": client_id}
    if gtm_id:
        entry["gtm_id"] = gtm_id
    if ga4_id:
        entry["ga4_id"] = ga4_id
    if pixel_id and meta_token:
        entry["pixel_id"]    = pixel_id
        entry["meta_token"]  = meta_token
    if google_ads_tag:
        entry["google_ads_id"] = google_ads_tag.replace("AW-", "")

    render_result = await _atualizar_render_clients_json(entry)
    render_ok = render_result["status"] == "ok"
    logger.info(f"[onboarding] Render: {render_result}")

    return {
        "success": True,
        "client_id": client_id,
        "gtm": {"public_id": gtm_id} if gtm_id else {},
        "ga4": {"measurement_id": ga4_id} if ga4_id else {},
        "clients_json_entry": entry,
        "render_atualizado": render_ok,
        "erros": [] if render_ok else [render_result.get("detalhe", "Erro Render")],
    }


# ─────────────────────────────────────────────
# Rota principal: recebe evento do GTM
# ─────────────────────────────────────────────

@app.post("/evento/{client_id}")
async def receber_evento(client_id: str, request: Request):
    """
    Recebe um evento disparado pelo GTM.
    Encaminha para Meta CAPI e/ou Google Ads conforme configuração do cliente.

    Payload esperado (JSON):
    {
        "event_name": "Lead",           // PageView, Lead, Purchase, etc.
        "event_id":   "uid-unico-123",  // deduplicação com o Pixel (Meta)
        "gclid":      "...",            // cookie _gcl_aw (Google Ads)
        "email":      "...",            // opcional — Advanced Matching / Enhanced Conv.
        "phone":      "11999998888",    // opcional
        "value":      0,                // opcional, para Purchase
        "currency":   "BRL",
        "source_url": "https://...",
        "ip":         "1.2.3.4",
        "user_agent": "Mozilla/...",
        "fbc":        "_fbc_...",       // cookie Meta
        "fbp":        "_fbp_..."        // cookie Meta
    }
    """
    clients = load_clients()

    if client_id not in clients:
        raise HTTPException(status_code=404, detail=f"Cliente '{client_id}' não encontrado")

    client = clients[client_id]

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    event_name = body.get("event_name", "Lead")
    event_id = body.get("event_id", str(time.time()))
    resultado = {"client": client_id, "event": event_name}

    # ── Meta CAPI ─────────────────────────────
    pixel_id = client.get("pixel_id")
    meta_token = client.get("meta_token")

    if pixel_id and meta_token:
        source_url = body.get("source_url", "")
        ip = body.get("ip", "")
        user_agent = body.get("user_agent", "")
        fbc = body.get("fbc", "")
        fbp = body.get("fbp", "")
        email = body.get("email", "")
        phone = body.get("phone", "")
        value = body.get("value", 0)
        currency = body.get("currency", "BRL")

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

        meta_payload = {"data": [evento]}
        test_code = client.get("test_code", "")
        if test_code:
            meta_payload["test_event_code"] = test_code

        meta_url = f"https://graph.facebook.com/v20.0/{pixel_id}/events?access_token={meta_token}"

        async with httpx.AsyncClient(timeout=10) as http:
            meta_resp = await http.post(meta_url, json=meta_payload)

        if meta_resp.status_code == 200:
            logger.info(f"[{client_id}] META CAPI OK — {event_name}")
            resultado["meta"] = "ok"
        else:
            logger.error(f"[{client_id}] META CAPI ERRO — {meta_resp.status_code}: {meta_resp.text[:200]}")
            resultado["meta"] = f"erro {meta_resp.status_code}"
    else:
        resultado["meta"] = "skip"

    # ── Google Ads ────────────────────────────
    # Só sobe conversão para eventos que representam ação (não PageView)
    if event_name != "PageView" and client.get("google_ads_id") and client.get("google_conv_id"):
        gads_result = await enviar_google_ads(client, body)
        if gads_result["status"] == "ok":
            logger.info(f"[{client_id}] GOOGLE ADS OK — {event_name}")
        elif gads_result["status"] == "erro":
            logger.error(f"[{client_id}] GOOGLE ADS ERRO — {gads_result.get('detalhe','')}")
        resultado["google_ads"] = gads_result["status"]
    else:
        resultado["google_ads"] = "skip"

    return resultado


# ─────────────────────────────────────────────
# Autenticação multi-usuário do painel
# ─────────────────────────────────────────────

import hashlib as _hashlib


_users_cache: list | None = None

def _load_users() -> list:
    global _users_cache
    if _users_cache is None:
        raw = os.environ.get("ONBOARDING_USERS", "[]")
        try:
            _users_cache = json.loads(raw)
        except Exception:
            _users_cache = []
    return _users_cache


def _hash_pass(password: str) -> str:
    return _hashlib.sha256(password.encode()).hexdigest()


def _make_token(username: str) -> str:
    secret = INVITE_CODE or RENDER_API_KEY or "trivo-secret"
    return _hashlib.sha256(f"trivo:{username}:{secret}".encode()).hexdigest()


async def _atualizar_render_users(novo_usuario: dict) -> dict:
    global _users_cache
    # Atualiza o cache em memória imediatamente (login funciona sem aguardar redeploy)
    current = list(_load_users())
    current = [u for u in current if u.get("username") != novo_usuario.get("username")]
    current.append(novo_usuario)
    _users_cache = current

    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return {"status": "skip", "motivo": "RENDER_API_KEY ou RENDER_SERVICE_ID não configurados"}
    h = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars", headers=h)
        if r.status_code != 200:
            return {"status": "ok_local", "detalhe": f"Cache atualizado; GET Render: {r.status_code}"}
        items = r.json()
        env_map = {i["envVar"]["key"]: i["envVar"]["value"] for i in items if "envVar" in i}
        env_map["ONBOARDING_USERS"] = json.dumps(current, ensure_ascii=False)
        payload = [{"key": k, "value": v} for k, v in env_map.items()]
        r2 = await http.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers=h,
            json=payload,
        )
        if r2.status_code not in (200, 201):
            return {"status": "ok_local", "detalhe": f"Cache atualizado; PUT Render: {r2.status_code}"}
    return {"status": "ok", "usuarios_total": len(current)}


@app.post("/register")
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    username  = body.get("username", "").strip().lower()
    password  = body.get("password", "").strip()
    invite    = body.get("invite_code", "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Usuário e senha são obrigatórios")
    if not INVITE_CODE:
        raise HTTPException(status_code=503, detail="Cadastro desabilitado: INVITE_CODE não configurado")
    if invite != INVITE_CODE:
        raise HTTPException(status_code=403, detail="Código de convite inválido")

    users = _load_users()
    if any(u.get("username") == username for u in users):
        raise HTTPException(status_code=409, detail="Usuário já existe")

    novo = {"username": username, "password_hash": _hash_pass(password)}
    result = await _atualizar_render_users(novo)
    if result.get("status") == "erro":
        raise HTTPException(status_code=500, detail=result.get("detalhe", "Erro ao salvar"))

    return {"ok": True, "username": username}


@app.post("/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    username = body.get("username", "").strip().lower()
    password = body.get("password", "").strip()

    # Tenta multi-usuário primeiro
    users = _load_users()
    if users:
        match = next((u for u in users if u.get("username") == username), None)
        if not match or match.get("password_hash") != _hash_pass(password):
            raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
        return {"token": _make_token(username), "username": username}

    # Fallback: variáveis legadas ONBOARDING_USER / ONBOARDING_PASS
    if not ONBOARDING_USER or not ONBOARDING_PASS:
        raise HTTPException(status_code=503, detail="Autenticação não configurada no servidor")
    if username != ONBOARDING_USER or password != ONBOARDING_PASS:
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")
    return {"token": _make_token(ONBOARDING_USER), "username": ONBOARDING_USER}


@app.get("/auth/verify")
async def auth_verify(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()

    # Verifica contra todos os usuários registrados
    users = _load_users()
    if users:
        for u in users:
            if _make_token(u["username"]) == token:
                return {"valid": True, "username": u["username"]}
        # Fallback legado dentro do mesmo bloco de users
        raise HTTPException(status_code=401, detail="Token inválido")

    # Fallback legado
    if not ONBOARDING_USER or not ONBOARDING_PASS:
        raise HTTPException(status_code=503, detail="Autenticação não configurada")
    if _make_token(ONBOARDING_USER) == token:
        return {"valid": True, "username": ONBOARDING_USER}
    raise HTTPException(status_code=401, detail="Token inválido")


# ─────────────────────────────────────────────
# Health check e diagnóstico
# ─────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "online", "servico": "Trivo CAPI", "versao": "2.0.0"}


@app.get("/painel")
def painel():
    return FileResponse("static/painel.html", media_type="text/html")


@app.get("/clientes")
def listar_clientes():
    clients = load_clients()
    return {
        "total": len(clients),
        "clientes": [
            {
                "id": cid,
                "meta": bool(c.get("pixel_id") and c.get("meta_token")),
                "google_ads": bool(c.get("google_ads_id")),
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
    tem_meta = bool(c.get("pixel_id") and c.get("meta_token"))
    tem_google = bool(c.get("google_ads_id"))
    tem_google_upload = bool(c.get("google_ads_id") and c.get("google_conv_id"))
    return {
        "status": "ok",
        "client_id": client_id,
        "providers": {
            "meta_capi": tem_meta,
            "google_ads_gtm": tem_google,
            "google_ads_server": tem_google_upload,
        },
        "pixel_id": c.get("pixel_id", ""),
        "google_ads_id": c.get("google_ads_id", ""),
    }


# ─────────────────────────────────────────────
# CAMADA CRM — desativada por padrão
# Descomentar quando o cliente tiver Kommo ou Bolten
# ─────────────────────────────────────────────

# @app.post("/webhook/{client_id}/kommo")
# async def webhook_kommo(client_id: str, request: Request, status: str = "novo"):
#     """
#     Recebe webhook do Kommo quando lead muda de etapa.
#     Busca telefone na API do Kommo e envia para Meta CAPI + Google Ads.
#
#     Variáveis necessárias no cliente:
#       kommo_token, kommo_subdominio
#     Mapeamento: status=novo → Lead, status=fechado → Purchase
#     """
#     # Ativar: ver ponte-marmoraria/main.py para implementação completa
#     pass


# @app.post("/webhook/{client_id}/bolten")
# async def webhook_bolten(client_id: str, request: Request, status: str = "novo"):
#     """
#     Recebe webhook do Bolten quando lead muda de etapa.
#     Bolten envia telefone diretamente no JSON — mais simples que Kommo.
#
#     Variáveis necessárias no cliente:
#       bolten_api_key
#     """
#     # Ativar: ver ponte-marmoraria/main.py para implementação completa
#     pass

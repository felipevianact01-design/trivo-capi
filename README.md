# Trivo CAPI

Servidor multi-tenant de rastreamento da Trivo.

Recebe eventos do GTM e reenvia para Meta CAPI com Advanced Matching.
Um único servidor atende todos os clientes, identificados pelo `client_id`.

## Rotas

| Rota | Descrição |
|---|---|
| `GET /` | Health check |
| `GET /clientes` | Lista todos os clientes configurados |
| `GET /verificar/{client_id}` | Verifica configuração de um cliente |
| `POST /evento/{client_id}` | Recebe evento do GTM e envia ao Meta CAPI |

## Adicionar cliente novo

Na variável `CLIENTS_JSON` do Render, adicione um objeto à lista:

```json
[
  {
    "id": "wmb",
    "pixel_id": "PIXEL_ID_AQUI",
    "meta_token": "TOKEN_AQUI",
    "google_ads_id": "ID_GOOGLE_ADS",
    "test_code": ""
  }
]
```

## Variáveis de ambiente (Render)

| Variável | Valor |
|---|---|
| `CLIENTS_JSON` | JSON com lista de clientes (ver acima) |

## Ativar CRM (Kommo ou Bolten)

Quando um cliente tiver CRM, descomentar as rotas no `main.py`:
- `/webhook/{client_id}/kommo` para Kommo
- `/webhook/{client_id}/bolten` para Bolten

E adicionar no objeto do cliente:
- Kommo: `"kommo_token"` e `"kommo_subdominio"`
- Bolten: `"bolten_api_key"`

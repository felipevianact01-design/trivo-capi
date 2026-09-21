# Template GTM — Trivo Rastreamento

Container GTM com todas as tags padrão para clientes da Trivo.

## O que está incluído

### Tags
- Meta Pixel (PageView + eventos)
- Meta Advanced Matching (captura email do form)
- CAPI Sender (envia eventos para trivo-capi.onrender.com)
- Google Tag (base GA4 + Google Ads)
- Conversion Linker (preserva GCLID)
- Google Ads — Botão WhatsApp/SOLICITAR (conversão de clique)
- Enhanced Conversions Web (hash email com conversão)
- GA4 PageView
- GA4 Botão WhatsApp/SOLICITAR

### Variáveis a preencher após importar
- `{{Meta - Pixel ID}}` → Pixel ID do cliente
- `{{Meta - Access Token}}` → Token CAPI do cliente
- `{{Google Ads - Tag ID}}` → AW-XXXXXXXXX do cliente
- `{{Google Ads - Label Botão}}` → rótulo da conversão
- `{{GA4 - Measurement ID}}` → G-XXXXXXXXX do cliente
- `{{Trivo CAPI - Client ID}}` → ID do cliente no servidor (ex: "wmb")

## Como usar

1. GTM → Admin → Importar container
2. Selecionar arquivo `container.json`
3. Escolher "Novo espaço de trabalho"
4. Preencher as 6 variáveis acima
5. Publicar

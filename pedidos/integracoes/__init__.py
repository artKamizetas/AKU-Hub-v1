"""
pedidos/integracoes/ — Integrações com os ERPs externos.

    bling.py        — pedido de COMPRA na conta da AK Uniformes (API v3 do Bling)
    olist.py        — pedido de VENDA na conta da Art Kamizetas (API v3 do Olist/Tiny)
    oauth.py        — fluxo OAuth2 genérico das duas plataformas (httpx injetável)
    repositorio.py  — credenciais/tokens/eventos no schema `app` (app.integracao*)

Regras:
- Payload builders são funções PURAS, separadas do HTTP (testáveis sem rede).
- Tokens e client_secret vivem no Supabase (app.integracao), nunca em disco.
- Toda chamada externa gera um registro em app.integracao_evento (sem tokens).
"""

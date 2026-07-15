"""
olist.py — Cliente da API v3 do Olist ERP/Tiny (conta Art Kamizetas):
pedido de VENDA.

O MESMO pedido nosso que virou compra no Bling entra aqui como venda da
fábrica: cliente = AK Uniformes, `numeroOrdemCompra` = nº do pedido de
compra do Bling (amarração entre os dois sistemas).

Camadas: montar_payload_venda() é PURA; o HTTP é fino com `http` injetável.
Itens referenciam produto pelo ID INTERNO do Olist (a API não aceita SKU) —
mapear_produtos_por_sku() constrói o de-para lendo o catálogo completo
(1 varredura paginada, não 1 GET por SKU — respeita rate limit).
"""

import pandas as pd


BASE = "https://api.tiny.com.br/public-api/v3"
_PAGINA = 100   # limit máximo aceito pelo GET /produtos


class OlistFalhou(Exception):
    """Resposta não-2xx da API do Olist (mensagem legível p/ a UI)."""


def _http_default():
    import httpx
    return httpx.Client(timeout=30)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _erro_legivel(resp) -> str:
    try:
        corpo = resp.json()
        msg = (corpo.get("mensagem") or corpo.get("message")
               or corpo.get("detail") or str(corpo)[:300])
    except Exception:
        msg = str(resp.text)[:300]
    return f"Olist retornou {resp.status_code}: {msg}"


def testar_conexao(token: str, http=None) -> tuple:
    """GET leve p/ validar token+permissões. Retorna (ok, mensagem)."""
    http = http or _http_default()
    resp = http.get(f"{BASE}/produtos", headers=_headers(token),
                    params={"limit": 1})
    if resp.status_code < 300:
        return True, "Conexão com o Olist OK."
    return False, _erro_legivel(resp)


def listar_produtos(token: str, http=None) -> list:
    """
    Catálogo completo do Olist (paginado). Retorna a lista crua de produtos
    ([{id, sku/codigo, descricao, ...}]). Uma varredura só — o mapeamento
    filtra localmente.
    """
    http = http or _http_default()
    produtos = []
    offset = 0
    while True:
        resp = http.get(f"{BASE}/produtos", headers=_headers(token),
                        params={"limit": _PAGINA, "offset": offset})
        if resp.status_code >= 300:
            raise OlistFalhou(_erro_legivel(resp))
        lote = (resp.json() or {}).get("itens") or []
        produtos.extend(lote)
        if len(lote) < _PAGINA:
            break
        offset += _PAGINA
    return produtos


def mapear_produtos_por_sku(token: str, skus: list, http=None) -> tuple:
    """
    De-para SKU → id interno do Olist (a API de pedidos exige o id).
    Retorna ({sku: id_olist}, [skus_sem_match]). SKUs são idênticos nos dois
    sistemas (confirmado pelo negócio) — match exato por código.
    """
    catalogo = listar_produtos(token, http)
    id_por_codigo = {}
    for p in catalogo:
        codigo = str(p.get("sku") or p.get("codigo") or "").strip()
        if codigo:
            id_por_codigo[codigo] = int(p["id"])

    mapa = {}
    faltantes = []
    for sku in skus:
        sku = str(sku).strip()
        if sku in id_por_codigo:
            mapa[sku] = id_por_codigo[sku]
        else:
            faltantes.append(sku)
    return mapa, faltantes


def montar_payload_venda(pedido: dict, itens: pd.DataFrame, rodada: dict,
                         cfg: dict, mapa_sku: dict, bling_numero: str,
                         obs_internas: str) -> dict:
    """
    Payload do POST /pedidos (PURO). Itens só com quantidade_final>0.

    cfg = app.integracao['olist'].config — exige contato_id (AK Uniformes),
    vendedor_id e deposito_id (obrigatórios na API). situacao default 0=Aberta.
    """
    faltando_cfg = [c for c in ("contato_id", "vendedor_id", "deposito_id")
                    if not str(cfg.get(c) or "").strip()]
    if faltando_cfg:
        raise ValueError("Config do Olist incompleta (faltam: "
                         f"{', '.join(faltando_cfg)}) — preencha na aba Integrações.")
    if not str(bling_numero or "").strip():
        raise ValueError("Pedido sem número do Bling — emita a compra primeiro "
                         "(o numeroOrdemCompra do Olist referencia o Bling).")

    validos = itens[itens["quantidade_final"] > 0]
    if len(validos) == 0:
        raise ValueError("Pedido sem itens com quantidade final > 0 — nada a emitir.")

    itens_payload = []
    for _, linha in validos.iterrows():
        sku = str(linha["sku"]).strip()
        if sku not in mapa_sku:
            raise ValueError(f"SKU {sku} não encontrado no catálogo do Olist — "
                             "rode a pré-validação na tela do pedido.")
        itens_payload.append({
            "produto": {"id": int(mapa_sku[sku])},
            "quantidade": int(linha["quantidade_final"]),
            "valorUnitario": float(linha["custo_unit"]),
            "infoAdicional": sku,
        })

    return {
        "idContato": int(cfg["contato_id"]),
        "situacao": int(cfg.get("situacao", 0)),          # 0 = Aberta
        "numeroOrdemCompra": str(bling_numero),
        "observacoes": str(pedido["titulo"]),
        "observacoesInternas": str(obs_internas),
        "vendedor": {"id": int(cfg["vendedor_id"])},
        "deposito": {"id": int(cfg["deposito_id"])},
        "itens": itens_payload,
    }


def criar_pedido_venda(token: str, payload: dict, http=None) -> dict:
    """POST /pedidos → {"olist_id", "olist_numero"}."""
    http = http or _http_default()
    resp = http.post(f"{BASE}/pedidos", headers=_headers(token), json=payload)
    if resp.status_code >= 300:
        raise OlistFalhou(_erro_legivel(resp))
    corpo = resp.json() or {}
    return {"olist_id": str(corpo.get("id", "")),
            "olist_numero": str(corpo.get("numeroPedido", ""))}

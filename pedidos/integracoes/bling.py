"""
bling.py — Cliente da API v3 do Bling (conta AK Uniformes): pedido de COMPRA.

Duas camadas bem separadas:
  - montar_payload_compra(): função PURA (dict) — testável sem rede;
  - criar_pedido_compra()/testar_conexao()/obter_pedido_compra_exemplo():
    HTTP fino com `http` injetável (default httpx).

⚠️ Os nomes de campos do POST /pedidos/compras seguem o padrão da API v3
(fornecedor/itens/observacoes/observacoesInternas), mas a referência
pública é uma SPA — o botão "Validar contrato" da aba Integrações usa
obter_pedido_compra_exemplo() (GET num pedido real) para conferir o shape
ANTES da primeira emissão. Divergiu? O ajuste é só nesta função pura.
"""

import pandas as pd


BASE = "https://api.bling.com.br/Api/v3"


class BlingFalhou(Exception):
    """Resposta não-2xx da API do Bling (mensagem legível p/ a UI)."""


def _http_default():
    import httpx
    return httpx.Client(timeout=30)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _erro_legivel(resp) -> str:
    try:
        corpo = resp.json()
        erro = corpo.get("error", {})
        msg = erro.get("description") or erro.get("message") or str(corpo)[:300]
    except Exception:
        msg = str(resp.text)[:300]
    return f"Bling retornou {resp.status_code}: {msg}"


def testar_conexao(token: str, http=None) -> tuple:
    """GET leve p/ validar token+permissões. Retorna (ok, mensagem)."""
    http = http or _http_default()
    resp = http.get(f"{BASE}/situacoes/modulos", headers=_headers(token))
    if resp.status_code < 300:
        return True, "Conexão com o Bling OK."
    return False, _erro_legivel(resp)


def obter_pedido_compra_exemplo(token: str, http=None) -> dict:
    """
    GET /pedidos/compras (1 registro) — validação de CONTRATO sem escrita:
    o JSON de um pedido real mostra os nomes de campos que o POST espera.
    Retorna {} se a conta ainda não tem pedidos de compra.
    """
    http = http or _http_default()
    resp = http.get(f"{BASE}/pedidos/compras", headers=_headers(token),
                    params={"limite": 1})
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    dados = (resp.json() or {}).get("data") or []
    return dados[0] if dados else {}


def montar_payload_compra(pedido: dict, itens: pd.DataFrame, rodada: dict,
                          cfg: dict, obs_internas: str) -> dict:
    """
    Payload do POST /pedidos/compras (PURO). Itens só com quantidade_final>0
    (zeros são decisão de não comprar — ficam como auditoria no nosso banco).

    cfg = app.integracao['bling'].config — exige fornecedor_id (Art Kamizetas).
    """
    fornecedor_id = str(cfg.get("fornecedor_id") or "").strip()
    if not fornecedor_id:
        raise ValueError("Config do Bling sem fornecedor_id (Art Kamizetas) — "
                         "preencha na aba Integrações.")

    validos = itens[itens["quantidade_final"] > 0]
    if len(validos) == 0:
        raise ValueError("Pedido sem itens com quantidade final > 0 — nada a emitir.")

    itens_payload = []
    for _, linha in validos.iterrows():
        id_produto = str(linha["id_produto_bling"]).strip()
        if not id_produto:
            raise ValueError(f"Item {linha['sku']} sem id de produto do Bling "
                             "(produto pode ter sido excluído do cadastro).")
        itens_payload.append({
            "produto": {"id": int(id_produto)},
            "quantidade": int(linha["quantidade_final"]),
            "valor": float(linha["custo_unit"]),
            "descricao": str(linha.get("produto", "") or ""),
        })

    return {
        "fornecedor": {"id": int(fornecedor_id)},
        "dataPrevista": str(pd.Timestamp(str(rodada["data_chegada"])).date()),
        "observacoes": str(pedido["titulo"]),
        "observacoesInternas": str(obs_internas),
        "itens": itens_payload,
    }


def criar_pedido_compra(token: str, payload: dict, http=None) -> dict:
    """POST /pedidos/compras → {"bling_id", "bling_numero"}."""
    http = http or _http_default()
    resp = http.post(f"{BASE}/pedidos/compras", headers=_headers(token), json=payload)
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    dados = (resp.json() or {}).get("data") or {}
    return {"bling_id": str(dados.get("id", "")),
            "bling_numero": str(dados.get("numero", ""))}

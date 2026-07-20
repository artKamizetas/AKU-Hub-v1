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

from pedidos import builder


BASE = "https://api.bling.com.br/Api/v3"

# Defaults dos campos de compra que o Bling exige mas o espelho do Supabase não
# tem (unidade não vem no cadastro espelhado; prazo é acordo comercial).
# Sobrescritos por app.integracao['bling'].config na aba Integrações.
UNIDADE_PADRAO = "PÇ"
PRAZO_PAGAMENTO_PADRAO = 30


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
                          cfg: dict, obs_completa: str, data_emissao=None) -> dict:
    """
    Payload do POST /pedidos/compras (PURO). Itens só com quantidade_final>0
    (zeros são decisão de não comprar — ficam como auditoria no nosso banco).

    Campos de texto (ordem verificada na gestão de compras do Bling):
      - observacoesInternas = título curto → é a coluna "Observação interna"
        da listagem e alimenta a busca por pedido;
      - observacoes         = bloco completo (obs_completa) com o resumo da rodada;
      - descricaoDetalhada  = memória de cálculo do item (builder), 1 linha.

    Por item, `codigoFornecedor` = nosso SKU (é a coluna "Código" da tela de
    compras; SKUs são idênticos nos dois sistemas) e `unidade` vem do config —
    o espelho do Supabase não traz unidade de medida do cadastro.

    `parcelas`: uma parcela única com vencimento em `prazo_pagamento_dias` a
    partir da emissão. Só entra no payload quando forma_pagamento_id está
    configurado (validar_pre_emissao_bling avisa antes do clique).

    cfg = app.integracao['bling'].config — exige fornecedor_id (Art Kamizetas).
    `data_emissao` (default hoje) fica injetável para os testes serem determinísticos.
    """
    fornecedor_id = str(cfg.get("fornecedor_id") or "").strip()
    if not fornecedor_id:
        raise ValueError("Config do Bling sem fornecedor_id (Art Kamizetas) — "
                         "preencha na aba Integrações.")

    validos = itens[itens["quantidade_final"] > 0]
    if len(validos) == 0:
        raise ValueError("Pedido sem itens com quantidade final > 0 — nada a emitir.")

    unidade = str(cfg.get("unidade_padrao") or UNIDADE_PADRAO).strip()
    mes = int(rodada.get("mes_disparo") or 0)
    ano = int(rodada.get("ano_disparo") or 0)

    itens_payload = []
    for _, linha in validos.iterrows():
        id_produto = str(linha["id_produto_bling"]).strip()
        if not id_produto:
            raise ValueError(f"Item {linha['sku']} sem id de produto do Bling "
                             "(produto pode ter sido excluído do cadastro).")
        itens_payload.append({
            "produto": {"id": int(id_produto)},
            "codigoFornecedor": str(linha["sku"]),
            "unidade": unidade,
            "quantidade": int(linha["quantidade_final"]),
            "valor": float(linha["custo_unit"]),
            "descricao": str(linha.get("produto", "") or ""),
            "descricaoDetalhada": builder.montar_descricao_item(linha, mes, ano),
        })

    payload = {
        "fornecedor": {"id": int(fornecedor_id)},
        "dataPrevista": str(pd.Timestamp(str(rodada["data_chegada"])).date()),
        "observacoes": str(obs_completa),
        "observacoesInternas": str(pedido["titulo"]),
        "itens": itens_payload,
    }

    # Pagamento: parcela única, vencimento = emissão + prazo. A data de emissão
    # vai explícita no payload para o vencimento não divergir do que o Bling
    # assumiria por conta própria.
    forma_id = str(cfg.get("forma_pagamento_id") or "").strip()
    if forma_id:
        emissao = (pd.Timestamp.now().normalize() if data_emissao is None
                   else pd.Timestamp(str(data_emissao)).normalize())
        prazo = int(cfg.get("prazo_pagamento_dias") or PRAZO_PAGAMENTO_PADRAO)
        total = float((validos["quantidade_final"] * validos["custo_unit"]).sum())
        payload["data"] = str(emissao.date())
        payload["parcelas"] = [{
            "valor": round(total, 2),
            "dataVencimento": str((emissao + pd.Timedelta(days=prazo)).date()),
            "formaPagamento": {"id": int(forma_id)},
        }]

    return payload


def listar_formas_pagamento(token: str, http=None) -> list:
    """
    GET /formas-pagamentos → [{"id", "descricao"}] p/ o selectbox da aba
    Integrações (o id é por conta — não dá para hardcodar).
    """
    http = http or _http_default()
    resp = http.get(f"{BASE}/formas-pagamentos", headers=_headers(token))
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    dados = (resp.json() or {}).get("data") or []
    return [{"id": str(f.get("id", "")), "descricao": str(f.get("descricao", ""))}
            for f in dados]


def criar_pedido_compra(token: str, payload: dict, http=None) -> dict:
    """POST /pedidos/compras → {"bling_id", "bling_numero"}."""
    http = http or _http_default()
    resp = http.post(f"{BASE}/pedidos/compras", headers=_headers(token), json=payload)
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    dados = (resp.json() or {}).get("data") or {}
    return {"bling_id": str(dados.get("id", "")),
            "bling_numero": str(dados.get("numero", ""))}

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

# `situacao.valor` do pedido de compra (enum fixo da API; o `situacao.id` é
# outra coisa — o id da situação no módulo, por conta). "Em aberto" é o filtro
# que libera alterar/cancelar um pedido já emitido. SEMPRE decidir pelo
# `valor`: nos pedidos de compra o `id` vem zerado na maioria das respostas.
SITUACAO_EM_ABERTO = 0
SITUACAO_ATENDIDO = 1
SITUACAO_CANCELADO = 2
SITUACAO_EM_ANDAMENTO = 3
ROTULOS_SITUACAO = {
    SITUACAO_EM_ABERTO: "Em aberto", SITUACAO_ATENDIDO: "Atendido",
    SITUACAO_CANCELADO: "Cancelado", SITUACAO_EM_ANDAMENTO: "Em andamento",
}

# Campos do pedido que o AKU-Hub NÃO gere. O PUT substitui o pedido inteiro:
# sem devolvê-los, um frete ou desconto lançado à mão no Bling sumiria na
# primeira alteração.
_CAMPOS_NAO_GERIDOS = ("ordemCompra", "desconto", "categoria", "transporte")


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


# ---------------------------------------------------------------------------
# Pós-emissão: ler, alterar e cancelar um pedido de compra que já existe
# ---------------------------------------------------------------------------
def _inteiro(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def normalizar_pedido_compra(dados: dict) -> dict:
    """
    GET /pedidos/compras/{id} → o que o pós-emissão usa (PURO). `bruto` guarda
    a resposta inteira: o PUT precisa devolver os campos que não gerimos.
    Itens como [{"id_produto", "sku", "quantidade", "valor"}] — o SKU é o
    `codigoFornecedor` (nós o gravamos na emissão); linha incluída à mão no
    Bling não o tem e cai no `produto.codigo`.
    """
    dados = dados or {}
    situacao = dados.get("situacao") or {}
    valor = _inteiro(situacao.get("valor"))
    itens = []
    for item in dados.get("itens") or []:
        produto = item.get("produto") or {}
        itens.append({
            "id_produto": str(produto.get("id") or "").strip(),
            "sku": str(item.get("codigoFornecedor") or produto.get("codigo") or "").strip(),
            "quantidade": float(item.get("quantidade") or 0),
            "valor": float(item.get("valor") or 0),
        })
    return {
        "id": str(dados.get("id") or ""),
        "numero": dados.get("numero"),
        "data": str(dados.get("data") or "")[:10],
        "situacao_valor": valor,
        "situacao_id": _inteiro(situacao.get("id")),
        "situacao_rotulo": ROTULOS_SITUACAO.get(valor, f"situação {valor}"),
        "parcelas": list(dados.get("parcelas") or []),
        "itens": itens,
        "bruto": dados,
    }


def obter_pedido_compra(token: str, id_pedido, http=None) -> dict:
    """GET /pedidos/compras/{id}, normalizado. 404 = pedido excluído no Bling."""
    http = http or _http_default()
    resp = http.get(f"{BASE}/pedidos/compras/{id_pedido}", headers=_headers(token))
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    return normalizar_pedido_compra((resp.json() or {}).get("data") or {})


def _com_valor(campo: str, bloco) -> bool:
    """
    True se o bloco opcional tem algo que valha devolver no PUT. Desconto só
    conta pelo VALOR: o Bling o devolve como {"valor": 0, "unidade": "REAL"}
    quando não há desconto, e a unidade sozinha não é informação.
    """
    if campo == "desconto":
        return float((bloco or {}).get("valor") or 0) > 0
    if isinstance(bloco, dict):
        return any(v not in (None, "", 0, 0.0) for v in bloco.values())
    return bloco not in (None, "", 0, 0.0)


def montar_payload_alteracao_compra(payload_novo: dict, atual: dict) -> dict:
    """
    Payload do PUT /pedidos/compras/{id} (PURO): o payload de emissão refeito
    com os itens novos (`payload_novo`, de montar_payload_compra) + o que
    precisa ser PRESERVADO do pedido que está no Bling (`atual`, normalizado).
    O PUT substitui o pedido inteiro, então o que não vier aqui se perde:

      - `numero` e `data` de emissão — sem o número o Bling renumera o pedido
        ("O número do seu pedido foi modificado") e o `numeroOrdemCompra` do
        Olist passaria a apontar para um pedido que não existe;
      - a situação NÃO vai no payload, de propósito: o GET devolve
        `situacao.id = 0` na maioria dos pedidos de compra (só o `valor` vem
        preenchido — conferido na conta: 403 de 500), e devolver esse zero
        seria mandar uma situação que não existe. Omitir é seguro por
        construção: só se altera pedido Em aberto, que é também o default;
      - as PARCELAS: mesmas datas e formas de pagamento, valores refeitos na
        proporção do total novo. Uma alteração de quantidade não muda o
        acordo de pagamento — recalcular o vencimento a partir de hoje
        empurraria o prazo a cada ajuste;
      - frete, desconto, categoria e ordem de compra lançados no Bling.
    """
    payload = dict(payload_novo)
    bruto = atual.get("bruto") or {}

    if atual.get("numero") not in (None, ""):
        payload["numero"] = atual["numero"]
    if atual.get("data"):
        payload["data"] = atual["data"]

    for campo in _CAMPOS_NAO_GERIDOS:
        if _com_valor(campo, bruto.get(campo)):
            payload[campo] = bruto[campo]

    total = round(sum(float(i["quantidade"]) * float(i["valor"])
                      for i in payload.get("itens") or []), 2)
    originais = [p for p in (atual.get("parcelas") or []) if p.get("dataVencimento")]
    if originais:
        soma = sum(float(p.get("valor") or 0) for p in originais)
        parcelas, acumulado = [], 0.0
        for pos, original in enumerate(originais):
            if pos == len(originais) - 1:
                valor = round(total - acumulado, 2)   # o resto fecha o total
            else:
                peso = (float(original.get("valor") or 0) / soma) if soma else 1 / len(originais)
                valor = round(total * peso, 2)
                acumulado += valor
            parcela = {"valor": valor, "dataVencimento": str(original["dataVencimento"])[:10]}
            forma = (original.get("formaPagamento") or {}).get("id")
            if forma:
                parcela["formaPagamento"] = {"id": int(forma)}
            if original.get("observacao"):
                parcela["observacao"] = original["observacao"]
            parcelas.append(parcela)
        payload["parcelas"] = parcelas
    return payload


def alterar_pedido_compra(token: str, id_pedido, payload: dict, http=None) -> dict:
    """PUT /pedidos/compras/{id} → {"bling_numero", "alertas"} (alertas do Bling)."""
    http = http or _http_default()
    resp = http.put(f"{BASE}/pedidos/compras/{id_pedido}", headers=_headers(token),
                    json=payload)
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    try:
        dados = (resp.json() or {}).get("data") or {}
    except Exception:
        dados = {}
    return {"bling_numero": str(dados.get("numero", "") or ""),
            "alertas": list(dados.get("alertas") or [])}


def listar_situacoes_compra(token: str, http=None) -> list:
    """
    Situações do módulo de pedidos de compra → [{"id", "nome"}] p/ o selectbox
    "Situação de cancelado" da aba Integrações. O PATCH de situação pede o ID
    da situação no módulo (por conta — não dá para hardcodar), não o `valor`.
    """
    http = http or _http_default()
    resp = http.get(f"{BASE}/situacoes/modulos", headers=_headers(token))
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    modulos = (resp.json() or {}).get("data") or []
    alvo = next((m for m in modulos
                 if "compra" in f"{m.get('nome', '')} {m.get('descricao', '')}".lower()), None)
    if not alvo:
        raise BlingFalhou("Bling não devolveu o módulo de pedidos de compra em "
                          "/situacoes/modulos.")
    resp = http.get(f"{BASE}/situacoes/modulos/{alvo['id']}", headers=_headers(token))
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))
    return [{"id": str(s.get("id", "")), "nome": str(s.get("nome", ""))}
            for s in (resp.json() or {}).get("data") or []]


def mudar_situacao_compra(token: str, id_pedido, id_situacao, http=None) -> None:
    """PATCH /pedidos/compras/{id}/situacoes/{idSituacao} (id da situação no módulo)."""
    http = http or _http_default()
    resp = http.patch(
        f"{BASE}/pedidos/compras/{id_pedido}/situacoes/{int(id_situacao)}",
        headers=_headers(token))
    if resp.status_code >= 300:
        raise BlingFalhou(_erro_legivel(resp))


def conferir_cancelamento_compra(token: str, id_pedido, http=None) -> None:
    """
    GET de conferência depois do PATCH: o id da situação vem de configuração
    escolhida na aba Integrações, e um id errado moveria o pedido para OUTRA
    situação sem erro nenhum. Só passa se o pedido ficou de fato Cancelado.
    """
    depois = obter_pedido_compra(token, id_pedido, http)
    if depois["situacao_valor"] != SITUACAO_CANCELADO:
        raise BlingFalhou(
            f"O Bling aceitou a mudança de situação, mas o pedido ficou "
            f"\"{depois['situacao_rotulo']}\" e não \"Cancelado\" — confira a "
            "\"Situação de cancelado\" na aba Integrações e o pedido no Bling.")


def cancelar_pedido_compra(token: str, id_pedido, id_situacao_cancelado, http=None) -> None:
    """Muda a situação para a de cancelado e confere o resultado."""
    mudar_situacao_compra(token, id_pedido, id_situacao_cancelado, http)
    conferir_cancelamento_compra(token, id_pedido, http)

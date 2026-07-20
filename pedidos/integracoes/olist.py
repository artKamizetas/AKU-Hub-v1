"""
olist.py — Cliente da API v3 do Olist ERP/Tiny (conta Art Kamizetas):
pedido de VENDA.

O MESMO pedido nosso que virou compra no Bling entra aqui como venda da
fábrica: cliente = AK Uniformes, `numeroOrdemCompra` = nº do pedido de
compra do Bling (amarração entre os dois sistemas).

Camadas: montar_payload_venda() é PURA; o HTTP é fino com `http` injetável.
Itens referenciam produto pelo ID INTERNO do Olist (a API não aceita SKU) —
mapear_produtos_por_sku() constrói o de-para com uma busca DIRECIONADA por
`?codigo=` por SKU (1 GET por SKU do pedido, ~7-15), não varrendo o catálogo
inteiro: a varredura antiga (dezenas de páginas × cada pedido) estourava o
rate limit do Olist (60 req/min no plano básico → HTTP 429).

Rate limit: TODA chamada passa por _requisitar(), que em 429 respeita o
cabeçalho `Retry-After` e tenta de novo (backoff limitado) — a rede se
autorregula em vez de falhar o pedido no primeiro esbarrão.
"""

import time

import pandas as pd


BASE = "https://api.tiny.com.br/public-api/v3"
_PAGINA = 100   # limit máximo aceito pelo GET /produtos

# Retry de rate limit (429). O Olist devolve Retry-After; sem ele, backoff fixo.
_TENTATIVAS_429 = 5
_ESPERA_429_PADRAO_S = 3

# Só o piso de segurança: o prazo real vem do config do BLING (mesmo acordo,
# dois documentos) e é injetado em montar_payload_venda(prazo_dias=...).
PRAZO_PAGAMENTO_PADRAO = 30


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


def _espera_429_s(resp) -> int:
    """Segundos a aguardar num 429 — o cabeçalho Retry-After manda; senão, padrão."""
    cab = getattr(resp, "headers", None) or {}
    try:
        return max(1, int(float(cab.get("Retry-After", _ESPERA_429_PADRAO_S))))
    except (TypeError, ValueError):
        return _ESPERA_429_PADRAO_S


def _requisitar(http, metodo: str, url: str, token: str, *, params=None,
                json=None, dormir=None):
    """
    Chamada HTTP com retry SÓ em 429 (rate limit): respeita Retry-After e tenta
    de novo até _TENTATIVAS_429. Demais status (2xx/erros) voltam intactos ao
    chamador na 1ª resposta. `dormir` injetável p/ testes não esperarem de fato.
    """
    dormir = dormir or time.sleep
    resp = None
    for tentativa in range(_TENTATIVAS_429):
        chamar = getattr(http, metodo)
        resp = (chamar(url, headers=_headers(token), params=params)
                if metodo == "get"
                else chamar(url, headers=_headers(token), json=json))
        if resp.status_code != 429 or tentativa == _TENTATIVAS_429 - 1:
            return resp
        dormir(_espera_429_s(resp))
    return resp


def testar_conexao(token: str, http=None, dormir=None) -> tuple:
    """GET leve p/ validar token+permissões. Retorna (ok, mensagem)."""
    http = http or _http_default()
    resp = _requisitar(http, "get", f"{BASE}/produtos", token,
                       params={"limit": 1}, dormir=dormir)
    if resp.status_code < 300:
        return True, "Conexão com o Olist OK."
    return False, _erro_legivel(resp)


def _id_por_codigo_exato(itens: list, sku: str):
    """
    Dentre os itens que o filtro ?codigo= devolveu, o id do que casa o código
    EXATAMENTE (o filtro pode ser parcial: 'ADC-CAL-P' traria 'ADC-CAL-PP').
    Aceita tanto `sku` quanto `codigo` no retorno. None se nenhum casa.
    """
    for p in itens:
        codigo = str(p.get("sku") or p.get("codigo") or "").strip()
        if codigo == sku:
            return int(p["id"])
    return None


def mapear_produtos_por_sku(token: str, skus: list, http=None, dormir=None) -> tuple:
    """
    De-para SKU → id interno do Olist (a API de pedidos exige o id).
    Retorna ({sku: id_olist}, [skus_sem_match]). SKUs são idênticos nos dois
    sistemas (confirmado pelo negócio) — match EXATO por código.

    Busca direcionada (`GET /produtos?codigo=<SKU>`), 1 GET por SKU distinto —
    não varre o catálogo inteiro. Dezenas de milhares de produtos varridos por
    pedido eram o que estourava o rate limit (429).
    """
    http = http or _http_default()
    mapa, faltantes, vistos = {}, [], set()
    for sku in skus:
        sku = str(sku).strip()
        if not sku or sku in vistos:
            continue
        vistos.add(sku)
        resp = _requisitar(http, "get", f"{BASE}/produtos", token,
                           params={"codigo": sku, "limit": _PAGINA}, dormir=dormir)
        if resp.status_code >= 300:
            raise OlistFalhou(_erro_legivel(resp))
        itens = (resp.json() or {}).get("itens") or []
        id_olist = _id_por_codigo_exato(itens, sku)
        if id_olist is not None:
            mapa[sku] = id_olist
        else:
            faltantes.append(sku)
    return mapa, faltantes


def montar_payload_venda(pedido: dict, itens: pd.DataFrame, rodada: dict,
                         cfg: dict, mapa_sku: dict, bling_numero: str,
                         obs_completa: str, prazo_dias=None,
                         data_emissao=None) -> dict:
    """
    Payload do POST /pedidos (PURO). Itens só com quantidade_final>0.

    Texto espelha o Bling: observacoesInternas = título curto (busca/listagem),
    observacoes = bloco completo (obs_completa) com o resumo da rodada.
    `dataPrevista` = data de chegada da rodada — o MESMO valor que vai no Bling,
    para os dois ERPs nunca divergirem sobre quando a mercadoria chega.

    `pagamento` tem SHAPE diferente do Bling: objeto aninhado com
    `formaRecebimento` (não `formaPagamento` solto no topo). Só entra quando
    forma_recebimento_id está configurado.

    `prazo_dias` é o prazo do BLING, injetado pelo chamador — o pedido de compra
    e o de venda são o MESMO acordo visto dos dois lados, então o vencimento tem
    de bater. Por isso não existe campo de prazo na config do Olist: um número
    editável em dois lugares divergiria na primeira vez que alguém mudasse um só.

    cfg = app.integracao['olist'].config — exige contato_id (AK Uniformes),
    vendedor_id e deposito_id (obrigatórios na API). situacao default 0=Aberta.
    `data_emissao` (default hoje) fica injetável p/ testes determinísticos.
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
            "produto": {"id": int(mapa_sku[sku]), "tipo": "P"},
            "quantidade": int(linha["quantidade_final"]),
            "valorUnitario": float(linha["custo_unit"]),
            "infoAdicional": sku,
        })

    chegada = pd.Timestamp(str(rodada["data_chegada"])).normalize()
    payload = {
        "idContato": int(cfg["contato_id"]),
        "situacao": int(cfg.get("situacao", 0)),          # 0 = Aberta
        "dataPrevista": str(chegada.date()),
        "numeroOrdemCompra": str(bling_numero),
        "observacoes": str(obs_completa),
        "observacoesInternas": str(pedido["titulo"]),
        "vendedor": {"id": int(cfg["vendedor_id"])},
        "deposito": {"id": int(cfg["deposito_id"])},
        "itens": itens_payload,
    }

    # Recebimento: parcela única com vencimento em `prazo_dias` a partir da
    # emissão — a MESMA regra e o MESMO prazo do pedido de compra do Bling.
    forma_id = str(cfg.get("forma_recebimento_id") or "").strip()
    if forma_id:
        emissao = (pd.Timestamp.now().normalize() if data_emissao is None
                   else pd.Timestamp(str(data_emissao)).normalize())
        prazo = int(prazo_dias if prazo_dias is not None else PRAZO_PAGAMENTO_PADRAO)
        total = float((validos["quantidade_final"] * validos["custo_unit"]).sum())
        payload["data"] = str(emissao.date())
        payload["pagamento"] = {
            "formaRecebimento": {"id": int(forma_id)},
            "parcelas": [{
                "dias": prazo,
                "data": str((emissao + pd.Timedelta(days=prazo)).date()),
                "valor": round(total, 2),
                "formaRecebimento": {"id": int(forma_id)},
            }],
        }
        meio_id = str(cfg.get("meio_pagamento_id") or "").strip()
        if meio_id:
            payload["pagamento"]["meioPagamento"] = {"id": int(meio_id)}
            payload["pagamento"]["parcelas"][0]["meioPagamento"] = {"id": int(meio_id)}

    return payload


def criar_pedido_venda(token: str, payload: dict, http=None, dormir=None) -> dict:
    """POST /pedidos → {"olist_id", "olist_numero"}. Retry só em 429."""
    http = http or _http_default()
    resp = _requisitar(http, "post", f"{BASE}/pedidos", token,
                       json=payload, dormir=dormir)
    if resp.status_code >= 300:
        raise OlistFalhou(_erro_legivel(resp))
    corpo = resp.json() or {}
    return {"olist_id": str(corpo.get("id", "")),
            "olist_numero": str(corpo.get("numeroPedido", ""))}

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


def _detalhes_validacao(corpo: dict) -> str:
    """
    Erros por campo que o Olist v3 devolve num 400 de validação, achatados em
    'campo: mensagem; campo: mensagem'. O corpo é
    `{"mensagem": "Ocorreram erros de validação", "detalhes": [{"campo", "mensagem"}]}`
    (a chave já apareceu como `detalhes`, `erros` e `campos` conforme o endpoint;
    itens ora {campo, mensagem}, ora {field, message}, ora string solta). Sem
    isso a mensagem do topo ("Ocorreram erros de validação") não diz NADA sobre
    QUAL campo reprovou. "" se não há array de detalhes reconhecível.
    """
    itens = None
    for chave in ("detalhes", "erros", "campos", "errors"):
        if isinstance(corpo.get(chave), list) and corpo[chave]:
            itens = corpo[chave]
            break
    if not itens:
        return ""
    partes = []
    for it in itens:
        if isinstance(it, dict):
            campo = it.get("campo") or it.get("field") or it.get("propriedade")
            texto = it.get("mensagem") or it.get("message") or it.get("descricao")
            partes.append(f"{campo}: {texto}" if campo else str(texto))
        else:
            partes.append(str(it))
    return "; ".join(p for p in partes if p and p != "None")


def _erro_legivel(resp) -> str:
    try:
        corpo = resp.json()
        msg = (corpo.get("mensagem") or corpo.get("message")
               or corpo.get("detail") or str(corpo)[:300])
        detalhes = _detalhes_validacao(corpo)
        if detalhes:
            msg = f"{msg} — {detalhes}"
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


def listar_formas_recebimento(token: str, http=None, dormir=None) -> list:
    """
    GET /formas-recebimento → [{"id", "nome", "ativa"}] p/ o selectbox da aba
    Integrações. O id da forma de recebimento é POR CONTA (não dá p/ hardcodar)
    e o Olist não o expõe na UI de forma óbvia — daí o valor ir num selectbox por
    nome em vez de o usuário caçar o número. `situacao == "1"` = ativa.
    """
    http = http or _http_default()
    resp = _requisitar(http, "get", f"{BASE}/formas-recebimento", token,
                       params={"limit": _PAGINA}, dormir=dormir)
    if resp.status_code >= 300:
        raise OlistFalhou(_erro_legivel(resp))
    itens = (resp.json() or {}).get("itens") or []
    return [{"id": str(f.get("id", "")), "nome": str(f.get("nome", "")),
             "ativa": str(f.get("situacao", "")) == "1"} for f in itens]


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


def _buscar_id_por_codigo(token: str, codigo: str, http, dormir) -> int:
    """id do produto cujo código bate EXATAMENTE com `codigo` (ou None)."""
    resp = _requisitar(http, "get", f"{BASE}/produtos", token,
                       params={"codigo": codigo, "limit": _PAGINA}, dormir=dormir)
    if resp.status_code >= 300:
        raise OlistFalhou(_erro_legivel(resp))
    return _id_por_codigo_exato((resp.json() or {}).get("itens") or [], codigo)


def mapear_produtos_por_sku(token: str, skus: list, http=None, dormir=None) -> tuple:
    """
    De-para SKU → id interno do Olist (a API de pedidos exige o id).
    Retorna ({sku: id_olist}, [skus_sem_match]). SKUs são idênticos nos dois
    sistemas (confirmado pelo negócio) — match EXATO por código.

    Busca direcionada (`GET /produtos?codigo=<SKU>`), 1 GET por SKU distinto —
    não varre o catálogo inteiro. Dezenas de milhares de produtos varridos por
    pedido eram o que estourava o rate limit (429). É o PISO (fallback) da
    resolução; mapear_por_familia() cobre a grade de tamanhos em menos chamadas.
    """
    http = http or _http_default()
    mapa, faltantes, vistos = {}, [], set()
    for sku in skus:
        sku = str(sku).strip()
        if not sku or sku in vistos:
            continue
        vistos.add(sku)
        id_olist = _buscar_id_por_codigo(token, sku, http, dormir)
        if id_olist is not None:
            mapa[sku] = id_olist
        else:
            faltantes.append(sku)
    return mapa, faltantes


def _sku_pai(sku: str):
    """
    SKU do produto-pai a partir do SKU-filho, tirando o sufixo de tamanho
    ('SES024MOLDIA-G' → 'SES024MOLDIA'). Heurística deliberadamente ingênua
    (corta no último '-'): se errar, a família não é encontrada e o SKU cai no
    fallback exato — nunca casa errado. None se não há '-' para cortar.
    """
    base = str(sku).rpartition("-")[0].strip()
    return base or None


def obter_variacoes(token: str, id_pai: int, http=None, dormir=None) -> dict:
    """
    {sku: id} de TODAS as variações de um produto-pai (GET /produtos/{id}),
    incluindo o próprio pai. Uma chamada devolve a grade de tamanhos inteira —
    o barato que substitui 1 GET por tamanho.
    """
    http = http or _http_default()
    resp = _requisitar(http, "get", f"{BASE}/produtos/{id_pai}", token, dormir=dormir)
    if resp.status_code >= 300:
        raise OlistFalhou(_erro_legivel(resp))
    corpo = resp.json() or {}
    mapa = {}
    sku_pai = str(corpo.get("sku") or "").strip()
    if sku_pai and corpo.get("id") is not None:
        mapa[sku_pai] = int(corpo["id"])
    for v in (corpo.get("variacoes") or []):
        s = str(v.get("sku") or "").strip()
        if s and v.get("id") is not None:
            mapa[s] = int(v["id"])
    return mapa


def mapear_por_familia(token: str, skus: list, http=None, dormir=None) -> tuple:
    """
    Resolve SKU → id agrupando por produto-PAI: um GET acha o pai
    (`?codigo=<pai>`) e outro traz a grade inteira (`/produtos/{id}`), ~2
    chamadas por família de ~7 tamanhos em vez de 7.

    Retorna ({sku: id_olist}, {skus_pendentes}). O mapa inclui TODOS os
    tamanhos que a grade trouxe (não só os pedidos) — de propósito, para o
    cache aquecer os irmãos. `pendentes` = SKUs sem pai derivável, com pai não
    encontrado, ou ausentes da grade → o chamador tenta o fallback exato.
    """
    http = http or _http_default()
    grupos, pendentes = {}, set()
    for sku in skus:
        sku = str(sku).strip()
        if not sku:
            continue
        pai = _sku_pai(sku)
        if pai is None:
            pendentes.add(sku)
        else:
            grupos.setdefault(pai, set()).add(sku)

    mapa = {}
    for pai, filhos in grupos.items():
        id_pai = _buscar_id_por_codigo(token, pai, http, dormir)
        if id_pai is None:
            pendentes |= filhos            # pai não existe → tenta cada filho exato
            continue
        grade = obter_variacoes(token, id_pai, http, dormir)
        mapa.update(grade)                 # aquece irmãos também
        pendentes |= {f for f in filhos if f not in grade}
    return mapa, pendentes


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

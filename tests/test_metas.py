"""
Testes do motor puro de Metas Escalonadas (etl/metas.py).

Sem streamlit/pandas/Supabase — só dicts. Cobre: classificação de nível nos
limites exatos, fallback do formato legado, mês ausente, rateio vendedor por
peso, herança de atribuição vendedor→loja, agregação de PA por média
ponderada (não pela média simples) e validação de prata<=ouro<=diamante.
"""

from etl import metas


# ---------------------------------------------------------------------
# chave_competencia
# ---------------------------------------------------------------------

def test_chave_competencia_formata_com_zero():
    assert metas.chave_competencia(2026, 1) == "2026-01"
    assert metas.chave_competencia(2026, 12) == "2026-12"


# ---------------------------------------------------------------------
# metas_da_loja
# ---------------------------------------------------------------------

def config_base():
    return {
        "daily": {
            "metas": {"Natal": 70000.0},
            "metas_mensais": {
                "2026-01": {
                    "Natal": {
                        "faturamento": {"prata": 120000, "ouro": 150000, "diamante": 180000},
                        "pa": {"prata": 2.0, "ouro": 2.5, "diamante": 3.0},
                    },
                },
            },
            "vendedores_loja": {
                "2026-01": {
                    "V1": {"loja": "Natal", "peso": 1.0, "ativo": True},
                    "V2": {"loja": "Natal", "peso": 1.0, "ativo": True},
                    "V3": {"loja": "Natal", "peso": 0.5, "ativo": True},
                },
            },
        }
    }


def test_metas_da_loja_configurada():
    cfg = config_base()
    m = metas.metas_da_loja(cfg, "Natal", "2026-01")
    assert m["origem"] == "configurada"
    assert m["faturamento"] == {"prata": 120000.0, "ouro": 150000.0, "diamante": 180000.0}
    assert m["pa"] == {"prata": 2.0, "ouro": 2.5, "diamante": 3.0}


def test_metas_da_loja_fallback_legado():
    cfg = config_base()
    # Fevereiro não tem metas_mensais -> cai no legado (Ouro = 70000)
    m = metas.metas_da_loja(cfg, "Natal", "2026-02")
    assert m["origem"] == "estimada"
    assert m["faturamento"]["ouro"] == 70000.0
    assert m["faturamento"]["prata"] == round(70000.0 * 0.85, 2)
    assert m["faturamento"]["diamante"] == round(70000.0 * 1.20, 2)
    assert m["pa"] is None


def test_metas_da_loja_ausente():
    cfg = config_base()
    m = metas.metas_da_loja(cfg, "Mossoró", "2026-02")
    assert m["origem"] == "ausente"
    assert m["faturamento"] is None
    assert m["pa"] is None


# ---------------------------------------------------------------------
# classificar_nivel
# ---------------------------------------------------------------------

def test_classificar_nivel_nos_limites_exatos():
    metas_teste = {"prata": 100.0, "ouro": 200.0, "diamante": 300.0}

    assert metas.classificar_nivel(99.99, metas_teste)["nivel"] is None
    r_prata = metas.classificar_nivel(100.0, metas_teste)
    assert r_prata["nivel"] == "Prata"
    assert r_prata["proximo_nivel"] == "Ouro"
    assert r_prata["falta"] == 100.0

    r_ouro = metas.classificar_nivel(200.0, metas_teste)
    assert r_ouro["nivel"] == "Ouro"
    assert r_ouro["proximo_nivel"] == "Diamante"

    r_diamante = metas.classificar_nivel(300.0, metas_teste)
    assert r_diamante["nivel"] == "Diamante"
    assert r_diamante["proximo_nivel"] is None
    assert r_diamante["falta"] == 0.0
    assert r_diamante["pct_do_proximo"] == 1.0

    # Acima do diamante: continua Diamante, sem "falta" negativa
    r_acima = metas.classificar_nivel(500.0, metas_teste)
    assert r_acima["nivel"] == "Diamante"
    assert r_acima["falta"] == 0.0


def test_classificar_nivel_sem_metas():
    r = metas.classificar_nivel(1000.0, None)
    assert r == {"nivel": None, "proximo_nivel": None, "proximo_valor": None,
                 "falta": None, "pct_do_proximo": None}


# ---------------------------------------------------------------------
# resumo_faturamento / resumo_pa
# ---------------------------------------------------------------------

def test_resumo_faturamento_projeta_run_rate_e_ritmo():
    metas_teste = {"prata": 100.0, "ouro": 200.0, "diamante": 300.0}
    # dia 10 de um mês de 30 dias, vendido 60 -> run rate = 60/10*30 = 180
    r = metas.resumo_faturamento(vendido=60.0, metas=metas_teste, dia_atual=10, dias_no_mes=30)
    assert r["run_rate"] == 180.0
    assert r["nivel"] is None            # 60 < prata (100)
    assert r["nivel_projetado"] == "Prata"  # 180 >= prata, < ouro
    assert r["falta"] == 40.0            # 100 - 60
    # ritmo necessário: faltam 40 até a prata em (30-10)=20 dias restantes
    assert r["ritmo_necessario"] == 2.0


def test_resumo_faturamento_mes_fechado_sem_dias_restantes():
    metas_teste = {"prata": 100.0, "ouro": 200.0, "diamante": 300.0}
    r = metas.resumo_faturamento(vendido=50.0, metas=metas_teste, dia_atual=30, dias_no_mes=30)
    assert r["run_rate"] == 50.0
    assert r["ritmo_necessario"] is None  # mês fechado, não bateu, sem "dias restantes"


def test_resumo_pa_nao_tem_run_rate():
    metas_pa = {"prata": 2.0, "ouro": 2.5, "diamante": 3.0}
    r = metas.resumo_pa(pecas=25.0, pedidos=10.0, metas=metas_pa)
    assert r["pa"] == 2.5
    assert r["nivel"] == "Ouro"
    assert "run_rate" not in r


def test_resumo_pa_zero_pedidos():
    r = metas.resumo_pa(pecas=0, pedidos=0, metas=None)
    assert r["pa"] == 0.0


# ---------------------------------------------------------------------
# atribuicao_vendedores — herança do mês anterior mais recente
# ---------------------------------------------------------------------

def test_atribuicao_herda_do_mes_anterior():
    cfg = config_base()
    # Março não tem entrada -> herda de Janeiro (não existe Fevereiro no bloco)
    a = metas.atribuicao_vendedores(cfg, "2026-03")
    assert a["V1"]["loja"] == "Natal"


def test_atribuicao_nao_herda_do_futuro():
    cfg = config_base()
    a = metas.atribuicao_vendedores(cfg, "2025-12")
    assert a == {}


def test_atribuicao_mes_exato_vence_sem_precisar_herdar():
    cfg = {
        "daily": {
            "vendedores_loja": {
                "2026-01": {"V1": {"loja": "Natal", "peso": 1.0, "ativo": True}},
                "2026-03": {"V1": {"loja": "Mossoró", "peso": 1.0, "ativo": True}},
            },
        }
    }
    assert metas.atribuicao_vendedores(cfg, "2026-02")["V1"]["loja"] == "Natal"
    assert metas.atribuicao_vendedores(cfg, "2026-03")["V1"]["loja"] == "Mossoró"
    assert metas.atribuicao_vendedores(cfg, "2026-12")["V1"]["loja"] == "Mossoró"


# ---------------------------------------------------------------------
# metas_do_vendedor — rateio por peso
# ---------------------------------------------------------------------

def test_metas_do_vendedor_rateio_soma_a_meta_da_loja():
    cfg = config_base()
    m1 = metas.metas_do_vendedor(cfg, "V1", "Natal", "2026-01")
    m2 = metas.metas_do_vendedor(cfg, "V2", "Natal", "2026-01")
    m3 = metas.metas_do_vendedor(cfg, "V3", "Natal", "2026-01")

    # pesos 1.0, 1.0, 0.5 -> total 2.5
    total_ouro = 150000.0
    assert m1["faturamento"]["ouro"] == total_ouro * (1.0 / 2.5)
    assert m2["faturamento"]["ouro"] == total_ouro * (1.0 / 2.5)
    assert m3["faturamento"]["ouro"] == total_ouro * (0.5 / 2.5)

    soma = m1["faturamento"]["ouro"] + m2["faturamento"]["ouro"] + m3["faturamento"]["ouro"]
    assert abs(soma - total_ouro) < 1e-9

    # PA não se rateia: repete a meta da loja para todos
    assert m1["pa"] == m2["pa"] == m3["pa"] == {"prata": 2.0, "ouro": 2.5, "diamante": 3.0}


def test_metas_do_vendedor_sem_atribuicao_retorna_none():
    cfg = config_base()
    m = metas.metas_do_vendedor(cfg, "V_INEXISTENTE", "Natal", "2026-01")
    assert m["faturamento"] is None
    assert m["peso"] == 0.0


# ---------------------------------------------------------------------
# Agregação
# ---------------------------------------------------------------------

def test_agregar_faturamento_soma_direto():
    linhas = [
        {"vendido": 100.0, "metas": {"prata": 50.0, "ouro": 100.0, "diamante": 150.0}},
        {"vendido": 200.0, "metas": {"prata": 80.0, "ouro": 120.0, "diamante": 160.0}},
    ]
    r = metas.agregar_faturamento(linhas)
    assert r["vendido"] == 300.0
    assert r["metas"] == {"prata": 130.0, "ouro": 220.0, "diamante": 310.0}


def test_agregar_pa_e_media_ponderada_por_pedidos_nao_media_simples():
    # Loja A: 10 peças / 5 pedidos -> PA 2.0 | Loja B: 30 peças / 5 pedidos -> PA 6.0
    linhas = [
        {"pecas": 10.0, "pedidos": 5.0, "metas": {"prata": 1.0, "ouro": 2.0, "diamante": 3.0}},
        {"pecas": 30.0, "pedidos": 5.0, "metas": {"prata": 1.0, "ouro": 2.0, "diamante": 3.0}},
    ]
    r = metas.agregar_pa(linhas)
    # PA agregado = 40 peças / 10 pedidos = 4.0 (não a média simples de 2.0 e 6.0, que seria 4.0
    # coincidentemente aqui por pesos iguais — testamos pesos DESIGUAIS abaixo para provar a diferença)
    assert r["pa"] == 4.0

    linhas_desiguais = [
        {"pecas": 10.0, "pedidos": 1.0, "metas": None},   # PA 10.0, poucos pedidos
        {"pecas": 20.0, "pedidos": 19.0, "metas": None},  # PA ~1.05, muitos pedidos
    ]
    r2 = metas.agregar_pa(linhas_desiguais)
    media_simples = (10.0 + 20.0 / 19.0) / 2   # ~5.5, NÃO é o resultado esperado
    assert r2["pa"] == 30.0 / 20.0
    assert abs(r2["pa"] - media_simples) > 1.0


def test_agregar_pa_meta_ponderada_por_pedidos():
    linhas = [
        {"pecas": 10.0, "pedidos": 1.0, "metas": {"prata": 1.0, "ouro": 2.0, "diamante": 3.0}},
        {"pecas": 20.0, "pedidos": 9.0, "metas": {"prata": 4.0, "ouro": 5.0, "diamante": 6.0}},
    ]
    r = metas.agregar_pa(linhas)
    # meta ouro ponderada = (2*1 + 5*9) / 10 = 4.7 (não a média simples 3.5)
    assert r["metas"]["ouro"] == (2.0 * 1 + 5.0 * 9) / 10


def test_agregar_pa_sem_nenhuma_meta_retorna_none():
    linhas = [{"pecas": 10.0, "pedidos": 5.0, "metas": None}]
    r = metas.agregar_pa(linhas)
    assert r["metas"] is None


# ---------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------

def test_validar_metas_mensais_aceita_valido():
    bloco = {
        "2026-01": {
            "Natal": {
                "faturamento": {"prata": 100, "ouro": 200, "diamante": 300},
                "pa": {"prata": 2.0, "ouro": 2.5, "diamante": 3.0},
            }
        }
    }
    assert metas.validar_metas_mensais(bloco) == []


def test_validar_metas_mensais_rejeita_prata_maior_que_ouro():
    bloco = {
        "2026-01": {
            "Natal": {"faturamento": {"prata": 300, "ouro": 200, "diamante": 100}}
        }
    }
    erros = metas.validar_metas_mensais(bloco)
    assert len(erros) == 1
    assert "Natal" in erros[0] and "faturamento" in erros[0]


def test_validar_metas_mensais_rejeita_negativo():
    bloco = {
        "2026-01": {"Natal": {"faturamento": {"prata": -10, "ouro": 200, "diamante": 300}}}
    }
    erros = metas.validar_metas_mensais(bloco)
    assert any("negativo" in e for e in erros)


def test_validar_metas_mensais_permite_cadastro_parcial():
    # Só ouro preenchido (em progresso) -> não dispara erro de ordem
    bloco = {"2026-01": {"Natal": {"faturamento": {"ouro": 200}}}}
    assert metas.validar_metas_mensais(bloco) == []


# =====================================================================
# Edição (transformação editor -> dict persistido em app.parametros)
#
# É o caminho de ESCRITA da configuração: um bug aqui corrompe os
# parâmetros de todo mundo. Cobre principalmente o que DESTRÓI dado.
# =====================================================================

def _linha(mes, fat=(None, None, None), pa=(None, None, None)):
    return {
        "mes": mes,
        "fat_prata": fat[0], "fat_ouro": fat[1], "fat_diamante": fat[2],
        "pa_prata": pa[0], "pa_ouro": pa[1], "pa_diamante": pa[2],
    }


def test_aplicar_edicao_metas_grava_faturamento_e_pa():
    novo, n = metas.aplicar_edicao_metas(
        {}, 2026, "Natal", [_linha(1, fat=(100, 200, 300), pa=(2.0, 2.5, 3.0))])
    assert n == 1
    assert novo["2026-01"]["Natal"]["faturamento"] == {"prata": 100.0, "ouro": 200.0, "diamante": 300.0}
    assert novo["2026-01"]["Natal"]["pa"] == {"prata": 2.0, "ouro": 2.5, "diamante": 3.0}


def test_aplicar_edicao_metas_linha_vazia_nao_conta_como_mes():
    novo, n = metas.aplicar_edicao_metas({}, 2026, "Natal", [_linha(7)])
    assert n == 0
    assert novo == {}          # não cria competência órfã


def test_aplicar_edicao_metas_limpar_celulas_remove_a_meta():
    base = {"2026-01": {"Natal": {"faturamento": {"prata": 1, "ouro": 2, "diamante": 3}}}}
    novo, n = metas.aplicar_edicao_metas(base, 2026, "Natal", [_linha(1)])
    assert n == 0
    assert novo == {}          # competência sem loja nenhuma some


def test_aplicar_edicao_metas_nao_toca_nas_outras_lojas():
    base = {"2026-01": {
        "Natal": {"faturamento": {"ouro": 1}},
        "Mossoró": {"faturamento": {"ouro": 999}},
    }}
    novo, _ = metas.aplicar_edicao_metas(base, 2026, "Natal", [_linha(1, fat=(10, 20, 30))])
    assert novo["2026-01"]["Mossoró"]["faturamento"] == {"ouro": 999}
    assert novo["2026-01"]["Natal"]["faturamento"]["ouro"] == 20.0


def test_aplicar_edicao_metas_apagar_uma_loja_preserva_a_outra():
    base = {"2026-01": {
        "Natal": {"faturamento": {"ouro": 1}},
        "Mossoró": {"faturamento": {"ouro": 999}},
    }}
    novo, n = metas.aplicar_edicao_metas(base, 2026, "Natal", [_linha(1)])
    assert n == 0
    assert "Natal" not in novo["2026-01"]
    assert novo["2026-01"]["Mossoró"]["faturamento"] == {"ouro": 999}


def test_aplicar_edicao_metas_nao_toca_em_outros_anos():
    base = {"2025-01": {"Natal": {"faturamento": {"ouro": 555}}}}
    novo, _ = metas.aplicar_edicao_metas(base, 2026, "Natal", [_linha(1, fat=(10, 20, 30))])
    assert novo["2025-01"]["Natal"]["faturamento"] == {"ouro": 555}


def test_aplicar_edicao_metas_nao_muta_a_entrada():
    base = {"2026-01": {"Natal": {"faturamento": {"ouro": 1}}}}
    copia = {"2026-01": {"Natal": {"faturamento": {"ouro": 1}}}}
    metas.aplicar_edicao_metas(base, 2026, "Natal", [_linha(1, fat=(9, 9, 9))])
    assert base == copia


def test_aplicar_edicao_metas_trata_nan_como_celula_vazia():
    # O data_editor devolve NaN (não None) em célula limpa
    nan = float("nan")
    novo, n = metas.aplicar_edicao_metas(
        {}, 2026, "Natal", [_linha(1, fat=(nan, 200, nan))])
    assert n == 1
    assert novo["2026-01"]["Natal"]["faturamento"] == {"prata": None, "ouro": 200.0, "diamante": None}


def test_aplicar_edicao_metas_zero_e_meta_valida_nao_celula_vazia():
    novo, n = metas.aplicar_edicao_metas(
        {}, 2026, "Natal", [_linha(1, fat=(0, 0, 0))])
    assert n == 1
    assert novo["2026-01"]["Natal"]["faturamento"] == {"prata": 0.0, "ouro": 0.0, "diamante": 0.0}


def test_aplicar_edicao_metas_so_pa_sem_faturamento():
    novo, n = metas.aplicar_edicao_metas(
        {}, 2026, "Natal", [_linha(1, pa=(2.0, 2.5, 3.0))])
    assert n == 1
    assert "faturamento" not in novo["2026-01"]["Natal"]
    assert novo["2026-01"]["Natal"]["pa"]["ouro"] == 2.5


def test_aplicar_edicao_vendedores_descarta_sem_atribuicao():
    linhas = [
        {"vendedor_id": "1", "loja": "Natal", "peso": 1.0, "ativo": True},
        {"vendedor_id": "2", "loja": "— sem atribuição —", "peso": 1.0, "ativo": True},
    ]
    novo = metas.aplicar_edicao_vendedores({}, "2026-01", linhas, "— sem atribuição —")
    assert list(novo["2026-01"].keys()) == ["1"]


def test_aplicar_edicao_vendedores_normaliza_tipos():
    linhas = [{"vendedor_id": 42, "loja": "Natal", "peso": "0.5", "ativo": 1}]
    novo = metas.aplicar_edicao_vendedores({}, "2026-01", linhas, "—")
    assert novo["2026-01"]["42"] == {"loja": "Natal", "peso": 0.5, "ativo": True}


def test_aplicar_edicao_vendedores_competencia_vazia_e_removida():
    base = {"2026-01": {"1": {"loja": "Natal", "peso": 1.0, "ativo": True}}}
    novo = metas.aplicar_edicao_vendedores(base, "2026-01", [], "—")
    assert "2026-01" not in novo


def test_aplicar_edicao_vendedores_preserva_outras_competencias():
    base = {"2025-06": {"9": {"loja": "Mossoró", "peso": 1.0, "ativo": True}}}
    linhas = [{"vendedor_id": "1", "loja": "Natal", "peso": 1.0, "ativo": True}]
    novo = metas.aplicar_edicao_vendedores(base, "2026-01", linhas, "—")
    assert novo["2025-06"]["9"]["loja"] == "Mossoró"
    assert novo["2026-01"]["1"]["loja"] == "Natal"


def test_aplicar_edicao_vendedores_nao_muta_a_entrada():
    base = {"2026-01": {"1": {"loja": "Natal", "peso": 1.0, "ativo": True}}}
    metas.aplicar_edicao_vendedores(base, "2026-02", [], "—")
    assert "2026-01" in base and len(base) == 1


# =====================================================================
# Histórico de atingimento (Fase 6)
# =====================================================================

def test_competencias_anteriores_atravessa_a_virada_do_ano():
    assert metas.competencias_anteriores("2026-02", 4) == [
        "2025-11", "2025-12", "2026-01", "2026-02"]


def test_competencias_anteriores_um_mes_e_ele_mesmo():
    assert metas.competencias_anteriores("2026-08", 1) == ["2026-08"]


def _cfg_hist():
    return {"daily": {"metas_mensais": {
        "2026-01": {"Natal": {"faturamento": {"prata": 100, "ouro": 200, "diamante": 300}}},
        "2026-02": {"Natal": {"faturamento": {"prata": 100, "ouro": 200, "diamante": 300}}},
    }}}


def test_historico_classifica_nivel_por_mes():
    realizado = {
        ("2026-01", "Natal"): {"vendido": 250, "pecas": 10, "pedidos": 4},
        ("2026-02", "Natal"): {"vendido": 90, "pecas": 5, "pedidos": 2},
    }
    h = metas.historico_atingimento(_cfg_hist(), ["Natal"], realizado,
                                    ["2026-01", "2026-02"])
    assert [x["nivel"] for x in h] == ["Ouro", None]     # 250 -> ouro; 90 -> nenhum
    assert all(x["tem_meta"] for x in h)


def test_historico_mes_sem_meta_marca_lacuna_nao_zero():
    h = metas.historico_atingimento(_cfg_hist(), ["Natal"], {}, ["2025-12"])
    assert h[0]["tem_meta"] is False
    assert h[0]["nivel"] is None
    assert h[0]["vendido"] == 0.0


def test_historico_agrega_lojas_no_mes():
    cfg = {"daily": {"metas_mensais": {"2026-01": {
        "Natal": {"faturamento": {"prata": 100, "ouro": 200, "diamante": 300}},
        "Mossoró": {"faturamento": {"prata": 50, "ouro": 100, "diamante": 150}},
    }}}}
    realizado = {
        ("2026-01", "Natal"): {"vendido": 150, "pecas": 6, "pedidos": 3},
        ("2026-01", "Mossoró"): {"vendido": 160, "pecas": 4, "pedidos": 1},
    }
    h = metas.historico_atingimento(cfg, ["Natal", "Mossoró"], realizado, ["2026-01"])
    assert h[0]["vendido"] == 310            # 150 + 160
    assert h[0]["metas"]["ouro"] == 300      # 200 + 100
    assert h[0]["nivel"] == "Ouro"           # 310 >= 300


def test_historico_pa_agrega_por_media_ponderada_nao_media_dos_pas():
    cfg = {"daily": {"metas_mensais": {"2026-01": {
        "Natal": {"pa": {"prata": 1.0, "ouro": 2.0, "diamante": 3.0}},
    }}}}
    realizado = {("2026-01", "Natal"): {"vendido": 0, "pecas": 10, "pedidos": 4}}
    h = metas.historico_atingimento(cfg, ["Natal"], realizado, ["2026-01"])
    assert h[0]["pa"] == 2.5                 # 10/4, não média de PAs
    assert h[0]["nivel_pa"] == "Ouro"


def test_historico_sem_pedidos_nao_classifica_pa():
    cfg = {"daily": {"metas_mensais": {"2026-01": {
        "Natal": {"pa": {"prata": 1.0, "ouro": 2.0, "diamante": 3.0}}}}}}
    h = metas.historico_atingimento(cfg, ["Natal"], {}, ["2026-01"])
    assert h[0]["nivel_pa"] is None          # 0 pedidos -> PA indefinido, não 0


def test_historico_origem_configurada_so_quando_todas_as_lojas_tem_cadastro():
    cfg = {"daily": {
        "metas_mensais": {"2026-01": {"Natal": {"faturamento": {"prata": 1, "ouro": 2, "diamante": 3}}}},
        "metas": {"Mossoró": 1000.0},          # legado -> fallback "estimada"
    }}
    # Só Natal: cadastro real
    h1 = metas.historico_atingimento(cfg, ["Natal"], {}, ["2026-01"])
    assert h1[0]["origem"] == "configurada"
    # Natal + Mossoró: Mossoró cai no legado -> o mês inteiro vira "estimada"
    h2 = metas.historico_atingimento(cfg, ["Natal", "Mossoró"], {}, ["2026-01"])
    assert h2[0]["origem"] == "estimada"


def test_historico_origem_ausente_quando_nenhuma_fonte_tem_meta():
    h = metas.historico_atingimento({"daily": {}}, ["Natal"], {}, ["2026-01"])
    assert h[0]["origem"] == "ausente"
    assert h[0]["tem_meta"] is False


# =====================================================================
# Atalhos de preenchimento da grade
# =====================================================================

def _base12(preenchidos=None):
    """12 linhas vazias; `preenchidos` = {mes: (fat_ouro, pa_ouro)}."""
    preenchidos = preenchidos or {}
    out = []
    for m in range(1, 13):
        f, p = preenchidos.get(m, (None, None))
        out.append({
            "mes": m,
            "fat_prata": None if f is None else f * 0.85,
            "fat_ouro": f,
            "fat_diamante": None if f is None else f * 1.2,
            "pa_prata": None, "pa_ouro": p, "pa_diamante": None,
        })
    return out


def test_copiar_do_ano_anterior_traz_metas_e_aplica_fator():
    mm = {"2025-03": {"Natal": {"faturamento": {"prata": 100, "ouro": 200, "diamante": 300},
                                "pa": {"prata": 1.0, "ouro": 2.0, "diamante": 3.0}}}}
    linhas, n = metas.copiar_do_ano_anterior(_base12(), mm, 2026, "Natal", fator=1.10)
    assert n == 1
    marco = linhas[2]
    assert marco["fat_ouro"] == 220.0          # 200 * 1.10
    assert marco["fat_prata"] == 110.0
    assert marco["pa_ouro"] == 2.2
    assert linhas[0]["fat_ouro"] is None        # meses sem origem seguem vazios


def test_copiar_do_ano_anterior_fator_1_e_copia_literal():
    mm = {"2025-01": {"Natal": {"faturamento": {"prata": 100, "ouro": 200, "diamante": 300}}}}
    linhas, n = metas.copiar_do_ano_anterior(_base12(), mm, 2026, "Natal")
    assert (linhas[0]["fat_prata"], linhas[0]["fat_ouro"]) == (100.0, 200.0)


def test_copiar_do_ano_anterior_ignora_outra_loja():
    mm = {"2025-01": {"Mossoró": {"faturamento": {"ouro": 999}}}}
    linhas, n = metas.copiar_do_ano_anterior(_base12(), mm, 2026, "Natal")
    assert n == 0 and linhas[0]["fat_ouro"] is None


def test_copiar_do_ano_anterior_nao_muta_entrada():
    mm = {"2025-01": {"Natal": {"faturamento": {"ouro": 200}}}}
    base = _base12()
    metas.copiar_do_ano_anterior(base, mm, 2026, "Natal")
    assert base[0]["fat_ouro"] is None


def test_replicar_nos_vazios_usa_a_primeira_preenchida():
    linhas, n = metas.replicar_nos_vazios(_base12({3: (5000.0, 2.5)}))
    assert n == 11                              # os outros 11 meses
    assert all(l["fat_ouro"] == 5000.0 for l in linhas)
    assert all(l["pa_ouro"] == 2.5 for l in linhas)


def test_replicar_nos_vazios_nao_sobrescreve_preenchidos():
    linhas, n = metas.replicar_nos_vazios(_base12({1: (100.0, 2.0), 6: (999.0, 4.0)}))
    assert n == 10
    assert linhas[5]["fat_ouro"] == 999.0       # junho preservado
    assert linhas[1]["fat_ouro"] == 100.0       # vazio recebeu o modelo (janeiro)


def test_replicar_nos_vazios_grade_toda_vazia_e_no_op():
    linhas, n = metas.replicar_nos_vazios(_base12())
    assert n == 0 and all(l["fat_ouro"] is None for l in linhas)


def test_replicar_nos_vazios_trata_nan_como_vazio():
    base = _base12({1: (100.0, 2.0)})
    for c in ("fat_prata", "fat_ouro", "fat_diamante", "pa_prata", "pa_ouro", "pa_diamante"):
        base[4][c] = float("nan")
    linhas, n = metas.replicar_nos_vazios(base)
    assert linhas[4]["fat_ouro"] == 100.0

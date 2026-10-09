"""
Testes das regras puras de escrita dos editores de Colégios
(etl/config_edicao.py).

Sem streamlit/pandas/Supabase — só dicts. O foco é o que NÃO pode ser gravado:
salvar a tabela sem digitar nada tem de deixar o config intacto, senão o
default da tela vira override e desliga o crescimento medido (a cascata de
`taxa_crescimento_efetiva` trata a presença da chave como decisão manual).
"""

from etl import config_edicao as ce
from etl.demanda import taxa_crescimento_efetiva


def linha(colegio, taxa=None, ns=None, pb=None):
    return {"colegio": colegio, "taxa_crescimento": taxa,
            "nivel_servico": ns, "proporcao_baixa": pb}


# ---------------------------------------------------------------------
# aplicar_edicao_colegios
# ---------------------------------------------------------------------

def test_salvar_sem_editar_nao_grava_override():
    novo, n = ce.aplicar_edicao_colegios({}, [linha("NEV"), linha("LMN")])
    assert novo == {}
    assert n == 0


def test_salvar_sem_editar_mantem_o_crescimento_medido():
    """O defeito que motivou o módulo: o medido tem de continuar valendo."""
    observado = {"NEV": {"__geral__": 1.5, "segmentos": {}}}
    novo, _ = ce.aplicar_edicao_colegios({}, [linha("NEV")])
    config = {"colegios": novo, "fabrica": {"crescimento_pct": 10.0}}
    assert taxa_crescimento_efetiva("NEV", config, None, True, observado) == 1.5


def test_celula_preenchida_vira_override_tipado():
    novo, n = ce.aplicar_edicao_colegios({}, [linha("NEV", taxa=1.2, ns=99.0, pb=0.6)])
    assert novo == {"NEV": {"taxa_crescimento": 1.2, "nivel_servico": 99,
                            "proporcao_baixa": 0.6}}
    assert isinstance(novo["NEV"]["nivel_servico"], int)
    assert n == 3


def test_limpar_a_celula_remove_o_override():
    atual = {"NEV": {"taxa_crescimento": 1.2, "nivel_servico": 99}}
    novo, n = ce.aplicar_edicao_colegios(atual, [linha("NEV", ns=99)])
    assert novo == {"NEV": {"nivel_servico": 99}}
    assert n == 1


def test_colegio_que_ficou_sem_campo_sai_do_dict():
    atual = {"NEV": {"taxa_crescimento": 1.2}}
    novo, _ = ce.aplicar_edicao_colegios(atual, [linha("NEV")])
    assert novo == {}


def test_nan_do_editor_conta_como_vazio():
    novo, n = ce.aplicar_edicao_colegios({}, [linha("NEV", taxa=float("nan"))])
    assert novo == {}
    assert n == 0


def test_preserva_a_matriz_por_serie():
    atual = {"NEV": {"taxa_crescimento": 1.2, "crescimento_grupos": {"EME": 1.5}}}
    novo, _ = ce.aplicar_edicao_colegios(atual, [linha("NEV")])
    assert novo == {"NEV": {"crescimento_grupos": {"EME": 1.5}}}


def test_colegio_fora_da_tabela_passa_intacto():
    atual = {"OVD": {"taxa_crescimento": 0.8}}
    novo, _ = ce.aplicar_edicao_colegios(atual, [linha("NEV", taxa=1.1)])
    assert novo["OVD"] == {"taxa_crescimento": 0.8}
    assert novo["NEV"] == {"taxa_crescimento": 1.1}


def test_nao_muta_a_entrada():
    atual = {"NEV": {"taxa_crescimento": 1.2}}
    ce.aplicar_edicao_colegios(atual, [linha("NEV")])
    assert atual == {"NEV": {"taxa_crescimento": 1.2}}


# ---------------------------------------------------------------------
# aplicar_edicao_crescimento_grupos
# ---------------------------------------------------------------------

def celula(colegio, grupo, taxa, base):
    return {"colegio": colegio, "grupo": grupo, "taxa_crescimento": taxa, "base": base}


def test_celula_igual_a_base_fica_viva():
    novo, n = ce.aplicar_edicao_crescimento_grupos(
        {}, [celula("NEV", "EME", 1.51, 1.51), celula("NEV", "EF1", 1.1, 1.1)])
    assert novo == {}
    assert n == 0


def test_celula_diferente_da_base_vira_override():
    novo, n = ce.aplicar_edicao_crescimento_grupos(
        {}, [celula("NEV", "EME", 1.3, 1.51), celula("NEV", "EF1", 1.1, 1.1)])
    assert novo == {"NEV": {"crescimento_grupos": {"EME": 1.3}}}
    assert n == 1


def test_voltar_a_base_remove_o_override():
    atual = {"NEV": {"crescimento_grupos": {"EME": 1.3}}}
    novo, n = ce.aplicar_edicao_crescimento_grupos(
        atual, [celula("NEV", "EME", 1.51, 1.51)])
    assert novo == {}
    assert n == 0


def test_celula_vazia_nao_vira_override():
    novo, _ = ce.aplicar_edicao_crescimento_grupos(
        {}, [celula("NEV", "EME", None, 1.51)])
    assert novo == {}


def test_matriz_preserva_os_campos_do_colegio():
    atual = {"NEV": {"taxa_crescimento": 1.2, "nivel_servico": 99,
                     "crescimento_grupos": {"EME": 1.3}}}
    novo, _ = ce.aplicar_edicao_crescimento_grupos(
        atual, [celula("NEV", "EME", 1.2, 1.2)])
    assert novo == {"NEV": {"taxa_crescimento": 1.2, "nivel_servico": 99}}


def test_matriz_nao_toca_colegio_fora_dela():
    atual = {"OVD": {"crescimento_grupos": {"EF1": 0.7}}}
    novo, _ = ce.aplicar_edicao_crescimento_grupos(
        atual, [celula("NEV", "EME", 1.3, 1.51)])
    assert novo["OVD"] == {"crescimento_grupos": {"EF1": 0.7}}


def test_matriz_nao_muta_a_entrada():
    atual = {"NEV": {"crescimento_grupos": {"EME": 1.3}}}
    ce.aplicar_edicao_crescimento_grupos(atual, [celula("NEV", "EME", 1.51, 1.51)])
    assert atual == {"NEV": {"crescimento_grupos": {"EME": 1.3}}}


# ---------------------------------------------------------------------
# Reposição de Loja — lojas, capacidade da gaveta, sortimento
# ---------------------------------------------------------------------

def test_lojas_gavetas_vazio_nao_e_zero():
    """Vazio = sem teto de espaço; zero = a loja só tem a arara."""
    novo = ce.aplicar_edicao_lojas([
        {"loja": "Natal", "prazo_entrega_dias": 1.0, "gavetas": 20.0},
        {"loja": "Mossoró", "prazo_entrega_dias": 2, "gavetas": None},
        {"loja": "Nova", "prazo_entrega_dias": None, "gavetas": 0},
    ])
    assert novo == {
        "Natal": {"prazo_entrega_dias": 1, "gavetas": 20},
        "Mossoró": {"prazo_entrega_dias": 2},
        "Nova": {"prazo_entrega_dias": 0, "gavetas": 0},
    }


def test_lojas_ignora_linha_sem_nome_e_negativo_vira_zero():
    novo = ce.aplicar_edicao_lojas([
        {"loja": " ", "prazo_entrega_dias": 5, "gavetas": 5},
        {"loja": "Natal", "prazo_entrega_dias": -3, "gavetas": -1},
    ])
    assert novo == {"Natal": {"prazo_entrega_dias": 0, "gavetas": 0}}


def test_capacidade_so_grava_o_que_difere_do_padrao():
    novo = ce.aplicar_edicao_capacidade([
        {"super_categoria": "Camiseta", "pecas": 50},      # igual ao padrão → não grava
        {"super_categoria": "Calça", "pecas": 30.0},
        {"super_categoria": "Meia", "pecas": None},        # vazio → segue o padrão
    ], padrao=50)
    assert novo == {"_padrao": 50, "Calça": 30}


def test_capacidade_padrao_invalido_cai_em_50_e_minimo_e_1():
    novo = ce.aplicar_edicao_capacidade([{"super_categoria": "Boné", "pecas": 0}], padrao=None)
    assert novo == {"_padrao": 50, "Boné": 1}


def test_sortimento_toda_loja_sai_com_chave():
    """Loja sem nenhum colégio marcado precisa da chave: é ela que impede o
    motor de voltar ao derivado das vendas."""
    novo = ce.aplicar_edicao_sortimento([
        {"colegio": "SES", "Natal": True, "Mossoró": True},
        {"colegio": "NEV", "Natal": True, "Mossoró": False},
        {"colegio": "DRM", "Natal": None, "Mossoró": None},
    ], ["Natal", "Mossoró", "Caicó"])
    assert novo == {"Natal": ["NEV", "SES"], "Mossoró": ["SES"], "Caicó": []}


def test_sortimento_cadastrado_e_o_que_o_motor_le():
    from etl import reposicao
    cadastro = ce.aplicar_edicao_sortimento(
        [{"colegio": "SES", "Natal": True, "Mossoró": False}], ["Natal", "Mossoró"])
    params = reposicao.parametros({"reposicao": {"sortimento": cadastro}})
    sortimento, origem = reposicao.sortimento_efetivo(
        params, ["Natal", "Mossoró"], {"Mossoró": {"SES", "NEV"}})
    assert sortimento == {"Natal": {"SES"}, "Mossoró": set()}
    assert set(origem.values()) == {reposicao.ORIGEM_CADASTRO}

"""
Padrão das tabelas (ui_tabelas.py): altura por tipo, arredondamento antes de
exibir e os formatadores pt-BR. Tudo puro — nada aqui desenha na tela.
"""

import pandas as pd
import pytest

import ui_tabelas as t


# -----------------------------------------------------------------
# Formatadores de texto
# -----------------------------------------------------------------
@pytest.mark.parametrize("valor, casas, esperado", [
    (125430, 0, "125.430"),
    (1234.5, 2, "1.234,50"),
    (0, 0, "0"),
    (-1500.4, 0, "-1.500"),
    (None, 0, "—"),
    (float("nan"), 2, "—"),
])
def test_num_pt_br(valor, casas, esperado):
    assert t.num(valor, casas) == esperado


def test_brl_prefixa_real_e_preserva_vazio():
    assert t.brl(125430) == "R$ 125.430"
    assert t.brl(1234.5, 2) == "R$ 1.234,50"
    assert t.brl(None) == "—"          # nunca "R$ —"


# -----------------------------------------------------------------
# Altura
# -----------------------------------------------------------------
def test_altura_cresce_com_as_linhas_ate_o_teto():
    base = t.ALTURA_CABECALHO + t._FOLGA
    assert t.altura_tabela(5, t.FILA) == 5 * 28 + base
    assert t.altura_tabela(5, t.PLACAR) == 5 * 35 + base
    # acima do teto a tabela rola em vez de crescer
    assert t.altura_tabela(5000, t.FILA) == t.MAX_LINHAS[t.FILA] * 28 + base


def test_altura_de_tabela_vazia_reserva_uma_linha():
    # 0 linhas ainda desenha o estado vazio do Streamlit — sem isso some o cabeçalho
    assert t.altura_tabela(0, t.FILA) == t.altura_tabela(1, t.FILA)


def test_teto_por_chamada_vence_o_do_tipo():
    assert t.altura_tabela(100, t.EDITOR, max_linhas=12) == t.altura_tabela(12, t.EDITOR)


def test_padrao_tabela_carrega_a_linha_do_tipo():
    assert t.padrao_tabela(t.FILA, 10)["row_height"] == 28
    assert t.padrao_tabela(t.MEMORIA, 10)["row_height"] == 28
    assert t.padrao_tabela(t.PLACAR, 10)["row_height"] == 35
    assert t.padrao_tabela(t.EDITOR, 10)["row_height"] == 35
    assert t.padrao_tabela(t.FILA, 10)["hide_index"] is True
    # célula sem valor fica VAZIA — o default do Streamlit escreve "None"
    assert t.padrao_tabela(t.FILA, 10)["placeholder"] == ""


# -----------------------------------------------------------------
# Arredondamento antes de exibir (o Streamlit trunca)
# -----------------------------------------------------------------
def test_arredonda_float_nas_casas_da_coluna():
    df = pd.DataFrame({"pecas": [15482.9, 3475.97], "valor": [45464.396, 10.004]})
    colunas = {"pecas": t.col_pecas("Peças"), "valor": t.col_moeda("Valor", casas=2)}
    saida = t.arredondar_para_exibicao(df, colunas)
    assert saida["pecas"].tolist() == [15483.0, 3476.0]
    assert saida["valor"].tolist() == [45464.40, 10.00]
    # a entrada não é alterada
    assert df["pecas"].tolist() == [15482.9, 3475.97]


def test_nao_toca_em_inteiro_texto_ou_coluna_sem_config():
    df = pd.DataFrame({"qtd": [1, 2], "sku": ["A", "B"], "livre": [1.239, 2.5]})
    colunas = {"qtd": t.col_pecas("Qtd"), "sku": t.col_sku(),
               "ausente": t.col_pecas("Não existe no df")}
    saida = t.arredondar_para_exibicao(df, colunas)
    assert saida["livre"].tolist() == [1.239, 2.5]
    assert saida["qtd"].tolist() == [1, 2]


def test_percentual_printf_nao_e_arredondado_pelo_molde():
    # col_pct usa printf (arredonda sozinho) e não declara step
    df = pd.DataFrame({"pct": [12.56]})
    saida = t.arredondar_para_exibicao(df, {"pct": t.col_pct("%", casas=1)})
    assert saida["pct"].tolist() == [12.56]


def test_nan_sobrevive_ao_arredondamento():
    df = pd.DataFrame({"meta": [1000.6, float("nan")]})
    saida = t.arredondar_para_exibicao(df, {"meta": t.col_moeda("Meta")})
    assert saida["meta"].iloc[0] == 1001.0
    assert pd.isna(saida["meta"].iloc[1])


# -----------------------------------------------------------------
# Vocabulário das colunas
# -----------------------------------------------------------------
def test_moeda_leva_a_unidade_ao_cabecalho():
    assert t.col_moeda("Investimento")["label"] == "Investimento (R$)"


def test_sku_e_fixo_e_casas_viram_step():
    assert t.col_sku()["pinned"] is True
    assert t.col_numero("PA", casas=2)["type_config"]["step"] == pytest.approx(0.01)
    assert t.col_pecas("Qtd")["type_config"]["step"] == 1
    assert t._casas_da_coluna(t.col_numero("PA", casas=2)) == 2
    assert t._casas_da_coluna(t.col_pecas("Qtd")) == 0


class TestDestacar:
    def test_pinta_so_as_celulas_pedidas(self):
        from ui_tabelas import ESTILO_ALTERADO, destacar
        df = pd.DataFrame({"sku": ["A", "B"], "qtd": [1, 2]})
        styler = destacar(df, [(1, "qtd")])
        styler._compute()
        pintadas = {pos: dict(regras) for pos, regras in styler.ctx.items() if regras}
        assert list(pintadas) == [(1, 1)]
        assert pintadas[(1, 1)]["background-color"] in ESTILO_ALTERADO

    def test_celula_inexistente_e_ignorada(self):
        from ui_tabelas import destacar
        df = pd.DataFrame({"sku": ["A"], "qtd": [1]})
        styler = destacar(df, [(5, "qtd"), (0, "sumiu")])
        styler._compute()
        assert not any(styler.ctx.values())

    def test_nao_altera_o_dado(self):
        from ui_tabelas import destacar
        df = pd.DataFrame({"sku": ["A"], "qtd": pd.array([None], dtype="Int64")})
        pd.testing.assert_frame_equal(destacar(df, [(0, "sku")]).data, df)

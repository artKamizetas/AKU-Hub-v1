"""
Testes da visão em grade (pedidos/grade.py) e do catálogo da inclusão manual
(pedidos/catalogo.py) — ambos puros, DataFrames sintéticos.

O que merece teste aqui é o que corrompe dado em silêncio: a ordem dos
tamanhos (grade ilegível), a volta da grade para o banco por id (quantidade no
SKU errado) e a resolução de célula nova (SKU inventado).
"""

import pandas as pd

from pedidos import catalogo, estados, grade


def itens_df(linhas):
    """[(id, sku, produto, tamanho, qtd_final)] → DataFrame no formato de listar_itens."""
    return pd.DataFrame(linhas, columns=["id", "sku", "produto", "tamanho", "quantidade_final"])


# ---------------------------------------------------------------------------
# Regras de leitura de SKU / produto / tamanho
# ---------------------------------------------------------------------------
class TestRegras:
    def test_ordem_das_letras_desafia_o_alfabeto(self):
        baguncado = ["XGG", "G", "PP", "M", "GG", "P", "XXGG", "PPP"]
        assert grade.ordenar_tamanhos(baguncado) == [
            "PPP", "PP", "P", "M", "G", "GG", "XGG", "XXGG"]

    def test_tamanho_fora_da_lista_cai_no_lugar_pela_regra(self):
        # nenhum destes está escrito em tabela: saem da regra P desce / G sobe / X empurra
        assert grade.ordenar_tamanhos(["XXXGG", "XG", "EG", "G3", "GG", "XP", "G1"]) == [
            "XP", "GG", "EG", "XG", "XXXGG", "G1", "G3"]

    def test_numeros_ordenam_pelo_valor_nao_pelo_texto(self):
        assert grade.ordenar_tamanhos(["10", "02", "8", "38", "4", "30/36"]) == [
            "02", "4", "8", "10", "30/36", "38"]

    def test_blocos_letras_numeros_desconhecido_unico(self):
        assert grade.ordenar_tamanhos([grade.TAMANHO_UNICO, "N", "12", "M", "E"]) == [
            "M", "12", "E", "N", grade.TAMANHO_UNICO]

    def test_produto_sem_variacao(self):
        f = grade.produto_sem_variacao
        assert f("Neves - Calça Feminina em Helanca - EM Tamanho:M") == \
            "Neves - Calça Feminina em Helanca - EM"
        assert f("Facex - Calça em Tactel - EF e EM Numeração:50") == \
            "Facex - Calça em Tactel - EF e EM"
        assert f("Tenis Pampili Luna Numeração:29;COR:BRANCO") == "Tenis Pampili Luna"
        assert f("Peça Inferior Sob Encomenda") == "Peça Inferior Sob Encomenda"
        assert f(None) == ""

    def test_tamanho_efetivo_cai_no_sufixo_do_sku_e_depois_em_unico(self):
        assert grade.tamanho_efetivo("M", "ABC-M") == "M"
        assert grade.tamanho_efetivo("nan", "TEN001-34") == "34"
        assert grade.tamanho_efetivo(float("nan"), "TEN001-34") == "34"
        assert grade.tamanho_efetivo("", "ENCOMENDA") == grade.TAMANHO_UNICO

    def test_familia(self):
        assert grade.familia("NEV019CLFEME-M") == "NEV019CLFEME"
        assert grade.familia("ENCOMENDA") == "ENCOMENDA"


# ---------------------------------------------------------------------------
# montar_grade / diff_grade
# ---------------------------------------------------------------------------
class TestGrade:
    def itens(self):
        return itens_df([
            ("i1", "CAM-G", "Camiseta Dry Tamanho:G", "G", 6),
            ("i2", "CAM-PP", "Camiseta Dry Tamanho:PP", "PP", 2),
            ("i3", "CAM-M", "Camiseta Dry Tamanho:M", "M", 0),
            ("i4", "CAL-M", "Calça Helanca Tamanho:M", "M", 4),
        ])

    def test_pivota_por_sku_pai_com_colunas_na_ordem_da_confeccao(self):
        g, celulas = grade.montar_grade(self.itens())
        assert list(g.columns) == ["SKU", "Produto", "PP", "M", "G"]
        assert g["SKU"].tolist() == ["CAL", "CAM"]
        assert g["Produto"].tolist() == ["Calça Helanca", "Camiseta Dry"]
        cam = g[g["SKU"] == "CAM"].iloc[0]
        assert (cam["PP"], cam["M"], cam["G"]) == (2, 0, 6)
        assert celulas[("CAM", "G")] == "i1"

    def test_celula_vazia_e_diferente_de_item_zerado(self):
        g, celulas = grade.montar_grade(self.itens())
        cal = g[g["SKU"] == "CAL"].iloc[0]
        assert pd.isna(cal["PP"]) and ("CAL", "PP") not in celulas   # não existe no pedido
        cam = g[g["SKU"] == "CAM"].iloc[0]
        assert cam["M"] == 0 and ("CAM", "M") in celulas             # existe, zerado

    def test_tamanho_repetido_na_familia_nao_perde_item(self):
        # cadastro sujo: dois SKUs da mesma família marcados 'M'
        itens = itens_df([
            ("a", "TEN-34", "Tenis Luna Numeração:34;COR:PRETO", "M", 1),
            ("b", "TEN-36", "Tenis Luna Numeração:36;COR:PRETO", "M", 2),
        ])
        g, celulas = grade.montar_grade(itens)
        assert list(g.columns) == ["SKU", "Produto", "34", "36"]
        assert set(celulas.values()) == {"a", "b"}

    def test_pedido_sem_itens(self):
        g, celulas = grade.montar_grade(pd.DataFrame())
        assert len(g) == 0 and celulas == {}

    def test_diff_devolve_so_o_que_mudou_pelo_id_do_item(self):
        itens = self.itens()
        g, celulas = grade.montar_grade(itens)
        g.loc[g["SKU"] == "CAM", "G"] = 10       # altera i1
        g.loc[g["SKU"] == "CAM", "PP"] = pd.NA   # apagar a célula = zerar i2
        alteracoes, novas = grade.diff_grade(g, itens, celulas)
        assert sorted(alteracoes, key=lambda a: a["id"]) == [
            {"id": "i1", "quantidade_final": 10}, {"id": "i2", "quantidade_final": 0}]
        assert novas == []

    def test_diff_sem_edicao_e_vazio(self):
        itens = self.itens()
        g, celulas = grade.montar_grade(itens)
        assert grade.diff_grade(g, itens, celulas) == ([], [])

    def test_celula_vazia_preenchida_vira_inclusao_e_nao_alteracao(self):
        itens = self.itens()
        g, celulas = grade.montar_grade(itens)
        g.loc[g["SKU"] == "CAL", "G"] = 8
        g.loc[g["SKU"] == "CAL", "PP"] = 0       # zero em célula vazia não inclui nada
        alteracoes, novas = grade.diff_grade(g, itens, celulas)
        assert alteracoes == []
        assert novas == [{"sku_pai": "CAL", "tamanho": "G", "quantidade": 8}]

    def test_quantidades_por_item_alimenta_os_totais(self):
        itens = self.itens()
        g, celulas = grade.montar_grade(itens)
        assert grade.quantidades_por_item(g, celulas) == {"i1": 6, "i2": 2, "i3": 0, "i4": 4}


# ---------------------------------------------------------------------------
# Catálogo da inclusão manual
# ---------------------------------------------------------------------------
def dados_catalogo():
    produtos = pd.DataFrame([
        # ID, codigo, Descricao, preco_custo
        ("1", "CAM", "Camiseta Dry", 0.0),                       # produto-PAI do Bling
        ("2", "CAM-P", "Camiseta Dry Tamanho:P", 20.0),
        ("3", "CAM-M", "Camiseta Dry Tamanho:M", 20.0),
        ("4", "CAM-GG", "Camiseta Dry Tamanho:GG", 22.5),
        ("5", "CAL-M", "Calça Helanca Tamanho:M", 35.0),
        ("6", "LACO", "Laço de cabelo", None),                   # sem variação, sem custo
        ("7", "TEN-34", "Tenis Luna Numeração:34", 90.0),        # sem detalhe cadastrado
    ], columns=["ID", "codigo", "Descricao", "preco_custo"])
    detalhes = pd.DataFrame([
        ("2", "CAI", "CAMISETAS", "P", "NEVES"),
        ("3", "CAI", "CAMISETAS", "M", "NEVES"),
        ("4", "CAI", "CAMISETAS", "GG", "NEVES"),
        ("5", "CLF", "CALÇAS", "M", "NEVES"),
        ("6", "ACE", "ACESSÓRIOS", "", ""),
    ], columns=["ID_produto", "categoria", "Super_categoria", "Tamanho", "Marca_sku"])
    return produtos, detalhes


class TestCatalogo:
    def test_produto_pai_do_bling_fica_fora(self):
        cat = catalogo.montar_catalogo(*dados_catalogo())
        assert "CAM" not in cat["sku"].tolist()
        assert {"CAM-P", "CAM-M", "CAM-GG", "LACO"} <= set(cat["sku"])

    def test_dimensoes_normalizadas_como_no_pedido(self):
        cat = catalogo.montar_catalogo(*dados_catalogo()).set_index("sku")
        from pedidos import builder
        assert cat.at["LACO", "colegio"] == builder.SEM_COLEGIO
        assert cat.at["TEN-34", "super_categoria"] == builder.SEM_SUPERCATEGORIA
        assert cat.at["TEN-34", "tamanho_grade"] == "34"          # sufixo do SKU
        assert cat.at["LACO", "tamanho_grade"] == grade.TAMANHO_UNICO
        assert cat.at["LACO", "custo_unit"] == 0                  # custo ausente não vira NaN

    def test_escopo_filtra_pelo_colegio_e_supercategoria_do_pedido(self):
        cat = catalogo.montar_catalogo(*dados_catalogo())
        escopo = catalogo.filtrar_escopo(cat, "NEVES", "CAMISETAS")
        assert set(escopo["sku"]) == {"CAM-P", "CAM-M", "CAM-GG"}
        fam = catalogo.listar_familias(escopo)
        assert fam.to_dict("records") == [
            {"familia": "CAM", "produto_pai": "Camiseta Dry", "n_tamanhos": 3}]

    def test_tamanhos_da_familia_na_ordem_da_grade(self):
        cat = catalogo.montar_catalogo(*dados_catalogo())
        assert catalogo.tamanhos_da_familia(cat, "CAM")["tamanho_grade"].tolist() == \
            ["P", "M", "GG"]

    def test_itens_manuais_so_com_quantidade_e_sem_sugestao(self):
        cat = catalogo.montar_catalogo(*dados_catalogo())
        membros = catalogo.tamanhos_da_familia(cat, "CAM")
        itens = catalogo.montar_itens_manuais(membros, {"CAM-P": 4, "CAM-M": 0})
        assert len(itens) == 1
        assert itens[0] == {
            "sku": "CAM-P", "id_produto_bling": "2", "produto": "Camiseta Dry Tamanho:P",
            "tamanho": "P", "categoria": "CAI", "quantidade_sugerida": 0,
            "quantidade_final": 4, "custo_unit": 20.0, "memoria_sugerida": {},
            "origem": estados.ORIGEM_MANUAL,
        }

    def test_celula_nova_so_vira_item_se_o_tamanho_existe(self):
        cat = catalogo.montar_catalogo(*dados_catalogo())
        itens, faltantes = catalogo.resolver_celulas(cat, [
            {"sku_pai": "CAM", "tamanho": "GG", "quantidade": 6},
            {"sku_pai": "CAM", "tamanho": "XGG", "quantidade": 2},   # não cadastrado
        ])
        assert [(i["sku"], i["quantidade_final"]) for i in itens] == [("CAM-GG", 6)]
        assert faltantes == ["CAM · XGG"]

    def test_catalogo_vazio(self):
        vazio = pd.DataFrame(columns=["ID", "codigo", "Descricao", "preco_custo"])
        assert len(catalogo.montar_catalogo(vazio, dados_catalogo()[1])) == 0


class TestRevendaECadastroSujo:
    """SKUs que não seguem CATEGORIA-TAMANHO não podem virar coluna-lixo nem sumir."""

    def test_sufixo_que_nao_e_tamanho_nao_vira_coluna(self):
        assert grade.parece_tamanho("34") and grade.parece_tamanho("XGG")
        assert not grade.parece_tamanho("0041509960") and not grade.parece_tamanho("089K")
        assert grade.tamanho_efetivo("nan", "00436-0041509960") == grade.TAMANHO_UNICO

    def test_revenda_vira_linha_propria_com_o_nome_completo(self):
        itens = itens_df([
            ("a", "02725-0890130926", "Meia Lupo Calçado:24/29;Cor:Preto", "nan", 3),
            ("b", "02725-0898100926", "Meia Lupo Calçado:30/36;Cor:Preto", "nan", 5),
            ("c", "CAM-M", "Camiseta Dry Tamanho:M", "M", 1),
        ])
        g, celulas = grade.montar_grade(itens)
        assert list(g.columns) == ["SKU", "Produto", "M", grade.TAMANHO_UNICO]
        meias = g[g["SKU"].str.startswith("02725")]
        assert meias["Produto"].tolist() == [
            "Meia Lupo Calçado:24/29;Cor:Preto", "Meia Lupo Calçado:30/36;Cor:Preto"]
        assert meias[grade.TAMANHO_UNICO].tolist() == [3, 5]
        assert len(celulas) == 3

    def test_catalogo_e_grade_dao_o_mesmo_nome_a_mesma_celula(self):
        produtos = pd.DataFrame([
            ("1", "KID-34", "Tenis Kidy NUMERAÇÃO:34", 80.0),
            ("2", "KID-35", "Tenis Kidy NUMERAÇÃO:35", 80.0),
            ("3", "00436-0041509960", "Cueca Lupo Tamanho:46/48;Cor:Preto", 12.0),
        ], columns=["ID", "codigo", "Descricao", "preco_custo"])
        detalhes = pd.DataFrame(columns=["ID_produto", "categoria", "Super_categoria",
                                         "Tamanho", "Marca_sku"])
        cat = catalogo.montar_catalogo(produtos, detalhes).set_index("sku")
        assert (cat.at["KID-35", "familia"], cat.at["KID-35", "tamanho_grade"]) == ("KID", "35")
        assert cat.at["00436-0041509960", "familia"] == "00436-0041509960"

        # pedido só com o 34 → digitar no 35 (coluna de outra linha) acha o SKU certo
        itens, faltantes = catalogo.resolver_celulas(
            cat.reset_index(), [{"sku_pai": "KID", "tamanho": "35", "quantidade": 2}])
        assert [i["sku"] for i in itens] == ["KID-35"] and faltantes == []



# ---------------------------------------------------------------------------
# Destaque do que foi alterado em relação à sugestão
# ---------------------------------------------------------------------------
def itens_com_sugestao(linhas):
    """[(id, sku, tamanho, sugerida, final)] → itens no formato de listar_itens."""
    df = pd.DataFrame(linhas, columns=["id", "sku", "tamanho",
                                       "quantidade_sugerida", "quantidade_final"])
    df["produto"] = "Camiseta Tamanho:" + df["tamanho"]
    return df


class TestAlteradoVsSugerido:
    ITENS = [("a", "CAM-P", "P", 10, 10), ("b", "CAM-M", "M", 20, 24), ("c", "CAM-G", "G", 6, 0)]

    def test_aplicar_edicoes_nao_mexe_no_original(self):
        df = itens_com_sugestao(self.ITENS)
        editado = grade.aplicar_edicoes(df, {0: {"quantidade_final": 12}})
        assert editado.loc[0, "quantidade_final"] == 12
        assert df.loc[0, "quantidade_final"] == 10

    def test_sem_edicao_devolve_igual(self):
        df = itens_com_sugestao(self.ITENS)
        pd.testing.assert_frame_equal(grade.aplicar_edicoes(df, {}), df)
        pd.testing.assert_frame_equal(grade.aplicar_edicoes(df, None), df)

    def test_celula_apagada_vira_vazio_e_conta_zero(self):
        df = itens_com_sugestao(self.ITENS)
        editado = grade.aplicar_edicoes(df, {1: {"quantidade_final": None}})
        assert pd.isna(editado.loc[1, "quantidade_final"])
        assert grade._qtd(editado.loc[1, "quantidade_final"]) == 0

    def test_edicao_fora_da_tabela_e_ignorada(self):
        # estado de widget antigo (linha/coluna que não existe mais) não pode quebrar a tela
        df = itens_com_sugestao(self.ITENS)
        editado = grade.aplicar_edicoes(df, {9: {"quantidade_final": 1}, 0: {"sumiu": 1}})
        pd.testing.assert_frame_equal(editado, df)

    def test_aplicar_edicoes_na_grade_reproduz_o_que_o_editor_devolve(self):
        itens = itens_com_sugestao(self.ITENS)
        df_grade, celulas = grade.montar_grade(itens)
        previsto = grade.aplicar_edicoes(df_grade, {0: {"P": 15, "G": None}})
        por_item = grade.quantidades_por_item(previsto, celulas)
        assert por_item == {"a": 15, "b": 24, "c": 0}

    def test_divergencias_so_o_que_difere_da_sugestao(self):
        itens = itens_com_sugestao(self.ITENS)
        salvo = dict(zip(itens["id"], itens["quantidade_final"]))
        assert grade.divergencias(itens, salvo) == [
            {"id": "b", "sku": "CAM-M", "tamanho": "M", "sugerida": 20, "final": 24},
            {"id": "c", "sku": "CAM-G", "tamanho": "G", "sugerida": 6, "final": 0},
        ]

    def test_voltar_ao_sugerido_tira_da_lista(self):
        itens = itens_com_sugestao(self.ITENS)
        assert grade.divergencias(itens, {"a": 10, "b": 20, "c": 6}) == []

    def test_item_manual_sempre_diverge(self):
        itens = itens_com_sugestao([("m", "CAM-GG", "GG", 0, 4)])
        assert [d["id"] for d in grade.divergencias(itens, {"m": 4})] == ["m"]

    def test_com_base_a_referencia_e_o_emitido_nao_a_sugestao(self):
        """Alteração pós-emissão: importa o que difere do que está nos ERPs."""
        itens = itens_com_sugestao(self.ITENS)          # b: sugerida 20, final 24
        emitido = {"a": 10, "b": 24, "c": 0}
        assert grade.divergencias(itens, emitido, base=emitido) == []   # nada mudou
        assert grade.divergencias(itens, {"a": 10, "b": 30, "c": 0}, base=emitido) == [
            {"id": "b", "sku": "CAM-M", "tamanho": "M", "sugerida": 24, "final": 30}]

    def test_item_fora_da_base_foi_incluido_na_alteracao(self):
        itens = itens_com_sugestao(self.ITENS)
        difs = grade.divergencias(itens, {"a": 10, "b": 24, "c": 2}, base={"a": 10, "b": 24})
        assert [(d["id"], d["sugerida"], d["final"]) for d in difs] == [("c", 0, 2)]

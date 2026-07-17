"""
Testes do builder de pedidos de compra (pedidos/builder.py).

Duas frentes:
  - DataFrames sintéticos no formato do processar_fabrica → controle total
    sobre agrupamento, normalização e formatos.
  - Pipeline real (fixtures do conftest → processar_fabrica → snapshot) →
    garante que o payload congelado é JSON-serializável de ponta a ponta
    (NaN, tipos numpy, datas do config).
"""

import json

import pandas as pd
import pytest

from etl.fabrica import processar_fabrica
from etl import demanda
from pedidos import builder


HOJE = pd.Timestamp("2026-07-15")


def cfg_rodadas():
    """Calendário explícito fixo: rodada 1 dispara Ago/2026 (chega 29/08)."""
    return {"planejamento": {
        "rodadas_datas": ["2026-08-01", "2026-11-01", "2027-02-01"],
        "lead_time_semanas": 4,
    }, "fabrica": {"cobertura_meses": 2}}


def df_fabrica(linhas):
    """DataFrame sintético com as colunas do processar_fabrica usadas pelo builder."""
    cols = ["SKU", "Produto", "Tamanho", "Categoria", "SuperCategoria",
            "Colegio", "SugestaoProducao", "CustoUnit", "JanelaLabel"]
    return pd.DataFrame(linhas, columns=cols)


def produtos_map(pares):
    """produtos mínimo p/ mapear SKU → ID Bling."""
    return pd.DataFrame({"ID": [p[1] for p in pares], "codigo": [p[0] for p in pares]})


# ---------------------------------------------------------------------------
# identidade_rodada — resolve datas absolutas a partir do calendário
# ---------------------------------------------------------------------------
class TestIdentidadeRodada:
    def test_resolve_datas_absolutas(self):
        ident = builder.identidade_rodada(cfg_rodadas(), 8, 2026, HOJE)
        assert ident["data_disparo"] == pd.Timestamp("2026-08-01")
        assert ident["data_chegada"] == pd.Timestamp("2026-08-29")     # +28 dias
        assert ident["data_chegada_seguinte"] == pd.Timestamp("2026-11-29")
        assert ident["rodada_numero"] == 1

    def test_rodada_inexistente_levanta_valueerror(self):
        with pytest.raises(ValueError, match="não encontrada"):
            builder.identidade_rodada(cfg_rodadas(), 1, 2000, HOJE)

    def test_sem_calendario_levanta_valueerror(self):
        cfg = {"planejamento": {"rodadas_datas": [], "lead_time_semanas": 4}}
        with pytest.raises(ValueError):
            builder.identidade_rodada(cfg, 8, 2026, HOJE)


# ---------------------------------------------------------------------------
# agrupar_pedidos — divisão Colégio × SuperCategoria
# ---------------------------------------------------------------------------
class TestAgruparPedidos:
    def test_agrupa_por_colegio_e_supercategoria(self):
        df = df_fabrica([
            ("CAL-P", "Calça P", "P", "Calça", "CALÇAS", "NEVES", 10, 50.0, "R1"),
            ("CAL-M", "Calça M", "M", "Calça", "CALÇAS", "NEVES", 6, 50.0, "R1"),
            ("CAM-P", "Camisa P", "P", "Camisa", "CAMISETAS", "NEVES", 4, 20.0, "R1"),
            ("CAL-G", "Calça G", "G", "Calça", "CALÇAS", "LMN", 2, 50.0, "R1"),
        ])
        produtos = produtos_map([("CAL-P", "1"), ("CAL-M", "2"), ("CAM-P", "3"), ("CAL-G", "4")])
        grupos = builder.agrupar_pedidos(df, produtos, "tester", 8, 2026)

        chaves = {(g["colegio"], g["super_categoria"]) for g in grupos}
        assert chaves == {("NEVES", "CALÇAS"), ("NEVES", "CAMISETAS"), ("LMN", "CALÇAS")}
        calcas_neves = next(g for g in grupos
                            if (g["colegio"], g["super_categoria"]) == ("NEVES", "CALÇAS"))
        assert len(calcas_neves["itens"]) == 2

    def test_exclui_sugestao_zero(self):
        df = df_fabrica([
            ("A", "A", "P", "c", "SC", "COL", 10, 1.0, "R1"),
            ("B", "B", "P", "c", "SC", "COL", 0, 1.0, "R1"),
        ])
        grupos = builder.agrupar_pedidos(df, produtos_map([("A", "1"), ("B", "2")]),
                                         "tester", 8, 2026)
        assert len(grupos) == 1
        assert [i["sku"] for i in grupos[0]["itens"]] == ["A"]

    def test_final_igual_sugerida_e_id_mapeado(self):
        df = df_fabrica([("A-PP", "Prod A", "PP", "Camisa", "CAMISETAS", "COL", 8, 12.5, "R1")])
        grupos = builder.agrupar_pedidos(df, produtos_map([("A-PP", "101")]), "tester", 8, 2026)
        item = grupos[0]["itens"][0]
        assert item["quantidade_final"] == item["quantidade_sugerida"] == 8
        assert item["id_produto_bling"] == "101"
        assert item["custo_unit"] == 12.5

    def test_normaliza_dimensoes_vazias(self):
        df = df_fabrica([
            ("A", "A", "P", "c", "", "", 4, 1.0, "R1"),
            ("B", "B", "P", "c", float("nan"), "nan", 2, 1.0, "R1"),
        ])
        grupos = builder.agrupar_pedidos(df, produtos_map([("A", "1"), ("B", "2")]),
                                         "tester", 8, 2026)
        assert len(grupos) == 1   # ambos caem no mesmo grupo "(sem ...)"
        assert grupos[0]["colegio"] == builder.SEM_COLEGIO
        assert grupos[0]["super_categoria"] == builder.SEM_SUPERCATEGORIA

    def test_titulo_padronizado(self):
        df = df_fabrica([("A", "A", "P", "c", "Calças", "Neves", 4, 1.0, "R1")])
        grupos = builder.agrupar_pedidos(df, produtos_map([("A", "1")]), "tester", 8, 2026)
        assert grupos[0]["titulo"] == "AKU-PC · NEVES · CALÇAS · R08/2026"

    def test_nada_para_produzir_lista_vazia(self):
        df = df_fabrica([("A", "A", "P", "c", "SC", "COL", 0, 1.0, "R1")])
        assert builder.agrupar_pedidos(df, produtos_map([("A", "1")]), "t", 8, 2026) == []

    def test_memoria_sugerida_extrai_drivers(self):
        """Item carrega a memória curada da sugestão (drivers order-up-to),
        em tipos nativos JSON-serializáveis (sem numpy)."""
        df = pd.DataFrame([{
            "SKU": "A", "Produto": "Prod A", "Tamanho": "P", "Categoria": "Calça",
            "SuperCategoria": "CALÇAS", "Colegio": "NEVES", "SugestaoProducao": 12,
            "CustoUnit": 50.0, "JanelaLabel": "R1",
            "VendasHist": 30, "DemandaProjetada": 40.0, "DemandaPeriodoAlta": 33.0,
            "DemandaPeriodoBaixa": 7.0, "EstoqueRede": 10, "Backlog": 2,
            "EstoqueSeguranca": 6.0, "EstoqueMeta": 46, "EstoqueProjetado": 8.0,
            "NivelServico": 99,   # pontos percentuais (99, 92…), não fração
        }])
        grupos = builder.agrupar_pedidos(df, produtos_map([("A", "1")]), "t", 8, 2026)
        mem = grupos[0]["itens"][0]["memoria_sugerida"]
        assert mem["demanda_periodo"] == 40.0
        assert mem["estoque_meta"] == 46.0
        assert mem["estoque_projetado"] == 8.0
        assert mem["nivel_servico"] == 99.0
        assert mem["janela_label"] == "R1"
        json.dumps(mem)   # nativo, sem numpy

    def test_memoria_sugerida_tolera_colunas_ausentes(self):
        """DataFrame parcial (sem as colunas de driver) → memória com None,
        ainda serializável (não quebra o congelamento)."""
        df = df_fabrica([("A", "A", "P", "c", "SC", "COL", 4, 1.0, "R1")])
        grupos = builder.agrupar_pedidos(df, produtos_map([("A", "1")]), "t", 8, 2026)
        mem = grupos[0]["itens"][0]["memoria_sugerida"]
        assert mem["demanda_periodo"] is None
        assert mem["janela_label"] == "R1"   # essa coluna existe no sintético
        json.dumps(mem)


# ---------------------------------------------------------------------------
# validar_pre_congelamento
# ---------------------------------------------------------------------------
class TestValidarPreCongelamento:
    def test_ok(self):
        df = df_fabrica([("A", "A", "P", "c", "SC", "COL", 4, 1.0, "Rodada 1")])
        assert builder.validar_pre_congelamento(df) == []

    def test_df_vazio(self):
        erros = builder.validar_pre_congelamento(df_fabrica([]))
        assert any("vazia" in e for e in erros)

    def test_tudo_zero(self):
        df = df_fabrica([("A", "A", "P", "c", "SC", "COL", 0, 1.0, "R1")])
        erros = builder.validar_pre_congelamento(df)
        assert any("sugestão de produção > 0" in e for e in erros)

    def test_sku_duplicado(self):
        df = df_fabrica([
            ("A", "A", "P", "c", "SC", "COL", 4, 1.0, "R1"),
            ("A", "A2", "P", "c", "SC", "COL", 2, 1.0, "R1"),
        ])
        erros = builder.validar_pre_congelamento(df)
        assert any("duplicados" in e for e in erros)

    def test_janela_ausente(self):
        df = df_fabrica([("A", "A", "P", "c", "SC", "COL", 4, 1.0, "")])
        erros = builder.validar_pre_congelamento(df)
        assert any("JanelaLabel" in e for e in erros)


# ---------------------------------------------------------------------------
# montar_snapshot_rodada — sintético e pipeline real
# ---------------------------------------------------------------------------
class TestSnapshot:
    def test_identidade_e_serializacao(self):
        df = df_fabrica([("A", "A", "P", "c", "SC", "COL", 4, 1.0, "Rodada 1 — disparo Ago/2026")])
        snap = builder.montar_snapshot_rodada(
            df, cfg_rodadas(), 8, 2026, ativo_crescimento=True,
            usuario="diretoria", data_referencia=HOJE,
        )
        assert snap["mes_disparo"] == 8 and snap["ano_disparo"] == 2026
        assert snap["data_disparo"] == "2026-08-01"
        assert snap["data_chegada"] == "2026-08-29"
        assert snap["data_chegada_seguinte"] == "2026-11-29"
        assert snap["data_referencia"] == "2026-07-15"
        assert snap["congelada_por"] == "diretoria"
        assert snap["ativo_crescimento"] is True
        assert snap["janela_label"] == "Rodada 1 — disparo Ago/2026"
        # config completo embarcado
        assert "planejamento" in snap["config_snapshot"]
        # snapshot integral: mantém inclusive linhas com sugestão 0
        assert len(snap["resultado_skus"]) == len(df)
        json.dumps(snap)   # payload inteiro JSON-serializável

    def test_pipeline_real_processar_fabrica(self, dados, config, hoje):
        """O teste mais importante: fixtures → processar_fabrica → snapshot
        → json.dumps sem erro (NaN, numpy int64/float64, datas do config)."""
        # Calendário relativo a hoje (3 datas → 2 rodadas); fixtures não têm
        # Tamanho/Descricao (o motor de demanda não usa, o fabrica sim)
        config["planejamento"]["rodadas_datas"] = [
            (hoje + pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in (30, 120, 210)
        ]
        dados["detalhes"] = dados["detalhes"].assign(Tamanho="PP")
        dados["produtos"] = dados["produtos"].assign(
            Descricao=dados["produtos"]["codigo"])

        rodada1 = demanda.sequencia_rodadas(config, hoje)[0]
        df = processar_fabrica(dados, config,
                               mes_disparo=rodada1["mes_disparo"],
                               ano_disparo=rodada1["ano_disparo"])
        snap = builder.montar_snapshot_rodada(
            df, config, rodada1["mes_disparo"], rodada1["ano_disparo"],
            ativo_crescimento=False, usuario="tester", data_referencia=hoje,
        )
        json.dumps(snap)   # não pode levantar TypeError
        assert len(snap["resultado_skus"]) == len(df) == len(dados["produtos"])
        assert snap["data_disparo"] == str(rodada1["data_disparo"].date())


# ---------------------------------------------------------------------------
# Observações internas do Bling — padrão de escrita
# ---------------------------------------------------------------------------
class TestObservacoesBling:
    def _base(self):
        rodada = {
            "mes_disparo": 8, "ano_disparo": 2026,
            "data_chegada": "2026-08-29", "data_chegada_seguinte": "2026-11-29",
            "congelada_em": "2026-07-12T10:00:00+00:00", "congelada_por": "diretoria",
        }
        pedido = {"id": "abc-123", "titulo": "AKU-PC · NEVES · CALÇAS · R08/2026"}
        itens = pd.DataFrame({
            "quantidade_final": [10, 0, 4],
            "custo_unit": [10.0, 5.0, 2.5],
        })
        return rodada, pedido, itens

    def test_formato_e_totais_pela_quantidade_final(self):
        rodada, pedido, itens = self._base()
        texto = builder.montar_observacoes_bling(rodada, pedido, itens)
        linhas = texto.split("\n")
        assert linhas[0] == "AKU-PC · NEVES · CALÇAS · R08/2026"
        # item com quantidade_final = 0 fica fora dos totais
        assert "2 SKUs · 14 pares" in linhas[2]
        assert "R$ 110,00" in linhas[2]        # 10×10 + 4×2,5
        assert "ref: abc-123" in linhas[3]
        assert "congelada em 12/07/2026 por diretoria" in linhas[3]

    def test_deterministico(self):
        rodada, pedido, itens = self._base()
        assert (builder.montar_observacoes_bling(rodada, pedido, itens)
                == builder.montar_observacoes_bling(rodada, pedido, itens))

    def test_titulo_padrao(self):
        assert (builder.montar_titulo("Neves", "calças", 8, 2026)
                == "AKU-PC · NEVES · CALÇAS · R08/2026")

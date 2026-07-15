"""
Testes do config_store (persistência de parâmetros no Supabase).

deep_merge/extrair_parametros são puros; RepositorioParametros é testado com
fake do gateway (mesmo padrão do test_pedidos_repositorio). O que exige o
banco real (grants, DDL) fica na verificação manual/seed.
"""

import pytest

from etl.config_store import (
    RepositorioParametros, deep_merge, extrair_parametros,
    TAB_PARAMETROS, TAB_HISTORICO, ID_LINHA,
)


# ---------------------------------------------------------------------------
# deep_merge — semântica do merge yaml ← Supabase
# ---------------------------------------------------------------------------
class TestDeepMerge:
    def test_escalar_do_override_vence(self):
        out = deep_merge({"vm": {"lead_time": 3, "vm_minimo": 2}},
                         {"vm": {"lead_time": 7}})
        assert out["vm"]["lead_time"] == 7
        assert out["vm"]["vm_minimo"] == 2      # default preservado

    def test_chave_nova_no_yaml_sobrevive(self):
        # parâmetro novo introduzido no yaml não some se o Supabase não o tem
        out = deep_merge({"demanda": {"novo_param": 1.5, "variacao_demanda": 0.25}},
                         {"demanda": {"variacao_demanda": 0.30}})
        assert out["demanda"]["novo_param"] == 1.5
        assert out["demanda"]["variacao_demanda"] == 0.30

    def test_lista_substitui_inteira(self):
        out = deep_merge({"demanda": {"janela_alta": [12, 1, 2]}},
                         {"demanda": {"janela_alta": [11, 12, 1]}})
        assert out["demanda"]["janela_alta"] == [11, 12, 1]

    def test_colecao_substitui_bloco_inteiro(self):
        # colegios está em CAMINHOS_SUBSTITUICAO: item apagado na UI NÃO
        # ressuscita do default do yaml
        out = deep_merge({"colegios": {"ADC": {"taxa_crescimento": 1.1}}},
                         {"colegios": {"NEV": {"taxa_crescimento": 1.3}}})
        assert out["colegios"] == {"NEV": {"taxa_crescimento": 1.3}}

    def test_cobertura_override_substitui(self):
        out = deep_merge(
            {"planejamento": {"lead_time_semanas": 4,
                              "cobertura_override": {"2026-07-01": 0.2}}},
            {"planejamento": {"cobertura_override": {"2026-10-01": 0.4}}})
        assert out["planejamento"]["cobertura_override"] == {"2026-10-01": 0.4}
        assert out["planejamento"]["lead_time_semanas"] == 4

    def test_nao_muta_argumentos(self):
        base = {"vm": {"lead_time": 3}}
        deep_merge(base, {"vm": {"lead_time": 9}})
        assert base["vm"]["lead_time"] == 3

    def test_override_vazio_devolve_base(self):
        base = {"vm": {"lead_time": 3}}
        assert deep_merge(base, {}) == base
        assert deep_merge(base, None) == base


# ---------------------------------------------------------------------------
# extrair_parametros — recorte da Categoria B
# ---------------------------------------------------------------------------
class TestExtrairParametros:
    def _config(self):
        return {
            "fonte": {"nome": "X"},                                # Categoria A
            "depositos": {"central": {"deposito_id": "1"}},        # Categoria A
            "daily": {"situacoes_venda": [9],                      # A
                      "metas": {"Natal": 1.0},                     # B
                      "status_ids": {"em_aberto": 6}},             # B
            "fabrica": {"crescimento_pct": 10.0,                   # B
                        "situacoes_backlog": [6, 15]},             # A
            "demanda": {"min_vendas_colegio": 30,                  # A
                        "janela_alta": [12, 1, 2]},                # B
            "planejamento": {"rodadas_datas": ["2026-07-01"],
                             "cobertura_override": {"2026-07-01": 0.4}},
            "colegios": {"NEV": {"taxa_crescimento": 1.2}},
            "excecoes_sku": {},
        }

    def test_categoria_a_fica_fora(self):
        dados = extrair_parametros(self._config())
        assert "fonte" not in dados
        assert "depositos" not in dados
        assert "situacoes_venda" not in dados.get("daily", {})
        assert "situacoes_backlog" not in dados.get("fabrica", {})
        assert "min_vendas_colegio" not in dados.get("demanda", {})

    def test_categoria_b_entra(self):
        dados = extrair_parametros(self._config())
        assert dados["daily"]["metas"] == {"Natal": 1.0}
        assert dados["fabrica"]["crescimento_pct"] == 10.0
        assert dados["demanda"]["janela_alta"] == [12, 1, 2]
        assert dados["planejamento"]["cobertura_override"] == {"2026-07-01": 0.4}
        assert dados["colegios"] == {"NEV": {"taxa_crescimento": 1.2}}

    def test_roundtrip_extrai_mescla_e_identidade(self):
        # extrair + mesclar de volta sobre o mesmo config = mesmo config
        cfg = self._config()
        assert deep_merge(cfg, extrair_parametros(cfg)) == cfg

    def test_bloco_ausente_nao_quebra(self):
        assert extrair_parametros({"daily": {"metas": {"Natal": 1.0}}}) == {
            "daily": {"metas": {"Natal": 1.0}}}


# ---------------------------------------------------------------------------
# RepositorioParametros — gateway fake
# ---------------------------------------------------------------------------
class RepoFake(RepositorioParametros):
    def __init__(self):
        super().__init__(client=None)
        self.parametros = {"id": ID_LINHA, "dados": {}}
        self.historico = []
        self.chamadas = []

    def _selecionar(self, tabela, filtros, colunas="*"):
        self.chamadas.append(("selecionar", tabela))
        if tabela == TAB_PARAMETROS and filtros.get("id") == ID_LINHA:
            return [dict(self.parametros)]
        return []

    def _atualizar(self, tabela, filtros, valores):
        self.chamadas.append(("atualizar", tabela))
        assert tabela == TAB_PARAMETROS and filtros == {"id": ID_LINHA}
        self.parametros.update(valores)
        return [dict(self.parametros)]

    def _inserir(self, tabela, linha):
        self.chamadas.append(("inserir", tabela))
        assert tabela == TAB_HISTORICO
        self.historico.append(dict(linha))
        return [dict(linha)]


class TestRepositorioParametros:
    def test_ler_vazio(self):
        repo = RepoFake()
        assert repo.ler() == {}

    def test_salvar_grava_estado_e_historico(self):
        repo = RepoFake()
        repo.salvar({"vm": {"lead_time": 5}}, usuario="diogo")
        assert repo.ler() == {"vm": {"lead_time": 5}}
        assert len(repo.historico) == 1
        assert repo.historico[0]["criado_por"] == "diogo"
        assert repo.historico[0]["dados"] == {"vm": {"lead_time": 5}}
        assert repo.parametros["atualizado_por"] == "diogo"

    def test_salvar_ordem_update_depois_insert(self):
        # estado vivo primeiro; se a auditoria falhar, o save não se perde
        repo = RepoFake()
        repo.salvar({"vm": {}}, usuario="u")
        ops = [c for c in repo.chamadas if c[0] != "selecionar"]
        assert ops == [("atualizar", TAB_PARAMETROS), ("inserir", TAB_HISTORICO)]

    def test_salvar_normaliza_tipos_nao_json(self):
        import pandas as pd
        repo = RepoFake()
        repo.salvar({"planejamento": {"periodo_historico_fim":
                                      pd.Timestamp("2026-01-31")}}, usuario="u")
        v = repo.ler()["planejamento"]["periodo_historico_fim"]
        assert isinstance(v, str) and v.startswith("2026-01-31")

    def test_ler_metadados(self):
        repo = RepoFake()
        repo.salvar({}, usuario="diogo")
        meta = repo.ler_metadados()
        assert meta["atualizado_por"] == "diogo"

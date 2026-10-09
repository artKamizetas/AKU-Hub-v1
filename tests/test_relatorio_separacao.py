"""Testes do relatório de separação impresso (etl/relatorio_separacao.py)."""

import pandas as pd

from etl import relatorio_separacao as rs


def _fila():
    return pd.DataFrame([
        # loja, colégio, modelo, tamanho, produto, separar
        ("Natal", "NEV", "NEV011CAM", "G", "Neves - Camisa Tamanho:G", 4),
        ("Natal", "NEV", "NEV011CAM", "PP", "Neves - Camisa Tamanho:PP", 2),
        ("Natal", "NEV", "NEV011CAM", "M", "Neves - Camisa Tamanho:M", 3),
        ("Natal", "NEV", "NEV020CAL", "10", "Neves - Calça Tamanho:10", 1),
        ("Natal", "NEV", "NEV020CAL", "02", "Neves - Calça Tamanho:02", 5),
        ("Natal", "ADC", "ADC001CAM", "M", "ADC - Camisa Tamanho:M", 6),
        ("Natal", "ADC", "ADC001CAM", "G", "ADC - Camisa Tamanho:G", 0),     # nada a separar
        ("Mossoró", "SES", "SES015CAM", "P", "Sesi <b>Camisa</b> Tamanho:P", 7),
    ], columns=["Loja", "Colegio", "Modelo", "Tamanho", "Produto", "Separar"])


def test_uma_linha_por_modelo_com_grade_ordenada():
    linhas = rs.montar_linhas(_fila())
    camisa = next(l for l in linhas if l["modelo"] == "NEV011CAM")
    assert camisa["tamanhos"] == [("PP", 2), ("M", 3), ("G", 4)]     # ordem da confecção
    assert camisa["total"] == 9
    assert camisa["produto"] == "Neves - Camisa"
    calca = next(l for l in linhas if l["modelo"] == "NEV020CAL")
    assert calca["tamanhos"] == [("02", 5), ("10", 1)]               # número pelo valor


def test_so_entra_o_que_tem_separar():
    linhas = rs.montar_linhas(_fila())
    adc = next(l for l in linhas if l["modelo"] == "ADC001CAM")
    assert adc["tamanhos"] == [("M", 6)]
    assert sum(l["total"] for l in linhas) == 28


def test_ordem_loja_colegio_modelo():
    chaves = [(l["loja"], l["colegio"], l["modelo"]) for l in rs.montar_linhas(_fila())]
    assert chaves == sorted(chaves)


def test_fila_vazia():
    assert rs.montar_linhas(pd.DataFrame()) == []
    assert rs.montar_linhas(None) == []
    assert "Nada a separar" in rs.montar_html(pd.DataFrame(), data="2026-10-09 08:00")


def test_html_tem_uma_secao_por_loja_e_os_totais():
    doc = rs.montar_html(_fila(), data="2026-10-09 08:00")
    assert doc.count("<section>") == 2
    assert "Separação — Natal" in doc and "Separação — Mossoró" in doc
    assert "<b>21</b> peças · 3 modelos" in doc          # Natal
    assert "09/10/2026 08:00" in doc
    assert "window.print" not in doc


def test_html_escapa_o_texto_do_cadastro():
    doc = rs.montar_html(_fila(), data="2026-10-09")
    assert "<b>Camisa</b>" not in doc
    assert "&lt;b&gt;Camisa&lt;/b&gt;" in doc


def test_imprimir_ao_abrir_injeta_o_script():
    assert "window.print" in rs.montar_html(_fila(), imprimir_ao_abrir=True)

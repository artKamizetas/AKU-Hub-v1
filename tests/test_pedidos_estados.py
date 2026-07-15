"""
Testes da máquina de estados do Pedido de Compra (pedidos/estados.py).
Tabela de verdade pura — sem I/O. Emissão em DOIS momentos: compra (Bling)
primeiro, venda (Olist) depois; *_EMITINDO são locks CAS transientes.
"""

import pytest

from pedidos import estados as e


class TestTransicoes:
    @pytest.mark.parametrize("de,para", [
        (e.RASCUNHO, e.PRONTO),
        (e.RASCUNHO, e.CANCELADO),
        (e.PRONTO, e.RASCUNHO),                    # reabrir
        (e.PRONTO, e.COMPRA_EMITINDO),             # lock da emissão da compra
        (e.PRONTO, e.CANCELADO),
        (e.COMPRA_EMITINDO, e.COMPRA_EMITIDA),     # compra criada no Bling
        (e.COMPRA_EMITINDO, e.PRONTO),             # falha/destravar → rollback
        (e.COMPRA_EMITIDA, e.VENDA_EMITINDO),      # lock da emissão da venda
        (e.VENDA_EMITINDO, e.EMITIDO),             # venda criada no Olist
        (e.VENDA_EMITINDO, e.COMPRA_EMITIDA),      # falha/destravar → rollback
        (e.EMITIDO, e.SINCRONIZADO),               # reservado (sincronizador)
    ])
    def test_transicoes_validas(self, de, para):
        assert e.pode_transicionar(de, para)

    @pytest.mark.parametrize("de,para", [
        (e.RASCUNHO, e.COMPRA_EMITINDO),   # não pula o PRONTO
        (e.RASCUNHO, e.EMITIDO),
        (e.PRONTO, e.COMPRA_EMITIDA),      # não pula o lock
        (e.PRONTO, e.VENDA_EMITINDO),      # venda não vem antes da compra
        (e.PRONTO, e.EMITIDO),
        (e.COMPRA_EMITIDA, e.EMITIDO),     # não pula o lock da venda
        (e.COMPRA_EMITIDA, e.PRONTO),      # compra já existe no Bling — não reabre
        (e.COMPRA_EMITIDA, e.CANCELADO),   # cancelamento pós-emissão é no ERP
        (e.COMPRA_EMITIDA, e.RASCUNHO),
        (e.EMITIDO, e.RASCUNHO),
        (e.EMITIDO, e.CANCELADO),
        (e.CANCELADO, e.RASCUNHO),         # terminal
        (e.SINCRONIZADO, e.PRONTO),        # terminal
        (e.RASCUNHO, e.RASCUNHO),          # auto-transição não existe
    ])
    def test_transicoes_invalidas(self, de, para):
        assert not e.pode_transicionar(de, para)

    def test_estado_desconhecido_nunca_transiciona(self):
        assert not e.pode_transicionar("INEXISTENTE", e.PRONTO)
        assert not e.pode_transicionar(e.RASCUNHO, "INEXISTENTE")
        assert not e.pode_transicionar("EMITINDO", e.EMITIDO)   # estado antigo removido


class TestEditavel:
    def test_so_rascunho_e_editavel(self):
        assert e.editavel(e.RASCUNHO)
        for status in (e.PRONTO, e.COMPRA_EMITINDO, e.COMPRA_EMITIDA,
                       e.VENDA_EMITINDO, e.EMITIDO, e.SINCRONIZADO, e.CANCELADO):
            assert not e.editavel(status)


class TestEmitindo:
    def test_locks_transientes(self):
        assert e.emitindo(e.COMPRA_EMITINDO)
        assert e.emitindo(e.VENDA_EMITINDO)
        for status in (e.RASCUNHO, e.PRONTO, e.COMPRA_EMITIDA,
                       e.EMITIDO, e.SINCRONIZADO, e.CANCELADO):
            assert not e.emitindo(status)


class TestConsistencia:
    def test_todo_estado_tem_badge(self):
        for status in e.TRANSICOES:
            assert status in e.ROTULOS_BADGE

    def test_destinos_sao_estados_conhecidos(self):
        conhecidos = set(e.TRANSICOES)
        for destinos in e.TRANSICOES.values():
            assert destinos <= conhecidos

    def test_locks_tem_rollback_e_commit(self):
        # Todo lock transiente precisa de exatamente 1 caminho de avanço e 1 de volta
        assert e.TRANSICOES[e.COMPRA_EMITINDO] == {e.COMPRA_EMITIDA, e.PRONTO}
        assert e.TRANSICOES[e.VENDA_EMITINDO] == {e.EMITIDO, e.COMPRA_EMITIDA}

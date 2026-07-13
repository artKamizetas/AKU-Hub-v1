"""
Testes da máquina de estados do Pedido de Compra (pedidos/estados.py).
Tabela de verdade pura — sem I/O.
"""

import pytest

from pedidos import estados as e


class TestTransicoes:
    @pytest.mark.parametrize("de,para", [
        (e.RASCUNHO, e.PRONTO),
        (e.RASCUNHO, e.CANCELADO),
        (e.PRONTO, e.RASCUNHO),      # reabrir
        (e.PRONTO, e.EMITINDO),      # reservado (fase futura)
        (e.PRONTO, e.CANCELADO),
        (e.EMITINDO, e.EMITIDO),
        (e.EMITINDO, e.PRONTO),      # falha de emissão volta p/ PRONTO
        (e.EMITIDO, e.SINCRONIZADO),
    ])
    def test_transicoes_validas(self, de, para):
        assert e.pode_transicionar(de, para)

    @pytest.mark.parametrize("de,para", [
        (e.RASCUNHO, e.EMITINDO),    # não pula o PRONTO
        (e.RASCUNHO, e.EMITIDO),
        (e.PRONTO, e.EMITIDO),       # não pula o EMITINDO
        (e.EMITIDO, e.RASCUNHO),     # emitido não reabre
        (e.EMITIDO, e.CANCELADO),    # cancelamento pós-emissão é no Bling, não aqui
        (e.CANCELADO, e.RASCUNHO),   # terminal
        (e.SINCRONIZADO, e.PRONTO),  # terminal
        (e.RASCUNHO, e.RASCUNHO),    # auto-transição não existe
    ])
    def test_transicoes_invalidas(self, de, para):
        assert not e.pode_transicionar(de, para)

    def test_estado_desconhecido_nunca_transiciona(self):
        assert not e.pode_transicionar("INEXISTENTE", e.PRONTO)
        assert not e.pode_transicionar(e.RASCUNHO, "INEXISTENTE")


class TestEditavel:
    def test_so_rascunho_e_editavel(self):
        assert e.editavel(e.RASCUNHO)
        for status in (e.PRONTO, e.EMITINDO, e.EMITIDO, e.SINCRONIZADO, e.CANCELADO):
            assert not e.editavel(status)


class TestConsistencia:
    def test_todo_estado_tem_badge(self):
        for status in e.TRANSICOES:
            assert status in e.ROTULOS_BADGE

    def test_destinos_sao_estados_conhecidos(self):
        conhecidos = set(e.TRANSICOES)
        for destinos in e.TRANSICOES.values():
            assert destinos <= conhecidos

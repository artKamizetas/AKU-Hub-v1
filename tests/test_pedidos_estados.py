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
        # --- pós-emissão: alterar ---
        (e.EMITIDO, e.EM_ALTERACAO),
        (e.COMPRA_EMITIDA, e.EM_ALTERACAO),        # vale para quem só emitiu a compra
        (e.EM_ALTERACAO, e.ALTERACAO_ENVIANDO),    # lock do envio
        (e.EM_ALTERACAO, e.EMITIDO),               # descartar
        (e.EM_ALTERACAO, e.COMPRA_EMITIDA),        # descartar (sem venda)
        (e.ALTERACAO_ENVIANDO, e.EMITIDO),         # enviado
        (e.ALTERACAO_ENVIANDO, e.COMPRA_EMITIDA),
        (e.ALTERACAO_ENVIANDO, e.EM_ALTERACAO),    # falha antes do ERP / voltar a editar
        # --- pós-emissão: cancelar ---
        (e.EMITIDO, e.CANCELAMENTO_ENVIANDO),
        (e.COMPRA_EMITIDA, e.CANCELAMENTO_ENVIANDO),
        (e.CANCELAMENTO_ENVIANDO, e.CANCELADO),
        (e.CANCELAMENTO_ENVIANDO, e.EMITIDO),      # falha antes do ERP / destravar
        (e.CANCELAMENTO_ENVIANDO, e.COMPRA_EMITIDA),
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
        (e.COMPRA_EMITIDA, e.CANCELADO),   # cancelar emitido só passando pelo lock
        (e.COMPRA_EMITIDA, e.RASCUNHO),
        (e.EMITIDO, e.RASCUNHO),           # RASCUNHO = "não existe nos ERPs"
        (e.EMITIDO, e.CANCELADO),
        (e.EMITIDO, e.ALTERACAO_ENVIANDO),         # não envia sem abrir a alteração
        (e.EM_ALTERACAO, e.RASCUNHO),
        (e.EM_ALTERACAO, e.CANCELADO),             # descarta primeiro, cancela depois
        (e.EM_ALTERACAO, e.CANCELAMENTO_ENVIANDO),
        (e.ALTERACAO_ENVIANDO, e.CANCELADO),
        (e.CANCELAMENTO_ENVIANDO, e.EM_ALTERACAO),
        (e.PRONTO, e.EM_ALTERACAO),                # antes de emitir é "reabrir rascunho"
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
    def test_so_rascunho_e_alteracao_sao_editaveis(self):
        assert e.editavel(e.RASCUNHO)
        assert e.editavel(e.EM_ALTERACAO)
        for status in (e.PRONTO, e.COMPRA_EMITINDO, e.COMPRA_EMITIDA,
                       e.VENDA_EMITINDO, e.EMITIDO, e.ALTERACAO_ENVIANDO,
                       e.CANCELAMENTO_ENVIANDO, e.SINCRONIZADO, e.CANCELADO):
            assert not e.editavel(status)


class TestPosEmissao:
    def test_so_pedido_emitido_em_repouso_e_alteravel(self):
        assert e.alteravel(e.COMPRA_EMITIDA) and e.alteravel(e.EMITIDO)
        for status in (e.RASCUNHO, e.PRONTO, e.COMPRA_EMITINDO, e.VENDA_EMITINDO,
                       e.EM_ALTERACAO, e.ALTERACAO_ENVIANDO,
                       e.CANCELAMENTO_ENVIANDO, e.CANCELADO):
            assert not e.alteravel(status)

    def test_locks_do_pos_emissao_nao_sao_os_da_emissao(self):
        """O 'Destravar' da emissão avisa de duplicata; aqui reexecutar é seguro."""
        for status in (e.ALTERACAO_ENVIANDO, e.CANCELAMENTO_ENVIANDO):
            assert e.enviando_alteracao(status) and not e.emitindo(status)
        for status in (e.COMPRA_EMITINDO, e.VENDA_EMITINDO, e.EM_ALTERACAO, e.EMITIDO):
            assert not e.enviando_alteracao(status)

    @pytest.mark.parametrize("olist_id,esperado", [
        ("900", e.EMITIDO), (900, e.EMITIDO),
        (None, e.COMPRA_EMITIDA), ("", e.COMPRA_EMITIDA), ("  ", e.COMPRA_EMITIDA),
        (float("nan"), e.COMPRA_EMITIDA),          # coluna vazia lida pelo pandas
    ])
    def test_estado_emitido_deriva_do_olist_id(self, olist_id, esperado):
        assert e.estado_emitido({"olist_id": olist_id}) == esperado

    def test_volta_do_pos_emissao_e_sempre_um_estado_emitido(self):
        for lock in (e.EM_ALTERACAO, e.ALTERACAO_ENVIANDO, e.CANCELAMENTO_ENVIANDO):
            assert {e.COMPRA_EMITIDA, e.EMITIDO} <= e.TRANSICOES[lock]


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

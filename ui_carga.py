"""
ui_carga.py — Carregamento de dados com feedback visível ao usuário.

Porta ÚNICA de carregamento das páginas. Antes cada página repetia a mesma
função `_carregar()` e o mesmo `st.spinner("Carregando dados...")` — um spinner
genérico que, em cache frio, girava ~2 minutos sem dizer nada. O usuário
concluía, razoavelmente, que o sistema tinha travado.

Hoje a carga fria leva ~10 s (ver `etl/loader.py`), mas o princípio vale igual:
espera sem expectativa vira suspeita de defeito. Esta tela mostra progresso
real, diz que é a PRIMEIRA carga da hora e diz quanto costuma demorar.

Uso nas páginas:
    from ui_carga import carregar_com_feedback
    dados, config = carregar_com_feedback()
"""

import pandas as pd
import streamlit as st

from etl.loader import carregar_dados, carregar_config, invalidar_cache_dados


# Expectativa mostrada ao usuário. Medido em 02/set/2026: ~11 s de rede + ~1 s
# de transformação. Arredondado para cima de propósito — prometer menos do que
# entrega é o que mantém a mensagem confiável.
SEGUNDOS_ESPERADOS = 15


def carregar_com_feedback():
    """
    Config + dados, com feedback visível enquanto carrega.

    Retorna (dados, config) — a MESMA ordem do `_carregar()` que substituiu.

    POR QUE SPINNER E NÃO BARRA DE PROGRESSO. O loader sabe reportar progresso
    página a página (`carregar_dados(_progresso=...)`), mas o Streamlit fecha as
    duas portas para exibi-lo aqui:

    * desenhar DENTRO da função cacheada funciona, mas o `st.cache_data` grava
      esses elementos e os REPRODUZ no cache hit (element replay) — a barra
      reapareceria pronta em toda carga quente de 0,1 s, o "piscar" que
      queremos evitar;
    * desenhar num bloco criado FORA e preenchido de dentro é proibido —
      `CacheReplayClosureError`.

    Sobraria rodar a carga numa thread e fazer polling do progresso na thread
    principal: muita máquina para uma carga que hoje leva ~10 s (eram 117 s).
    O spinner com cronômetro é criado FORA do cache, some sozinho no fim e não
    sofre replay. O `_progresso` do loader continua existindo para chamadores
    fora do Streamlit (scripts CLI), onde nada disso se aplica.
    """
    config = carregar_config()

    with st.spinner(
        f"Lendo dados do Bling (Supabase) — primeira carga da hora, "
        f"~{SEGUNDOS_ESPERADOS}s. As próximas são instantâneas.",
        show_time=True,
    ):
        dados = carregar_dados()

    return dados, config


def rodape_frescor(dados: dict) -> None:
    """
    Rodapé discreto com a idade dos dados + botão de recarga.

    Torna o cache VISÍVEL em vez de mágico: sem isso o usuário não tem como
    saber se o número na tela é de agora ou de 50 minutos atrás — o que é uma
    questão de confiança no dado, não só de conforto.
    """
    quando = dados.get("carregado_em")
    if quando is None:
        return

    col_txt, col_btn = st.columns([5, 1])
    with col_txt:
        st.caption(
            f"Dados lidos às {quando:%H:%M} · releitura automática às "
            f"{(quando + pd.Timedelta(hours=1)):%H:%M}"
        )
    with col_btn:
        if st.button("↻ Recarregar", width="content", key="recarregar_dados"):
            invalidar_cache_dados()
            st.rerun()

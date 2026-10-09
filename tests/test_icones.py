"""
Ícones Material Symbols (`:material/nome:`) usados na interface.

Um nome errado não quebra na importação: em `icon=` só estoura ao renderizar a
tela, e dentro de texto markdown aparece cru (":material/xpto:") sem erro
nenhum. Este teste varre o código da UI e confere cada nome contra a lista que o
próprio Streamlit valida.
"""

import re
from pathlib import Path

import pytest
from streamlit.material_icon_names import ALL_MATERIAL_ICONS

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVOS_UI = sorted(
    [RAIZ / "app.py", RAIZ / "auth.py", RAIZ / "ui_carga.py", RAIZ / "ui_tabelas.py",
     RAIZ / "etl" / "loader.py"]
    + list((RAIZ / "pages").glob("*.py"))
)
PADRAO = re.compile(r":material/([A-Za-z0-9_]+):")


@pytest.mark.parametrize("arquivo", ARQUIVOS_UI, ids=lambda p: p.name)
def test_nomes_de_icone_existem(arquivo):
    nomes = set(PADRAO.findall(arquivo.read_text(encoding="utf-8")))
    desconhecidos = sorted(n for n in nomes if n not in ALL_MATERIAL_ICONS)
    assert not desconhecidos, f"ícones inexistentes em {arquivo.name}: {desconhecidos}"


def test_navegacao_usa_material():
    """As 6 páginas do menu têm ícone Material (não emoji, que varia por sistema)."""
    icones = re.findall(r'st\.Page\([^)]*?icon="([^"]+)"',
                        (RAIZ / "app.py").read_text(encoding="utf-8"), flags=re.S)
    assert len(icones) == 6
    assert all(PADRAO.fullmatch(i) for i in icones), icones

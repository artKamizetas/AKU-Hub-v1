"""
pedidos/ — Domínio transacional de Pedidos de Compra.

Transforma o output do Simulador de Produção (etl/fabrica.py) em documentos
persistidos no Supabase (schema `app`, gravável — o `public` é o espelho
read-only do Bling): rodada congelada (snapshot) → pedidos rascunho por
Colégio × SuperCategoria → revisão/edição → (fases futuras: emissão no Bling
via API v3 e sincronização com o espelho).

Módulos:
    estados.py      — máquina de estados pura (constantes + transições)
    builder.py      — puro: DataFrame congelado → payloads (snapshot, grupos)
    grade.py        — puro: itens ↔ grade SKU pai × tamanho (pivot + diff da edição)
    catalogo.py     — puro: catálogo de produtos p/ a inclusão manual de itens
    repositorio.py  — ÚNICA porta de escrita/leitura do schema `app`
"""

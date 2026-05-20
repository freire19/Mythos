# Runbook — Cadastro de variantes de modelo em `_PRICING`

> **Quando aplicar:** sempre que um provider lançar uma variante de modelo com **preço diferente** do modelo-base já cadastrado (ex: `gpt-4.1-turbo` quando `gpt-4.1` já existe).

## Contexto

`_price_for` usa boundary match (`m.startswith(key + "-")`) para resolver variantes:

```python
# alpha/cost.py
"gemini-3.1-pro-preview-customtools" → casa com "gemini-3.1-pro" ✅
"xgpt-4.1"                            → não casa (prefix-pollution evitada) ✅
"gpt-4.1-turbo"                       → casa com "gpt-4.1" ⚠️ (preço pode estar errado!)
```

O último caso é o problema. `gpt-4.1-turbo` e `gpt-4.1` são modelos diferentes com **preços diferentes**, mas o boundary match não consegue distinguir. Não há solução pura de string-matching — a mitigação é **curadoria manual**.

## Checklist quando adicionar suporte a um modelo novo

Antes de mergear o código que adiciona o novo provider/model:

- [ ] **O novo modelo compartilha prefixo com algum modelo já em `_PRICING`?**
  - Liste os existentes: `python -c "from alpha.cost import _PRICING; print(sorted(_PRICING))"`
  - Use boundary check mental: `novo.startswith(existente + "-")`? Se sim, há colisão.

- [ ] **Os preços são iguais?**
  - Confira a [pricing page oficial](https://docs.anthropic.com/en/docs/about-claude/pricing) (ou equivalente do provider).
  - Se iguais → nenhuma ação. O fallback do boundary match resolve corretamente.
  - Se **diferentes** → **cadastre o novo modelo explicitamente** em `_PRICING`.

- [ ] **Cadastrou explicitamente?**
  - Edite `alpha/cost.py:_PRICING` adicionando a entrada com tupla `(prompt_per_1M, completion_per_1M)`.
  - Mantenha agrupado por provider (DeepSeek, OpenAI, Anthropic, Google, Grok).
  - Atualize o comentário `# Last updated YYYY-MM` no topo do dict.

- [ ] **Adicionou teste em `tests/test_cost.py::TestPriceFor`?**
  - Sugestão: `test_<modelo>_distinct_from_base` que confirma `_price_for("modelo-novo")` retorna o preço correto, não o do prefixo.

## Exemplo concreto

Cenário: OpenAI lança `gpt-4.1-turbo` com preço `(4.00, 16.00)` (vs `gpt-4.1` com `(2.00, 8.00)`).

**Antes:**
```python
_PRICING = {
    "gpt-4.1":      (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    ...
}
```

`_price_for("gpt-4.1-turbo")` → `(2.00, 8.00)` ❌ (preço subestimado em 2x).

**Depois (correto):**
```python
_PRICING = {
    "gpt-4.1":       (2.00, 8.00),
    "gpt-4.1-mini":  (0.40, 1.60),
    "gpt-4.1-turbo": (4.00, 16.00),   # ← cadastro explícito
    ...
}
```

`_price_for("gpt-4.1-turbo")` → `(4.00, 16.00)` ✅ (boundary match casa o key mais longo primeiro via `_PRICING_KEYS_BY_LEN`).

## O que NÃO fazer

- ❌ **Tentar resolver via código:** não há regex/heurística que distingua "variante mais cara" de "variante de mesmo preço" sem conhecer o mercado.
- ❌ **Cadastrar todos os sufixos imagináveis defensivamente:** infla o dict sem benefício; sufixos `-preview`, `-customtools` etc. tipicamente preservam preço do base e o boundary match já cobre.
- ❌ **Ignorar o problema:** o card `/preflight` mostra estimativa de custo. Estimativa errada pode levar o usuário a aprovar uma sessão que custaria 2-5x o estimado.

## Referências

- `alpha/cost.py:66-83` — implementação de `_price_for` com boundary match.
- `tests/test_cost.py::TestPriceFor` — testes existentes; cobrem prefix-pollution e longest-key-wins.

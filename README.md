# Engine de Inteligência Agendada

Um núcleo determinístico para conservar estado enquanto inteligências efêmeras medem e
propõem. Esta primeira entrega implementa a **fase 1** da especificação: objetos imutáveis,
templates tipados, proveniência, eventos, ponteiros e blobs endereçados por conteúdo.

## Garantias do núcleo

- A identidade de um objeto é `sha256(JSON canônico({tipo, schema, pais, corpo}))`.
  Data de criação e estado operacional não contaminam a identidade.
- Escritas relacionadas são transações `BEGIN IMMEDIATE`: objeto, arestas e evento aparecem
  juntos ou não aparecem.
- O corpo é validado com JSON Schema Draft 2020-12 antes de persistir.
- Toda movimentação de ponteiro é registrada no log e aceita compare-and-swap (`expected`),
  evitando atualizações perdidas.
- Leituras verificam novamente o hash, inclusive para blobs, e denunciam corrupção.
- O grafo pode ser navegado para trás (causas) ou para frente (efeitos).

### A âncora de gênese

Exigir que todo template cite outro template produz regressão infinita. O núcleo torna a
exceção explícita e pequena: o hash de 64 zeros é uma âncora embutida, utilizável **somente**
para criar templates-raiz por `create_genesis_template`. O JSON Schema desses templates é
validado antes da escrita. Nenhum objeto de domínio pode usar a âncora diretamente.

## Início rápido

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
engine --db engine.db init
```

Crie `programa.schema.json`:

```json
{
  "alvo": "programa",
  "nome": "programa_v1",
  "json_schema": {
    "type": "object",
    "required": ["nome", "proposito"],
    "additionalProperties": false,
    "properties": {
      "nome": {"type": "string", "minLength": 1},
      "proposito": {"type": "string", "minLength": 1}
    }
  }
}
```

Então materialize o template e use o hash retornado:

```bash
engine --db engine.db genesis-template programa.schema.json
engine --db engine.db create programa HASH_DO_TEMPLATE programa.json
engine --db engine.db set-pointer programas/ativo HASH_DO_PROGRAMA
engine --db engine.db lineage HASH_DO_PROGRAMA
```

O CLI escreve JSON em `stdout` e erros operacionais em formato de erro do `argparse`, o que
o torna simples de integrar a scripts sem abrir uma interface de shell remoto.

## API Python

```python
from engine import Store

with Store("engine.db") as engine:
    programa = engine.create("programa", schema_hash, {"nome": "local", "proposito": "medir"})
    engine.append_event(programa.hash, "agendado", {"slot": "claude-h"})
    engine.set_pointer("programas/ativo", programa.hash)
```

## Escopo e próximos passos

O pacote ainda não abre endpoints MCP nem executa código. Essa fronteira é deliberada: o
store é a raiz auditável sobre a qual serão construídos, em ordem, inventário/fila de jobs,
cards e travas de sessão, circuitos de decisão e calendário. Executores futuros devem chamar
esta API por operações curadas; nunca receber uma ferramenta de shell livre.

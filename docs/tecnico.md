# Documentação técnica do migrador MySQL → PostgreSQL

Para quem vai manter o código ou contribuir. Explica **como** o migrador funciona: que bibliotecas
usa, que consultas faz, que decisões toma e porquê, e o que fica de fora.

- Como usar (opções, passo a passo, testes de aceitação): [`guia.html`](guia.html) e [`README.md`](../README.md).
- Este documento não repete o guia; cita funções e classes pelo nome (ex.: `Planeador.mapear_tipo`, em `migrar.py`).

---

## 1. Visão geral

O migrador é um script de linha de comando (`migrar.py`) com uma interface web opcional
(`interface.py`). Corre em três fases:

| Fase | O que faz | Toca no destino? |
|---|---|---|
| 1. **validar** | Lê o esquema e os dados do MySQL, decide tipos e nomes no PostgreSQL, lista as incongruências (ERRO / AVISO / INFO). | Só leituras e testes em transações desfeitas. |
| 2. **migrar** | Cria tabelas, copia os dados, cria PK/índices, sequências, triggers, FKs e objetos de código. | Sim. |
| 3. **verificar** | Compara colunas, tipos, linhas, campos, FKs, índices, objetos e sequências. | Só leituras (e uma transação desfeita para comparar views). |

A fase 1 corre **sempre**, porque é ela que constrói o plano (tipos, nomes, conversões) de que as
outras precisam. `--modo` escolhe o resto:

| `--modo` | Fases |
|---|---|
| `validar` | 1 |
| `migrar` | 1 + 2 |
| `verificar` | 1 + 3 |
| `tudo` (padrão) | 1 + 2 + 3 |

Se a fase 1 tiver erros, a fase 2 não corre (salvo `--forcar`) e o código de saída é `2`.

### Fluxo de dados

```
                 information_schema                    pg_catalog / testes
   MySQL ─────────────────────────────┐            ┌──────────────── PostgreSQL
     │                                ▼            ▼                      ▲
     │                     ler_esquema_mysql   validar()                  │
     │                     + objetos.ler_objetos   │                      │
     │                                │            │                      │
     │      estatisticas_tabela ──►  EsquemaMySQL ─┴─► Planeador.planear  │
     │      (1 SELECT por tabela)     (Tabela, Coluna,    (tipo_pg,       │
     │                                 Indice, FK)         conversao,     │
     │                                                     colacao_pg...) │
     │                                                         │          │
     │  SSCursor (streaming)                                   ▼          │
     └──────────────────────► Migrador.copiar_tabela ── conversor() ── COPY FROM STDIN
                                                                          │
     ┌──────────────────────── Verificador ◄──────────────────────────────┘
     │   linhas_mysql() + linhas_pg(), ordenadas pela PK, normalizar() dos dois lados
     ▼
  Relatorio.gravar() ──► relatorios/migracao_<data>.{md,json,sql} + _por_rever.sql
```

### Módulos

| Ficheiro | Responsabilidade |
|---|---|
| `migrar.py` | Tudo o que é tabelas e dados: ligações, leitura do esquema, estatísticas, plano de tipos, validação, cópia, FKs, verificação, relatório, `main()`. |
| `objetos.py` | Leitura e tradução de triggers, procedures, funções, eventos e views para PL/pgSQL; tradução de expressões (colunas geradas, `DEFAULT (expr)`). |
| `interface.py` | Servidor HTTP local que lança o `migrar.py` num subprocesso e envia a saída para o navegador. |
| `web/index.html` | Página única (HTML + JS sem dependências): formulário, emulador de terminal, lista de relatórios. |
| `testes/casos.sql`, `testes/casos_espaciais.sql` | Bases MySQL com os casos difíceis. |

### Classes principais (`migrar.py`)

| Classe | Papel |
|---|---|
| `DadosConexao` | host, porta, utilizador, senha, base. Construída por `ler_conexao` (URL ou perguntas no terminal). |
| `Coluna`, `Indice`, `ChaveEstrangeira`, `Tabela`, `EsquemaMySQL` | Modelo do esquema MySQL. As `Coluna` também guardam as decisões do plano (`tipo_pg`, `conversao`, `colacao_pg`, `padrao_pg`, `geracao_modo`...). |
| `Estatisticas` | Contagens tiradas dos dados de uma coluna (datas zero, `\0`, etc.). |
| `Planeador` | Decide tipos, valores por defeito, collations e nomes no PostgreSQL. |
| `Relatorio` | Acumula achados, DDL e resultados; grava os ficheiros. |
| `Migrador` | Fase 2. |
| `Verificador` | Fase 3. |
| `Traducao` | Resultado da tradução de um objeto de código (comandos, erro, nota). |

---

## 2. Bibliotecas

`requirements.txt`: `pymysql>=1.1`, `psycopg[binary]>=3.1`, `sqlglot==30.25.0`. O resto é biblioteca padrão.
É preciso **Python 3.12+** (ver secção 10).

### pymysql (leitura do MySQL)

- **`ligar_mysql`** abre a ligação com `charset="utf8mb4"` e `autocommit=True` e faz
  `SET SESSION time_zone = '+00:00', SESSION sql_mode = ''`:
  - `time_zone` em UTC: os `TIMESTAMP` saem exatamente como estão gravados (o MySQL guarda-os em UTC).
  - `sql_mode` vazio: o MySQL deixa ler datas zero (`0000-00-00`) e outros valores "não estritos"
    sem erro. O pymysql devolve-as como **texto** (não as consegue converter em `date`), e o
    `conversor` usa isso para as reconhecer.
- **`pymysql.cursors.SSCursor`** (cursor sem buffer) na cópia e na verificação: as linhas vêm em
  streaming com `fetchmany(--lote)`, sem carregar a tabela em memória. Cada tabela usa uma ligação
  nova (`copiar_tabela`, `Verificador.linhas_mysql`), porque um SSCursor ocupa a ligação até ao fim.
- As leituras de esquema e as estatísticas usam o cursor normal na ligação principal.

### psycopg 3 (escrita no PostgreSQL)

- **`ligar_pg`**: `autocommit=True` e `SET TIME ZONE 'UTC'`. As transações são explícitas, com
  `con.transaction()`.
- **Composição segura de SQL** com `psycopg.sql`: `sql.Identifier` para nomes (aspas e esquema),
  `sql.Literal` para valores, `sql.SQL(...).format(...)` para juntar. Os tipos e as expressões
  traduzidas entram como `sql.SQL(texto)`, porque já são SQL. Do lado MySQL, `qm()` põe os nomes
  entre acentos graves; em `objetos.py`, `_qi()` põe-nos entre aspas.
- **`COPY ... FROM STDIN`** com `cursor.copy()` e `Copy.write_row()` em `Migrador.copiar_tabela`:
  o psycopg adapta cada valor Python (bool, int, Decimal, datetime, bytes, str) para o formato do COPY.
  Uma tabela = uma transação: se a cópia falha, a tabela fica vazia e passa-se à seguinte.
- **`transaction(force_rollback=True)`** para testar sem deixar rasto: expressões de colunas geradas
  e `DEFAULT` (`testar_expressoes`), e a comparação de views sem os registos criados (`comparar_view`).
- **Cursor com nome** (`cursor(name=...)`, cursor do lado do servidor) em `Verificador.linhas_pg`,
  com `itersize = --lote`, para ler o PostgreSQL em streaming.
- `Migrador.executar` guarda o texto de cada comando (`as_string`) em `Relatorio.ddl` antes de o executar.

### sqlglot (tradução de SQL), versão fixa `30.25.0`

Usado só em `objetos.py`, para o SQL "normal" dentro dos objetos de código e para expressões:

- `sqlglot.parse_one(texto, read="mysql")` → árvore; `Tradutor._ajustar` mexe na árvore
  (`transform`) e `.sql(dialect="postgres", normalize_functions=False)` escreve PostgreSQL.
- Faz as traduções de funções e operadores (`IFNULL`, `<=>`, `IF()`, `DATE_ADD`, `CONCAT`...).
- `exp.DataType.build(t, dialect="mysql")` traduz tipos de parâmetros e variáveis (`Tradutor.tipo`).

A versão está fixa porque o resultado da tradução depende da versão (nomes de nós da árvore,
como `exp.DPipe`, `exp.Introducer`, `exp.NullSafeEQ`, e a forma do SQL gerado). Antes de subir a
versão, correr os testes da secção 9 e comparar os `.sql` e `_por_rever.sql`.

A parte procedimental (`BEGIN ... END`, `DECLARE`, `IF`, `WHILE`...) **não** é do sqlglot:
é um tokenizador por expressão regular (`objetos._TOKEN`, `tokenizar`) e um analisador
descendente recursivo (`objetos.Procedimento`).

### Biblioteca padrão

| Módulo | Para quê |
|---|---|
| `argparse` | Opções de `migrar.py` (`ler_argumentos`) e `interface.py`. |
| `dataclasses` | Modelo do esquema, `Estatisticas`, `Traducao`, objetos de `objetos.py`. |
| `urllib.parse` | Ler URLs de ligação (`urlparse`, `unquote`) e montá-los na interface (`quote`). |
| `getpass` | Pedir a senha sem eco. |
| `hashlib` | `md5` para encurtar nomes com mais de 63 bytes (`Planeador.nome_unico`). |
| `json`, `decimal`, `datetime` | Conversão e normalização de valores; relatório JSON. |
| `collections.Counter` | Contadores de transformações; comparação de tabelas sem PK e de views. |
| `http.server` (`ThreadingHTTPServer`), `subprocess`, `threading`, `codecs`, `signal`, `secrets`, `socket`, `webbrowser` | Interface web (secção 6). |

---

## 3. Fase 1 — validar

Começa em `main()` → `ler_esquema_mysql` → `Planeador(...)` → `validar(...)`.

### 3.1 Leitura do esquema (`ler_esquema_mysql`, `objetos.ler_objetos`)

Tudo vem do `information_schema`, filtrado por `TABLE_SCHEMA = <base>`:

| Consulta | Dá |
|---|---|
| `TABLES` (`TABLE_TYPE='BASE TABLE'`) | tabelas, comentário, `TABLE_ROWS` (estimativa, só para o progresso), tamanho. Aplica `--tabelas` / `--excluir`. |
| `COLUMNS` | tipo (`DATA_TYPE`, `COLUMN_TYPE`), nulo, default, `EXTRA` (auto_increment, on update, generated), tamanhos, collation, comentário, `GENERATION_EXPRESSION` (se a consulta falhar — MySQL ≤ 5.6 — repete sem ela). |
| `COLUMNS.SRS_ID` | SRID das colunas espaciais (só MySQL 8; erro ignorado). |
| `STATISTICS` | PK (`INDEX_NAME='PRIMARY'`), índices, `NON_UNIQUE`, `SUB_PART` (prefixo), `INDEX_TYPE` (BTREE, FULLTEXT, SPATIAL). Coluna `NULL` = índice funcional (`tipo="FUNCIONAL"`). |
| `KEY_COLUMN_USAGE` ⨝ `REFERENTIAL_CONSTRAINTS` | FKs (colunas, tabela e base referenciadas, `UPDATE_RULE`, `DELETE_RULE`). |
| `TRIGGERS`, `ROUTINES`, `PARAMETERS`, `EVENTS`, `VIEWS` | objetos de código (`objetos.ler_objetos`). |

O utilizador MySQL só precisa de `SELECT` (e `SHOW VIEW`, `TRIGGER`, `EVENT` para ver o corpo
desses objetos).

### 3.2 Estatísticas dos dados (`estatisticas_tabela`, `Estatisticas`)

Para decidir a conversão é preciso saber o que está nos dados. Em vez de uma consulta por
problema, `estatisticas_tabela` monta **um único `SELECT SUM(...), SUM(...), ... FROM tabela`**
por tabela, com uma expressão por (coluna, contagem). É uma leitura completa da tabela.
A mesma leitura devolve também `COUNT(*)` (fica em `Tabela.linhas_contadas`; o `TABLE_ROWS` do
InnoDB é só uma estimativa) e a soma de `LENGTH()` de todos os valores (`Tabela.bytes_dados`),
usados na estimativa de tempo.

| Campo de `Estatisticas` | Colunas | Expressão MySQL (resumida) |
|---|---|---|
| `datas_zero` | date, datetime, timestamp | ano, mês e dia = 0 |
| `datas_parciais` | idem | algum de ano/mês/dia = 0, mas não todos |
| `datas_invalidas` | idem | `DAYOFMONTH(x) > DAYOFMONTH(LAST_DAY(x))` (ex.: 2023-02-31) |
| `fora_bool` | `tinyint(1)` (sem `--sem-booleanos`) | `x NOT IN (0,1)` |
| `nul_bytes` | texto, set, json | `INSTR(CAST(x AS BINARY), CHAR(0)) > 0` |
| `json_nul` | json | contém `\u0000` |
| `espacos_fim` | texto em PK/índice/FK com collation PAD SPACE | `CHAR_LENGTH(x) > CHAR_LENGTH(RTRIM(x))` |
| `enum_vazio` | enum sem `''` na lista | `x = ''` |
| `hora_fora` | time | `< '00:00:00'` ou `>= '24:00:00'` |
| `acima_bigint` | bigint unsigned | `> 9223372036854775807` |

`colunas_chave` diz que colunas estão em PK, índice ou FK (dos dois lados); `pad_space` diz se a
collation ignora espaços no fim (no MySQL 8 só as `_0900_` são NO PAD; no MariaDB as `nopad`).

### 3.3 Mapeamento de tipos (`Planeador.mapear_tipo`)

Devolve `(tipo_pg, conversao)`. A `conversao` escolhe a função de `conversor()` usada na cópia.

| MySQL | PostgreSQL | `conversao` | Notas |
|---|---|---|---|
| `tinyint(1)` | `boolean` | `bool` | Só se `fora_bool == 0`, sem `--sem-booleanos`, e `parece_numero` falso (nome `id`/`*_id` ou coluna de FK). |
| outro `tinyint` | `smallint` | | |
| `smallint` | `smallint` (`integer` se unsigned) | | |
| `mediumint` | `integer` | | |
| `int`, `integer` | `integer` (`bigint` se unsigned) | | |
| `bigint` | `bigint` (`numeric(20)` se unsigned **e** `acima_bigint > 0`) | | `numeric(20)` impediria FKs para `bigint`. |
| `decimal(p,s)`, `numeric` | `numeric(p,s)` | | |
| `float` | `real` | | |
| `double`, `real` | `double precision` | | |
| `bit(1)` | `boolean` | `bit1` | |
| `bit(2..63)` | `bigint` | `bit` | bytes → inteiro |
| `bit(64)` | `numeric(20)` | `bit` | não cabe em bigint |
| `char(n)`, `varchar(n)` | `varchar(n)` | `texto` | `char` vira `varchar` (sem enchimento com espaços). |
| `tinytext` … `longtext` | `text` | `texto` | |
| `binary`, `varbinary`, `*blob` | `bytea` | `bytes` | |
| `date` | `date` | `data` | |
| `datetime(p)` | `timestamp(p)` | `data` | sem fuso |
| `timestamp(p)` | `timestamptz(p)` | `data_tz` | valor lido em UTC e marcado UTC |
| `time(p)` | `time(p)` | `hora` | `timedelta` → `time` |
| `time` com `hora_fora` | `interval` | | negativo ou ≥ 24h |
| `year` | `smallint` | | |
| `json` | `jsonb` | `json` | `\u0000` removido |
| `enum(...)` | `varchar(maior valor)` + `CHECK (col IN (...))` | `texto` | `''` entra no CHECK se existir nos dados; `--enum-sem-check` tira o CHECK. |
| `set(...)` | `text` | `texto` | sem validação |
| `geometry`, `point`, `polygon`... | `geometry(Subtipo[,SRID])` | `geom` | Só com PostGIS disponível; senão ERRO. |
| qualquer outro | — | | ERRO "Tipo não suportado". |

Depois do tipo, `Planeador.planear` ainda decide:

- `AUTO_INCREMENT` em inteiro → `GENERATED BY DEFAULT AS IDENTITY` (`identidade`), e força `bigint`
  se o tipo não for inteiro.
- unsigned numérico (sem identidade) → `CHECK (col >= 0)` (`nao_negativo`).
- datas sem valor nos dados → a coluna passa a aceitar NULL (`nula_pg = True`).
- nomes: minúsculas (salvo `--manter-maiusculas`); índices passam a `<tabela>_<índice>` e FKs ficam
  com o nome original, ambos por `nome_unico` (máx. 63 bytes, com sufixo md5 se for preciso, e únicos no esquema).

### 3.4 Collations (`colacao_equivalente`, `COLACOES_PG`)

No MySQL, `_ci` ignora maiúsculas (e acentos, nas `_ai_ci` / `general_ci`). No PostgreSQL
isso exige uma **collation ICU não determinística**. Com `--comparacao-texto mysql` (padrão):

| Collation MySQL | Collation criada no esquema de destino | ICU |
|---|---|---|
| `*_ci` (general, unicode, `0900_ai_ci`...) | `mysql_ci_ai` | `und-u-ks-level1` (ignora maiúsculas e acentos) |
| `*_as_ci` | `mysql_ci` | `und-u-ks-level2` (ignora só maiúsculas) |
| `*_bin`, `*_cs`, vazia | nenhuma | comparação exata |

`Migrador.criar_tabelas` cria-as com `CREATE COLLATION ... (provider = icu, locale = ..., deterministic = false)`
e `ddl_coluna` põe `COLLATE esquema.mysql_ci_ai` nas colunas de texto.

`validar` desliga isto (AVISO) se o PostgreSQL for anterior ao 12 ou não tiver ICU, e avisa que
`LIKE` só funciona nestas collations a partir do PostgreSQL 18.

**PAD SPACE:** no MySQL (exceto `_0900_` e `nopad`) `'x' = 'x '`; no PostgreSQL não. Isto não se
resolve com collation. O migrador conta (`espacos_fim`) e avisa; com `--aparar-espacos` a coluna
fica com `aparar = True` e o `conversor` tira os espaços do fim na cópia.

### 3.5 Valores por defeito (`Planeador.traduzir_padrao`)

| `COLUMN_DEFAULT` MySQL | PostgreSQL |
|---|---|
| `NULL` / sem default | nenhum |
| `CURRENT_TIMESTAMP`, `NOW()`, `LOCALTIMESTAMP` | `CURRENT_TIMESTAMP` (`CURRENT_DATE` numa `date`) |
| `CURDATE()`, `CURRENT_DATE` | `CURRENT_DATE` |
| `0`/`1`/`b'0'`/`b'1'` em boolean | `FALSE` / `TRUE` |
| `b'101'` em bit | inteiro |
| `'0000-00-00...'` em data | com `--datas-zero null` (padrão): nenhum, e a coluna passa a aceitar NULL; com `--datas-zero AAAA-MM-DD`: essa data, e a coluna mantém-se NOT NULL (AVISO nos dois casos) |
| número | número (`Decimal`); se não for número, removido (AVISO) |
| expressão (`EXTRA` com `DEFAULT_GENERATED`, ex. `('texto')`, `(uuid())`) | traduzida depois por `Tradutor.expressao_coluna` e testada no PostgreSQL |
| restante | `sql.Literal(texto)` |

No MariaDB os defaults vêm entre aspas e `NULL` vem como texto; o código trata essa diferença.
No MySQL 8 as expressões vêm com as aspas escapadas (`_utf8mb4\'x\'`), que
`objetos.desescapar_information_schema` desfaz.

### 3.6 Expressões testadas no PostgreSQL (`testar_expressoes`)

As fórmulas de colunas geradas e os `DEFAULT (expressão)` traduzidos são **experimentados** numa
tabela temporária (`migr_teste_expressao`) dentro de `transaction(force_rollback=True)`:

1. Coluna gerada: tenta `GENERATED ALWAYS AS (...) STORED`. Se der, `geracao_modo = "stored"`.
2. Se falhar (o PostgreSQL só aceita funções **imutáveis**, ex.: `date_format`/`to_char` não é),
   testa se `SELECT (expr)::tipo` funciona. Se sim, `geracao_modo = "trigger"` (fase 2 cria um trigger).
3. Senão, `geracao_modo = ""`: fica coluna normal com os valores copiados (AVISO).
4. `DEFAULT`: testa `SELECT (expr)::tipo`; se falhar, a coluna fica sem default (AVISO).

As colunas `VIRTUAL` do MySQL ficam `STORED` (o PostgreSQL ≤ 17 não tem virtuais; os valores são iguais).

`Tradutor.expressao_coluna` faz ajustes próprios para colunas geradas: tira introdutores
(`_utf8mb4'x'` → `'x'`), `CAST(x AS CHAR)` → `CAST(x AS TEXT)`, e `CONCAT(a, b)` →
`(a::text || b::text)` (imutável e NULL se um lado for NULL, como no MySQL).

### 3.7 Validações de FKs (em `validar`)

Para cada FK:

1. **Fora da migração** (outra base, ou tabela excluída): `ignorar = True`, AVISO, não é criada.
2. **Tipos incompatíveis**: compara `familia()` dos dois lados (`numero`, `texto`, `data`, ...). ERRO.
3. **Colunas referenciadas sem UNIQUE/PK** (o MySQL aceita, o PostgreSQL não): conta duplicados
   com `GROUP BY ... HAVING COUNT(*) > 1`. Sem duplicados → acrescenta a `uniques_extra` e a fase 2
   cria um `UNIQUE` (AVISO). Com duplicados → ERRO e a FK é ignorada.
4. **Órfãos**: `LEFT JOIN` filho → pai `WHERE filho NOT NULL AND pai IS NULL`, com `COUNT(*)` e
   `COUNT(DISTINCT chave)`. A condição de junção (`igual()`, função interna) compara **como o PostgreSQL vai comparar**:
   - texto sem collation `_ci` no destino → `CAST(... AS BINARY)` (sensível a maiúsculas);
   - texto com `_ci` dos dois lados → `=` do MySQL, mais uma condição sobre o número de espaços
     no fim se a collation for PAD SPACE (o PostgreSQL não os ignora);
   - com `--aparar-espacos`, `RTRIM` dos dois lados.

   Resultado: AVISO "pais em falta serão criados" (`--orfaos criar-pais`) ou "FK fica NOT VALID".

### 3.8 Outras verificações

| Função / bloco | O que detecta |
|---|---|
| `avisar_modo_nao_estrito` | `@@GLOBAL.sql_mode` sem `STRICT_*`, **ou** sinais nos dados de escrita sem modo estrito (datas zero, ENUM `''`, `DEFAULT '0000-00-00'`). Lista as colunas NOT NULL sem default: o PostgreSQL recusa INSERTs que as omitam. |
| `avisar_nomes_reservados` | Nomes de tabela/coluna em `pg_get_keywords()` com `catcode IN ('R','T')`. São criados entre aspas; a aplicação terá de os escrever entre aspas. |
| `planear` | Nomes com mais de 63 bytes; tabelas ou colunas que colidem depois de passar a minúsculas (ERRO). Com `--renomear-duplicadas`, a tabela cujo nome já está em minúsculas fica com ele e as outras recebem `_2`, `_3`... (AVISO); as views e rotinas usam o nome novo, porque o tradutor procura primeiro o nome exato (`Tradutor.nomes_exatos`). |
| `validar` (fim) | Permissão `CREATE` na base/esquema; tabelas já existentes no destino (ERRO, ou AVISO com `--substituir`). |
| `validar` (por tabela) | Tabela sem PK, FULLTEXT → GIN, SPATIAL → GiST, índice funcional (não migrado), índice com prefixo, `ON UPDATE CURRENT_TIMESTAMP`. |
| `traduzir_objetos` | Traduz já todos os objetos de código, para mostrar na validação o que fica por rever. |

### 3.9 ERRO, AVISO, INFO

`Relatorio.add(fase, nivel, categoria, objeto, mensagem)`.

| Nível | Significado | Efeito |
|---|---|---|
| **ERRO** | A migração falharia ou ficaria errada (tipo sem conversão, FK impossível, nome demasiado longo, colisão de nomes, tabelas já existentes sem `--substituir`, sem permissão, espaciais sem PostGIS). Na fase 3: qualquer diferença. | Na fase 1, bloqueia a fase 2 (exceto `--forcar`). Na 3, código de saída `1`. |
| **AVISO** | Migra, mas há uma mudança de comportamento ou de dados que alguém deve rever (datas → NULL, `\0` removido, órfãos, objeto por rever, modo não estrito...). | Nenhum; aparece no relatório. |
| **INFO** | Decisão tomada que convém saber (bigint unsigned → bigint, FULLTEXT → GIN, tradução automática feita...). | Nenhum. |

---

## 4. Fase 2 — migrar (`Migrador.executar_tudo`)

### 4.1 Ordem e porquê

| # | Método | Porquê nesta posição |
|---|---|---|
| 1 | `criar_tabelas` | Numa só transação: esquema, `CREATE EXTENSION postgis` (se preciso), collations ICU, `migracao.registos_criados` (com `criar-pais`), `DROP ... CASCADE` (com `--substituir`), `CREATE TABLE` só com colunas, NOT NULL, DEFAULT, CHECK e identidade, e os comentários. **Sem PK, índices nem FKs.** |
| 2 | `copiar_tabela` (cada tabela) | Sem FKs nem índices, a ordem das tabelas não importa e o COPY é mais rápido. |
| 3 | `criar_chaves_e_indices` | PK, `UNIQUE`, índices normais, GIN (FULLTEXT), GiST (SPATIAL), índices de prefixo e os `UNIQUE` extra exigidos por FKs. Criar índices depois dos dados é mais rápido do que mantê-los durante o COPY. |
| 4 | `acertar_sequencias` | `setval(pg_get_serial_sequence(...), MAX(id))`: o próximo id é o maior + 1. |
| 5 | `criar_pais_em_falta` (só `criar-pais`) e de novo `acertar_sequencias` | Insere os pais em falta antes das FKs; os ids inseridos podem passar o máximo. |
| 6 | `criar_triggers_on_update` | Depois da cópia, para não mexer nos valores copiados. |
| 7 | `criar_triggers_geradas` | Idem: os valores das colunas geradas por trigger já vêm do MySQL. |
| 8 | `criar_fks` | Por último entre as constraints: `ADD CONSTRAINT ... NOT VALID` e depois `VALIDATE CONSTRAINT`. Se a validação falhar (órfãos), a FK fica NOT VALID (AVISO), em vez de não existir. |
| 9 | `criar_objetos` | Funções, views, triggers e eventos dependem de tabelas, colunas e outras funções. |
| 10 | `ANALYZE` | Estatísticas do planeador do PostgreSQL. |

Cada comando de DDL depois da criação das tabelas corre com `Migrador.tentar`: uma transação
por comando; se falhar, regista ERRO e continua (um índice falhado não impede os outros).

### 4.2 Cópia e conversão de valores

`copiar_tabela`:

1. Lista as colunas a copiar (exclui as `GENERATED ... STORED`; o PostgreSQL calcula-as e recusa valores).
2. `SELECT` no MySQL com `select_mysql(c)`; para espaciais lê `HEX(SRID) || HEX(ST_AsBinary(col, 'axis-order=long-lat'))`.
3. Para cada linha, aplica `conversor(c)` a cada valor e escreve com `cp.write_row(...)`.

`conversor(c, contadores, chave)` devolve uma função por coluna, escolhida por `c.conversao`:

| `conversao` | Conversão | Contador (relatório) |
|---|---|---|
| `bool` | `bool(v)` | |
| `bit1` | bytes → `!= 0` | |
| `bit` | bytes → `int.from_bytes(..., "big")` | |
| `data`, `data_tz` | se o pymysql devolveu `str` (data zero/parcial/inválida) → `None`, ou `Coluna.data_substituta` com `--datas-zero AAAA-MM-DD`; `data_tz` recebe `tzinfo=UTC` | `datas_zero` (AVISO) |
| `hora` | `timedelta` → `time` | |
| `texto` | bytes → UTF-8 (com `errors="replace"` se inválido); tira `\x00`; com `aparar`, `rstrip(" ")` | `utf8_invalido` (AVISO), `nul_removido`, `espacos_aparados` |
| `json` | se contiver `\u0000`: `json.loads`, limpa recursivamente, `json.dumps` | `json_nul` |
| `bytes` | `str` → bytes | |
| `geom` | `ewkb_hex`: SRID + WKB → EWKB hexadecimal (bit `0x20000000` no tipo + SRID) | |
| `""` | sem conversão | |

Os contadores viram achados no fim da fase 2: `Datas zero → NULL` (ou `→ <data>`) e
`UTF-8 inválido` como AVISO; os restantes como `Transformações aplicadas` (INFO). Como a
verificação passa os valores do MySQL pelo mesmo `conversor`, a data substituta conta como igual.

**Datas zero: as duas opções (`--datas-zero`).** O PostgreSQL não tem `0000-00-00`, nem datas
parciais (`2024-05-00`) ou inválidas (`2023-02-31`).

| Opção | Valores | Coluna NOT NULL no MySQL | `DEFAULT '0000-00-00'` | Na aplicação |
|---|---|---|---|---|
| `null` (padrão) | NULL | passa a aceitar NULL | removido | `= '0000-00-00'` → `IS NULL` |
| `0001-01-01` (ou outra data) | essa data (às 00:00:00 em datetime/timestamp) | mantém NOT NULL | essa data | `= '0000-00-00'` → `= '0001-01-01'` |

`0001-01-01` é a sugestão porque é a menor data válida e, como `0000-00-00` no MySQL, ordena
antes de todas as outras. As datas parciais perdem o ano/mês que tinham (passam todas à mesma
data); quem precisar delas deve corrigi-las no MySQL antes de migrar.

### 4.3 Pais em falta (`criar_pais_em_falta`, `valor_substituto`)

Com `--orfaos criar-pais` (padrão), para cada FK não ignorada é feito um único comando:

```sql
WITH novos AS (
  INSERT INTO pai (chave..., campos_obrigatorios...)
  SELECT k.k0, ..., <valores_substitutos>
  FROM (SELECT DISTINCT f.col AS k0 ... FROM filho f
        WHERE f.col IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pai p WHERE p.col = f.col)) k
  ON CONFLICT DO NOTHING
  RETURNING jsonb_build_array(<pk do pai>) AS chave)
INSERT INTO migracao.registos_criados (esquema, tabela, colunas, chave, motivo)
SELECT ... FROM novos
```

- `valor_substituto` preenche as colunas NOT NULL sem default: primeiro valor do ENUM; se for
  FK, `(SELECT MIN(ref) FROM avô)`; números `0` (ou `MAX + ROW_NUMBER()` se for único); texto
  `REGISTO EM FALTA (criado na migração)` (ou `'em falta ' || md5(...)` se for único, cortado ao `varchar(n)`);
  datas `1970-01-01`; boolean `FALSE`; `jsonb '{}'`; `bytea ''`; `interval '0'`.
  Se não souber (ex.: geometria), a FK fica NOT VALID (AVISO).
- Repete até 5 vezes, porque um pai criado pode precisar ele próprio de um pai.
- `migracao.registos_criados` guarda a chave (jsonb) de cada registo criado; a fase 3 usa-a
  para não contar estes registos como diferenças.

### 4.4 Sequências

As colunas `AUTO_INCREMENT` são `GENERATED BY DEFAULT AS IDENTITY` (aceita os ids copiados).
`acertar_sequencias` faz `setval(seq, COALESCE(MAX(col), 1), MAX(col) IS NOT NULL)`: com a tabela
vazia, o próximo valor é 1. O contador `AUTO_INCREMENT` do MySQL (que pode estar acima do máximo,
depois de apagar linhas) **não** é lido.

### 4.5 `ON UPDATE CURRENT_TIMESTAMP` (`criar_triggers_on_update`)

Uma função genérica `migr_on_update_timestamp()` (`FUNCAO_ON_UPDATE`) recebe os nomes das colunas
em `TG_ARGV`. Num `BEFORE UPDATE`, se a linha mudou e a coluna **não** foi alterada pelo UPDATE,
põe `statement_timestamp()` (via `to_jsonb` / `jsonb_populate_record`, para aceder à coluna pelo nome).
É o mesmo comportamento do MySQL: se o UPDATE define a coluna, o valor dado prevalece.

### 4.6 Colunas geradas

- `geracao_modo == "stored"`: `GENERATED ALWAYS AS (...) STORED` no `CREATE TABLE`; não é copiada.
- `geracao_modo == "trigger"`: coluna normal, copiada, e depois `criar_triggers_geradas` cria
  `migr_gerar_<tabela>()` com `NEW.col := <expr com NEW.>` em `BEFORE INSERT OR UPDATE`.
- `""`: coluna normal com os valores atuais, sem recálculo.

### 4.7 Índices especiais

| MySQL | PostgreSQL | Onde |
|---|---|---|
| `FULLTEXT (a, b)` | `GIN (to_tsvector('simple', coalesce(a,'') \|\| ' ' \|\| coalesce(b,'')))` | `expressao_fulltext` |
| `SPATIAL` | `GiST` | `criar_chaves_e_indices` |
| prefixo, não único, colunas `varchar ≤ 600` | índice normal sobre a coluna inteira | `forma_indice_prefixo` → `"inteira"` |
| prefixo, não único, 1 coluna longa | `USING hash` (igualdade, sem limite de tamanho do B-tree) | `"hash"` |
| prefixo único ou várias colunas | `left(col, n)` | `"inicio"` |
| funcional (MySQL 8) | não migrado (AVISO) | |

### 4.8 Objetos de código (`objetos.py`, `traduzir_objetos`, `Migrador.criar_objetos`)

`traduzir_objetos` (na fase 1) produz uma `Traducao` por objeto, ordenada: funções/procedures (1),
views (2), triggers (3), eventos (4).

| MySQL | Gerado por | PostgreSQL |
|---|---|---|
| `TRIGGER` | `Tradutor.trigger` | `CREATE FUNCTION <trigger>_fn() RETURNS trigger` (retorna `NEW`, `OLD` em DELETE, ou `NULL` em AFTER) + `CREATE TRIGGER ... FOR EACH ROW` |
| `PROCEDURE` / `FUNCTION` | `Tradutor.rotina` | `CREATE OR REPLACE PROCEDURE/FUNCTION ... LANGUAGE plpgsql`, parâmetros com `IN/OUT/INOUT` e tipos via `Tradutor.tipo` |
| `EVENT` | `Tradutor.evento` | `PROCEDURE evento_<nome>()` + `cron.schedule_in_database(...)` se houver `pg_cron`; senão, uma linha de crontab na nota. `INTERVALO_CRON` cobre MINUTE, HOUR, DAY, WEEK (só 1), MONTH. |
| `VIEW` | `Tradutor.view` | `CREATE OR REPLACE VIEW` com o SQL traduzido pelo sqlglot, depois de dois ajustes (abaixo) |

Ajustes feitos às views antes de as escrever (`Tradutor._alias_no_having`, `Tradutor._completar_agrupamento`).
Cada um deixa uma nota INFO no relatório:

| Diferença | MySQL | PostgreSQL | O que o tradutor faz |
|---|---|---|---|
| Alias do SELECT no HAVING | `SELECT COUNT(*) AS total ... HAVING total > 1` | erro `column "total" does not exist` | troca o alias pela expressão: `HAVING COUNT(*) > 1` |
| Coluna fora do GROUP BY | aceita (sem `ONLY_FULL_GROUP_BY`) e devolve o valor de uma linha qualquer do grupo | erro `must appear in the GROUP BY clause` | envolve a coluna em `ANY_VALUE()` (PostgreSQL 16+; antes, `MIN()`). Também nas consultas com agregação e sem GROUP BY, e no HAVING e ORDER BY |

`ANY_VALUE` dá o mesmo resultado quando a coluna depende do que está no GROUP BY (o caso normal, ex.:
`a.codigo` com `GROUP BY a.id`). Se não depender, o MySQL e o PostgreSQL podem escolher linhas
diferentes, e a verificação da view mostra a diferença.

Com `--manter-maiusculas`, os nomes das tabelas nas views e rotinas vão entre aspas
(`"MID_ENTIDADE"`). Sem aspas, o PostgreSQL passava-os a minúsculas e não as encontrava.

`Procedimento` traduz: `BEGIN/END` (com rótulos), `DECLARE var tipo [DEFAULT]`, `SET` (várias
atribuições), `IF/ELSEIF/ELSE`, `CASE`, `WHILE`, `LOOP`, `REPEAT ... UNTIL` (→ `LOOP ... EXIT WHEN`),
`LEAVE`/`ITERATE` (→ `EXIT`/`CONTINUE`), `RETURN`, `SIGNAL SQLSTATE ... SET MESSAGE_TEXT`
(→ `RAISE EXCEPTION USING ERRCODE, MESSAGE`), `CALL`. Outros comandos SQL passam pelo sqlglot
(`Tradutor.comando_sql`); um `SELECT` sem `INTO` vira `PERFORM`.

`Tradutor._ajustar` ainda: tira o nome da base MySQL (`base.tabela` → esquema de destino), passa
nomes para minúsculas, aplica os nomes de tabela do plano e troca `0/1` por `FALSE/TRUE` quando a
coluna comparada ou atribuída passou a `boolean` (usa os tipos do plano, `_tipo_coluna`).

**O que levanta `NaoSuportado`** (o objeto vai para `_por_rever.sql`): cursores, `DECLARE ... HANDLER`
e `CONDITION`, `OPEN/FETCH/CLOSE`, `PREPARE/EXECUTE/DEALLOCATE`, `RESIGNAL`, variáveis `@sessao`,
`SET GLOBAL/SESSION/NAMES/...`, `SIGNAL` com condição nomeada, `DECLARE` fora do início do bloco,
SQL que o sqlglot não reconhece ou devolve como `exp.Command`, tipos sem tradução.

`criar_objetos` executa cada `Traducao` sem erro numa transação; repete enquanto houver progresso
(uma view pode depender de outra). O que continuar a falhar fica com `erro` preenchido → AVISO e `_por_rever.sql`.

---

## 5. Fase 3 — verificar (`Verificador.executar_tudo`)

### 5.1 Colunas (`verificar_colunas`, `tipos_esperados`)

`tipos_esperados` cria uma tabela temporária com uma coluna de cada tipo planeado e lê
`format_type()` para saber a forma canónica (`varchar(20)` → `character varying(20)`). Depois
compara, a partir de `pg_attribute`, para cada coluna: existe, tipo, NULL/NOT NULL, default (ou
identidade), collation (só texto) e `attgenerated = 's'` para as STORED. Colunas a mais → AVISO.

### 5.2 Linhas (`comparar_linhas`)

- **Os dois lados passam pela mesma normalização.** O lado MySQL aplica `conversor()` (o mesmo
  da cópia) e depois `normalizar()`; o lado PostgreSQL só `normalizar()`. Assim, uma data zero que
  virou NULL conta como igual.
- `normalizar`: `memoryview` → bytes; `Decimal` inteiro → `int`, senão `normalize()`; `float` a
  6 algarismos significativos em `real`, 15 em `double`; `datetime` com fuso → UTC sem fuso;
  `jsonb` → `json.dumps(sort_keys=True)`. Geometria: o PostgreSQL devolve `upper(encode(ST_AsEWKB(col),'hex'))`,
  comparável com `ewkb_hex` do lado MySQL.
- **Tabelas com PK:** as duas leituras vêm ordenadas pela PK (texto: `CAST(... AS BINARY)` no
  MySQL e `COLLATE "C"` no PostgreSQL, ambos por bytes UTF-8) e são percorridas em paralelo, como
  um *merge join*. Chave só no MySQL → **em falta**; só no PostgreSQL → **a mais** (exceto se estiver
  em `migracao.registos_criados`, conta como **criada**); nos dois e tuplos diferentes → **diferente**,
  com contagem por campo e amostras (`--amostras`). Memória constante.
- **Tabelas sem PK:** cada lado vira um `Counter` de `repr(linha)` (multiconjunto); a diferença
  dá em falta / a mais. Fica tudo em memória e não diz que campo difere.
- **Tamanhos por coluna:** soma o comprimento (`tamanho_valor`) e o número de não-nulos de cada
  coluna nos dois lados; se diferirem → ERRO "Tamanho de campo diferente".

É ERRO qualquer `diferentes`, `em_falta`, `a_mais` > 0 ou tamanho diferente. Hashes não são usados:
compara-se valor a valor.

Com `--sem-comparar-linhas`, `contar` só compara `COUNT(*)` (descontando os registos criados).

### 5.3 FKs, índices, objetos e sequências

| Método | Verifica |
|---|---|
| `verificar_fks_e_indices` | Cada FK do MySQL existe em `pg_constraint` (mesmas colunas, pela ordem); `convalidated = false` → AVISO NOT VALID. Número de índices em `pg_indexes` ≥ esperado (PK + índices não funcionais). |
| `verificar_objetos` | Cada objeto traduzido existe (`pg_trigger`, `pg_proc`, `pg_views`). |
| `comparar_view` | `SELECT *` da view nos dois lados → `Counter`. Se houver registos criados, apaga-os numa transação desfeita com `session_replication_role = replica` (precisa de superutilizador; sem ele, a diferença fica AVISO em vez de ERRO). |
| `verificar_sequencias` | `pg_sequence_last_value` ≥ `MAX(id)`, senão o próximo INSERT colidiria. |

---

## 6. Interface web (`interface.py`, `web/index.html`)

### Como lança o migrador

`montar_comando` valida o JSON da página contra listas fixas (`MODOS`, `FLAGS`, `ESCOLHAS`,
`TEXTOS`, `NUMEROS`) e monta `[sys.executable, "-u", "migrar.py", "--modo", ..., "--sim", ...]`.
Não há shell: a lista de argumentos vai direta ao `subprocess.Popen`.

- As ligações vão nas variáveis de ambiente `MIGRAR_ORIGEM` / `MIGRAR_DESTINO` (URL com a senha
  codificada por `quote`), não na linha de comando, que é visível no `ps`.
- `MIGRAR_TERMINAL=1` faz o `migrar.py` emitir cores ANSI e linhas de progresso com `\r` mesmo sem TTY.
- `start_new_session=True`: o subprocesso tem o seu grupo de processos.
- Só uma execução de cada vez (`Estado.trinco`; responde 409 se já houver uma).

### Como a saída chega ao navegador

1. `Execucao._ler` (thread) lê o stdout com `os.read(fd, 4096)` e um descodificador UTF-8 incremental
   (`codecs.getincrementaldecoder`), para não partir caracteres a meio. Cada bloco vai para `partes`
   e a `threading.Condition` acorda os leitores.
2. `GET /api/eventos` é **Server-Sent Events** (`text/event-stream`): envia cada parte como
   `id: N` + `data: "<json>"`; a cada 15 s sem nada, um comentário `: ainda aqui`; no fim,
   `event: fim` com o estado (código, duração, relatórios novos).
3. O `EventSource` do navegador volta a ligar sozinho e manda `Last-Event-ID`; o servidor continua
   desse ponto. Recarregar a página faz `GET /api/estado` e `seguir(0)` (repete a saída toda).
4. A página emula um terminal mínimo: `\n`, `\r`, `ESC[...m` (cores) e `ESC[K` (apagar até ao fim da linha).

**Parar** (`POST /api/parar` → `Execucao.parar`): 1.ª vez `SIGINT` (o `migrar.py` apanha
`KeyboardInterrupt` e grava o relatório; código 130); 2.ª vez `SIGKILL` ao grupo.

### Segurança

| Medida | Onde |
|---|---|
| Só escuta em `127.0.0.1` | `ThreadingHTTPServer(("127.0.0.1", porta), ...)` |
| Token aleatório por arranque (`secrets.token_urlsafe(24)`), no link aberto | `Estado.token` |
| Toda a API exige o token: cabeçalho `X-Token` ou `?t=` em GET; **só cabeçalho em POST** (um formulário de outro site não consegue pôr cabeçalhos) | `_token_valido`, comparação com `secrets.compare_digest` |
| Cabeçalho `Host` tem de ser `127.0.0.1:<porta>` ou `localhost:<porta>` (contra DNS rebinding) | `_host_valido` |
| CSP restritiva, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` | `do_GET`, `_enviar` |
| Pedidos JSON até 100 kB | `_ler_json` |
| `/relatorio/<nome>` só serve nomes que existem em `relatorios/` (sem `../`) | `do_GET` |
| A página guarda o formulário em `localStorage` **sem as senhas** | `guardar()` em `index.html` |

`POST /api/testar` (`testar_ligacao`) liga com `connect_timeout=5`, devolve a versão e lista as
bases (`SHOW DATABASES` / `pg_database`) para sugerir nomes. Se a base de destino não existir,
liga à `postgres` só para listar.

### Relatórios na interface

`relatorios_existentes` tira um retrato (nome → `mtime`) da pasta antes de lançar; no fim, os
ficheiros novos ou alterados são os "desta execução". `GET /api/relatorios` lista os 15 `.md`
mais recentes com os `.json`, `.sql` e `_por_rever.sql` correspondentes.

---

## 7. Relatórios

`Relatorio.gravar` corre sempre no `finally` de `main()` (também após Ctrl+C). Ficheiros em
`--relatorios` (padrão `./relatorios`), com o prefixo `migracao_<AAAAMMDD_HHMMSS>`:

| Ficheiro | Quando | Conteúdo |
|---|---|---|
| `.md` | sempre | Resultado no topo; início, fim, duração, modo, origem/destino e opções (sem os URLs, que podem levar a senha); tabela **Tempo** (estimado vs real por fase); **Erros**, **Avisos** e **Informação** agrupados por categoria; tabela **Tabelas** (linhas nos dois lados, tempo de cópia, iguais, diferentes, em falta, a mais, criadas p/ FK, estado); lista dos outros ficheiros da execução. |
| `.json` | sempre | Tudo: `inicio`, `fim`, `resultado`, `opcoes`, `resumo`, `tempos`, `estimativa` (incluindo os custos medidos e as amostras), `achados`, `migracao` (linhas, estado, segundos e segundos estimados por tabela), `verificacao` (incluindo `campos_diferentes`, `campos_tamanho_diferente`, `amostras`, volumes). |
| `.sql` | se a fase 2 executou DDL | Todos os comandos executados no PostgreSQL, por ordem (inclui os INSERT dos pais em falta). |
| `_por_rever.sql` | se houver objetos com `erro` | Por objeto: tipo, nome, motivo, original MySQL comentado e, se houver, a tradução automática a rever. |

Códigos de saída de `migrar.py`: `0` sem diferenças, `1` diferenças ou erros, `2` parado na
validação (ou nenhuma tabela), `3` sem ligação, `130` interrompido.

---

## Estimativa de tempo

No fim da validação (funções `calibrar`, `estimar` e `imprimir_estimativa`, em `migrar.py`) o
migrador mostra quanto deve demorar cada fase. Durante a cópia, a linha de progresso mostra
quanto falta para a tabela atual e para a cópia toda. No fim de cada fase imprime o tempo real ao
lado do estimado, e o relatório guarda os dois.

### Critérios

A duração depende sobretudo de **quantas linhas** e **quantos bytes** há para mover, e da
**velocidade desta máquina e desta rede**. Por isso a estimativa usa três coisas:

1. **Volume exato de cada tabela.** `COUNT(*)` e a soma de `LENGTH()` de todos os valores, feitos na
   leitura que a validação já faz (`estatisticas_tabela`). Não se usa o `TABLE_ROWS`/`DATA_LENGTH`
   do `information_schema`: no InnoDB são aproximações e incluem espaço vazio das páginas.
2. **Velocidade medida, não adivinhada.** `medir_amostra` copia uma amostra real das 3 maiores
   tabelas (até 20 000 linhas ou 3 s de leitura cada) pelo mesmo caminho da migração: `SELECT`
   no MySQL, `conversor`, `COPY` no PostgreSQL. Usa uma tabela normal, que escreve no WAL como a
   verdadeira, ou uma temporária se não houver permissão. A transação é sempre desfeita
   (`force_rollback`), por isso nada fica no destino. Mede separadamente:
   - o tempo de leitura e conversão;
   - o tempo de escrita;
   - o tempo de um `CREATE INDEX` sobre a amostra.
3. **Um custo por linha e outro por byte.** Uma tabela com milhões de linhas pequenas e outra com
   poucas linhas grandes (texto, blobs) não demoram o mesmo para os mesmos MB. `ajustar_custos`
   encontra `segundos = a·linhas + b·bytes` pelos mínimos quadrados sobre as amostras. Se só houver
   uma amostra, ou as larguras forem parecidas, escala os custos de referência
   (`CUSTO_LINHA_PADRAO`, `CUSTO_BYTE_PADRAO`) para baterem com o medido.

### Fórmulas por fase

| Fase | Estimativa |
|---|---|
| Cópia dos dados | soma por tabela de `a·linhas + b·bytes`, mais 0,03 s por tabela (CREATE TABLE, comentários, ANALYZE) |
| Chaves primárias e índices | `k · n·log2(n)` por índice e PK (custo de ordenar `n` valores); `k` vem do `CREATE INDEX` da amostra |
| FKs, triggers e objetos | `k · n·log2(n)` por FK (o PostgreSQL lê a filha e procura no índice do pai); ×2 com `--orfaos criar-pais` (procura de órfãos antes) |
| Verificação | 2,5 × o custo de ler e converter do MySQL: lê os dois lados, normaliza e compara (fator medido com 1,3 milhões de linhas); com `--sem-comparar-linhas`, só contagens |

O total aparece com uma margem de 0,6× a 1,8×. Durante a cópia, o tempo que falta é corrigido
pela velocidade real: `falta = (resto da tabela ao ritmo atual) + fator × (estimativa das tabelas
por copiar)`, em que `fator = tempo real / tempo estimado` das tabelas já copiadas.

### Resultado medido

Base sintética de 1,3 milhões de linhas (129 MB) em 3 tabelas, MySQL 8.4 e PostgreSQL 18 em
Docker na mesma máquina:

| Fase | Estimado | Real |
|---|---:|---:|
| Cópia dos dados | ~12–16 s | 11–14 s |
| Chaves primárias e índices | ~2 s | 1–3 s |
| FKs | ~1 s | 1–2 s |
| Verificação | ~23 s | 24–27 s |

### O que a estimativa não prevê

- Carga de outros programas nos servidores durante a migração, e redes que variam.
- Índices em colunas de texto com collation ICU que ignora maiúsculas (mais lentos a criar do que
  o índice numérico da amostra), FULLTEXT (GIN) e espaciais (GiST).
- Tabelas cujas primeiras linhas (as da amostra) sejam muito diferentes das restantes.
- A tradução de objetos e a criação de pais em falta, que dependem do conteúdo e não do volume.

### Linha de comando ou interface web

O tempo é o mesmo. A interface lança o mesmo `migrar.py` como subprocesso (`Execucao`) e só
reencaminha a saída para o navegador, cerca de 35 KB numa migração de 1,3 milhões de linhas.
Medido com a mesma base: linha de comando 42–44 s, web 40–42 s; a diferença está dentro da
variação entre execuções.

---

## 8. O que o sistema NÃO faz (limitações)

Verificado no código; se mudar, atualizar aqui.

**Âmbito da migração**

- **Não é sincronização contínua.** Não há CDC, binlog nem modo incremental: é uma cópia única.
  Para uma migração com a aplicação parada; o que for escrito no MySQL durante a cópia pode ou não entrar.
- **Sem snapshot consistente entre tabelas.** Cada tabela é lida numa ligação nova, sem
  `START TRANSACTION WITH CONSISTENT SNAPSHOT`. Com escritas na origem, tabelas diferentes podem
  refletir momentos diferentes.
- **Não migra utilizadores, senhas, `GRANT`s, `DEFINER`/`SQL SECURITY`** nem configuração do servidor.
- **Uma base de cada vez**, para um esquema de destino. FKs para outras bases são ignoradas (AVISO).
- **Objetos de código não respeitam `--tabelas` / `--excluir`**: views, procedures, funções e eventos
  da base são todos lidos (só os triggers são filtrados pela tabela). Uma view sobre uma tabela
  excluída falha na criação e vai para `_por_rever.sql`.
- **Não retoma** uma migração a meio: volta a correr com `--substituir`.

**Esquema**

- **Particionamento**: as tabelas particionadas migram como uma tabela normal; as partições não são recriadas.
- **`CHECK` do MySQL 8** (`information_schema.CHECK_CONSTRAINTS`) não são lidos. Só existem no
  destino os CHECK que o migrador cria (unsigned, ENUM).
- **Índices funcionais** (MySQL 8) não são migrados (AVISO).
- **Comentários de índices**, `ENGINE`, `ROW_FORMAT`, `AUTO_INCREMENT=` da tabela, opções de
  charset por tabela: ignorados. O contador `AUTO_INCREMENT` não é copiado; a sequência fica no `MAX(id)`.
- **ENUM** vira `varchar` + CHECK: perde-se a ordenação pelo índice do ENUM (`ORDER BY` passa a alfabético) e `enum + 0`.
- **SET** vira `text` sem validação (`FIND_IN_SET` tem de ser reescrito na aplicação).
- **`char(n)`** vira `varchar(n)`: o PostgreSQL não enche com espaços, mas o MySQL também os tira ao ler, por isso os dados ficam iguais.
- **`VIRTUAL`** passa a `STORED` (ocupa espaço).
- **Espaciais** só com PostGIS; sem ele é ERRO.
- **Ordem dos triggers**: no PostgreSQL disparam por ordem alfabética do nome, não por `FOLLOWS/PRECEDES` (INFO).

**Dados**

- Datas zero, parciais e inválidas passam a **NULL** (e a coluna aceita NULL). Não há opção para outro valor.
- `\0` em texto e `\u0000` em JSON são **removidos**. UTF-8 inválido é trocado por `�`.
- A leitura é sempre em `utf8mb4`: o servidor MySQL converte colunas `latin1` etc. Texto guardado
  com um charset errado (ex.: UTF-8 dentro de `latin1`, "mojibake") é copiado como o MySQL o devolve, sem correção.
- `float` é comparado a 6 algarismos significativos: diferenças abaixo disso não são detetadas.
- Espaços no fim em chaves PAD SPACE só são tratados com `--aparar-espacos` (e aí perdem-se).

**Objetos de código** (vão para `_por_rever.sql`)

- Cursores, handlers, condições, SQL dinâmico (`PREPARE`), `RESIGNAL`, variáveis `@sessao`, `SET` de sistema.
- Qualquer SQL que o sqlglot não saiba ler, ou que traduza para algo que o PostgreSQL recuse ao criar.
- **A tradução é sintática**: o PostgreSQL só valida o corpo PL/pgSQL quando o executa. Um objeto
  criado sem erro pode ainda falhar em tempo de execução (funções sem equivalente, tipos diferentes).
  A verificação confirma que o objeto existe e que as views devolvem os mesmos dados; não executa procedures nem triggers.
- Eventos `ONE TIME`, `EVERY n WEEK` (n > 1), `SECOND`, `YEAR`, `QUARTER`... ficam sem agendamento automático (só a procedure).
- `MATCH ... AGAINST` da aplicação não é traduzido: só o índice. A aplicação tem de usar `@@ plainto_tsquery`.

**Aplicação**

- O migrador não reescreve o SQL da aplicação (nomes reservados, `LIKE` vs `ILIKE`, `= '0000-00-00'`,
  `LIMIT a, b`, `INSERT IGNORE`, `ON DUPLICATE KEY UPDATE`, etc.). Os avisos indicam onde olhar.

---

## 9. Testar e estender

### Testar

Não há testes unitários; os testes são de ponta a ponta contra bases descartáveis. Os passos
completos estão no [guia](guia.html) (secção 8, "Como testar"). Em resumo:

```bash
# MySQL descartável com os casos difíceis
docker run -d --name mysql-teste -e MYSQL_ROOT_PASSWORD=teste -p 3307:3306 mysql:8.0
mysql --default-character-set=utf8mb4 -uroot -pteste -h127.0.0.1 -P3307 < testes/casos.sql

# PostgreSQL descartável (ou createdb num servidor local)
docker run -d --name pg-teste -e POSTGRES_PASSWORD=teste -p 5433:5432 postgres:18
docker exec pg-teste createdb -U postgres casos_teste

.venv/bin/python migrar.py --origem mysql://root:teste@127.0.0.1:3307/casos \
  --destino postgresql://postgres:teste@127.0.0.1:5433/casos_teste --sim --substituir
```

O resultado esperado é código `0` ("os dados no PostgreSQL são iguais aos do MySQL") com avisos.
`testes/casos.sql` cobre: datas zero/parciais/inválidas (`ALLOW_INVALID_DATES`), órfãos simples e
compostos, FK para coluna sem UNIQUE, tabela sem PK com linhas repetidas, unsigned acima de bigint,
bit(1)/bit(8)/bit(64), TIME > 24h e negativo, NUL em texto, `\u0000` em JSON, ENUM `''`, chaves PAD
SPACE com maiúsculas e espaços no fim, nomes reservados, colunas geradas STORED/VIRTUAL (uma com
`date_format`, que cai em trigger), `DEFAULT (expr)`, índices de prefixo sobre TEXT, FULLTEXT,
`ON UPDATE CURRENT_TIMESTAMP`, triggers (um com cursor → por rever), procedure, função, evento e
views. `testes/casos_espaciais.sql` precisa de PostGIS no destino (imagem `postgis/postgis`).

Para um caso novo: acrescentar ao `casos.sql` a tabela e os dados mínimos, migrar, e confirmar
no `.md` o achado esperado e a verificação a `OK`. Para testar a verificação, alterar um valor
no PostgreSQL e correr `--modo verificar`: tem de aparecer "Dados diferentes".

### Acrescentar um mapeamento de tipo

1. `Planeador.mapear_tipo`: devolver `(tipo_pg, conversao)` para o novo `tipo_dado`.
2. Se os valores precisarem de conversão, um ramo novo em `conversor()` para essa `conversao`
   (e, se o PostgreSQL devolver uma forma diferente na leitura, um caso em `normalizar()`).
3. Se precisar de olhar para os dados primeiro, um campo em `Estatisticas` e a expressão
   `SUM(...)` em `estatisticas_tabela`.
4. Se o tipo tiver uma família nova para FKs, `familia()`; para pais em falta, `valor_substituto`.
5. Se o default precisar de tradução, `traduzir_padrao`.
6. Um caso em `testes/casos.sql`.

### Acrescentar uma verificação

- **Fase 1** (antes de migrar): uma função `avisar_<algo>(esq, ..., rel)` chamada em `validar()`, que faz
  `rel.add("validacao", nivel, categoria, objeto, mensagem)`. Use ERRO só se a migração falhar
  ou ficar errada; o resto é AVISO ou INFO. Mensagens no estilo das existentes: o que acontece e o que fazer.
- **Fase 3** (depois): um método em `Verificador`, chamado em `executar_tudo`, com
  `rel.add("verificacao", ...)`. Um ERRO aqui faz o código de saída ser `1`.

### Acrescentar uma tradução de objeto

- Comando procedimental novo: um ramo em `Procedimento.comando`.
- Função ou expressão SQL: normalmente é o sqlglot; ajustes na árvore vão em `Tradutor._ajustar`
  (ou em `expressao_coluna`, se for só para colunas geradas/defaults).
- Se não houver tradução segura, `raise NaoSuportado("motivo")`: o objeto vai para `_por_rever.sql`
  com essa mensagem, em vez de ser criado errado.

### Acrescentar uma opção à interface

Acrescentar o argumento em `ler_argumentos` (`migrar.py`), a chave em `FLAGS`/`ESCOLHAS`/`TEXTOS`/`NUMEROS`
(`interface.py`) e o campo com `data-opcao` em `web/index.html`.

---

## 10. Notas para quem mantém

- **Python 3.10+**: o código não usa sintaxe mais recente (ex.: aspas iguais dentro de f-strings, PEP 701).
- A fase 1 lê as tabelas inteiras (estatísticas, órfãos, duplicados) e a fase 3 volta a lê-las.
  Em bases grandes, contar com pelo menos três leituras completas de cada tabela no MySQL.
- `Planeador.nome_unico` guarda os nomes já usados; as PKs (`<tabela>_pkey`) e os triggers recebem
  o nome na fase 2, por isso a ordem das chamadas afeta os sufixos `_2`, `_3`.
- Estado partilhado: `args` leva dados entre fases (`args.planeador`, `args.traducoes`,
  `args.postgis`, `args.usar_ci`, `args.versao_pg`, `args.pg_cron`). Ao mexer na ordem de `main()`, ter isto em conta.

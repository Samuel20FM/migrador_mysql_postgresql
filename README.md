# Samuel Massinga - 2026

# Migrador MySQL → PostgreSQL

Script que migra uma base MySQL para PostgreSQL em três fases:

1. **Validar.** Lê o esquema e os dados do MySQL e mostra as incongruências antes de mexer no destino.

2. **Migrar.** Cria as tabelas, copia os dados, cria chaves primárias e índices, e só no fim cria as FKs.

3. **Verificar.** Compara colunas, tipos, contagens, cada linha e cada campo, e o tamanho de cada coluna.

## Instalar

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # pymysql, psycopg, sqlglot
```

## Usar

```bash
# Tudo (validar + migrar + verificar). A senha é pedida no terminal.
.venv/bin/python migrar.py \
  --origem  mysql://migra@127.0.0.1:3306/g_processes \
  --destino postgresql://samuel@127.0.0.1:5432/g_processes

# Só ver os problemas, sem tocar no destino
.venv/bin/python migrar.py --modo validar --origem ... --destino ...

# Voltar a comparar uma migração já feita
.venv/bin/python migrar.py --modo verificar --origem ... --destino ...
```

Sem `--origem` ou `--destino`, o script pergunta host, porta, utilizador, base e senha.
Para a senha não ficar no histórico do shell, deixe-a fora do URL ou use as variáveis
`MIGRAR_ORIGEM` e `MIGRAR_DESTINO`.

Opções úteis: `--substituir` (recria tabelas que já existem), `--tabelas a,b`, `--excluir a,b`,
`--esquema outro`, `--orfaos not-valid`, `--comparacao-texto exata`, `--forcar` (migra mesmo com erros), `--sim` (sem confirmação). Para ver todas: `--help`.

O utilizador do MySQL só precisa de `SELECT` (e de `SHOW VIEW`, `TRIGGER` e `EVENT` para listar esses objetos).

## Relatórios

Cada execução grava em `relatorios/`:

- `migracao_<data>.md`: resumo legível;
- `migracao_<data>.json`: tudo, incluindo exemplos de linhas diferentes;
- `migracao_<data>.sql`: os comandos DDL executados no PostgreSQL.

Códigos de saída: `0` sem diferenças, `1` diferenças ou erros, `2` parado na validação, `3` sem ligação.

## Como as FKs são tratadas

As FKs são criadas depois dos dados, por isso nada bloqueia a cópia. Se houver registos órfãos
(um filho a apontar para um pai que não existe), o script, **antes de criar a FK, insere no pai os
registos com as chaves em falta**, e a FK fica válida (`--orfaos criar-pais`, o padrão).

- Os campos obrigatórios desses registos levam valores neutros: texto `REGISTO EM FALTA (criado na migração)`,
  números `0`, datas `1970-01-01`, o primeiro valor do ENUM, e, se o campo for FK, um registo que já exista.
  Campos únicos recebem valores que não se repetem.
- Cada registo criado fica anotado em `migracao.registos_criados` (tabela, chave e FK que o pediu):

  ```sql
  SELECT tabela, chave, motivo FROM migracao.registos_criados;
  ```
- A verificação final sabe quais são, e não os conta como diferenças.

Com `--orfaos not-valid` os dados ficam como estão e a FK é criada `NOT VALID`: existe no esquema
e protege os dados novos, mas não as linhas antigas. Depois de corrigir os órfãos:
`ALTER TABLE anexos VALIDATE CONSTRAINT anexos_ibfk_2;`

O pgloader faz o contrário: se a FK falha, não a cria, e a coluna fica como campo normal.

## Maiúsculas e acentos (WHERE igual ao MySQL)

No MySQL, as colunas com collation `_ci` (ex.: `utf8mb4_general_ci`) ignoram maiúsculas e acentos:
`WHERE email = 'ANA@X.COM'` encontra `ana@x.com`. No PostgreSQL, por defeito, não.

Por isso o script cria duas collations no esquema de destino e aplica-as às colunas de texto:

| MySQL | PostgreSQL |
|---|---|
| `*_ci`, `*_ai_ci` (general, unicode, 0900...) | `mysql_ci_ai`: ignora maiúsculas e acentos |
| `*_as_ci` | `mysql_ci`: ignora só maiúsculas |
| `*_bin`, `*_cs` | sem collation: comparação exata |

Com isto, `=`, `IN`, `LIKE`, `GROUP BY`, `DISTINCT`, `UNIQUE` e FKs comportam-se como no MySQL.

- Precisa do PostgreSQL 18 para `LIKE` (antes do 18, `LIKE` dá erro nestas colunas; o script avisa).
- `ILIKE` e expressões regulares (`~`) dão erro nestas colunas: use `LIKE`, que já ignora maiúsculas.
- Para a comparação normal do PostgreSQL ('Ana' ≠ 'ana'), use `--comparacao-texto exata`.

## O que o script valida

| Problema | O que acontece |
|---|---|
| Registos órfãos (texto comparado como no PostgreSQL vai comparar) | pais em falta criados e FK válida (ou `NOT VALID` com `--orfaos not-valid`) |
| FK para colunas sem índice único | cria `UNIQUE` se os dados forem únicos; senão é erro |
| FK com tipos incompatíveis | erro |
| Datas `0000-00-00` | passam a NULL (e a coluna aceita NULL) |
| `tinyint(1)` com valores além de 0/1 | fica `smallint` em vez de `boolean` |
| Carácter `\0` em texto | é removido |
| `TIME` negativo ou acima de 24h | fica `interval` |
| ENUM com `''` inválido | `''` entra no CHECK |
| Tipos espaciais | PostGIS (`geometry`); sem PostGIS no servidor é erro |
| Tabelas sem chave primária | comparadas como conjunto de linhas |
| Nomes com mais de 63 bytes ou repetidos em minúsculas | erro |
| Tabelas que já existem no destino | erro (ou aviso com `--substituir`) |
| Triggers, views, procedures, funções, eventos | traduzidos para PL/pgSQL; o que não der vai para `*_por_rever.sql` |
| `tinyint(1)` com nome `*_id` ou em FK | fica `smallint` (é um número, não um booleano) |

## Triggers, procedures, funções, eventos e views

São traduzidos automaticamente (`objetos.py`) e criados no fim, depois dos dados e das FKs:

| MySQL | PostgreSQL |
|---|---|
| `CREATE TRIGGER ... BEGIN ... END` | função `<trigger>_fn()` em PL/pgSQL + `CREATE TRIGGER` |
| `PROCEDURE` / `FUNCTION` | `PROCEDURE` / `FUNCTION ... LANGUAGE plpgsql` |
| `EVENT ... EVERY 1 DAY` | procedure `evento_<nome>()` + agendamento no pg_cron (se existir) ou linha de crontab no relatório |
| `VIEW` | `CREATE VIEW` com o SQL traduzido |
| `DECLARE`, `SET`, `IF/ELSEIF`, `CASE`, `WHILE`, `REPEAT`, `LOOP`, `LEAVE`, `ITERATE` | equivalentes em PL/pgSQL |
| `SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = ...` | `RAISE EXCEPTION USING ERRCODE = '45000', MESSAGE = ...` |
| `IFNULL`, `<=>`, `CONCAT`, `IF()`, `DATE_ADD`, ... | via [sqlglot](https://github.com/tobymao/sqlglot) |
| `estado = 1` numa coluna que passou a boolean | `estado = TRUE` |

Cursores, handlers (`DECLARE ... HANDLER`), variáveis `@sessao` e SQL dinâmico (`PREPARE`) não têm tradução
segura: o objeto vai para `relatorios/migracao_<data>_por_rever.sql`, com o original e o motivo.

A verificação confirma que cada objeto existe no PostgreSQL e que cada view devolve as mesmas linhas que no MySQL.
Use `--sem-objetos` para não traduzir nada disto.

## Índices FULLTEXT e colunas espaciais

- **FULLTEXT** → índice GIN sobre `to_tsvector('simple', coalesce(col, ''))`. As pesquisas `MATCH ... AGAINST`
  da aplicação passam a `to_tsvector('simple', coalesce(col, '')) @@ plainto_tsquery('simple', 'texto')`.
- **Espaciais** (`point`, `polygon`, `geometry`...) → PostGIS `geometry(Point,4326)` etc., com o SRID e a ordem
  longitude/latitude certos, e índices `SPATIAL` → GiST. Precisa do PostGIS instalado no servidor PostgreSQL
  (`apt install postgresql-18-postgis-3`). Teste: `testes/casos_espaciais.sql`.

## Conversões

| MySQL | PostgreSQL |
|---|---|
| `AUTO_INCREMENT` | `GENERATED BY DEFAULT AS IDENTITY`, com a sequência acertada para o maior id |
| `tinyint(1)` | `boolean` |
| `int unsigned` / `bigint unsigned` | `bigint` / `numeric(20)` |
| `datetime` / `timestamp` | `timestamp` / `timestamptz` |
| `enum(...)` | `varchar` + `CHECK (... IN (...))` |
| `set`, `text` | `text` |
| `json` | `jsonb` |
| `blob`, `binary` | `bytea` |
| `ON UPDATE CURRENT_TIMESTAMP` | trigger `migr_on_update_timestamp` |
| índices | `<tabela>_<índice>` (no PostgreSQL o nome é único por esquema) |

## Testes

`testes/casos.sql` cria no MySQL uma base `casos` com os casos difíceis (datas zero, órfãos, FK composta,
chave de texto com maiúsculas, unsigned, JSON, BLOB, BIT, TIME > 24h, NUL, etc.):

```bash
docker run -d --name mysql-teste -e MYSQL_ROOT_PASSWORD=teste -p 3307:3306 mysql:8.0
mysql --default-character-set=utf8mb4 -uroot -pteste -h127.0.0.1 -P3307 < testes/casos.sql
createdb casos_teste
.venv/bin/python migrar.py --origem mysql://root:teste@127.0.0.1:3307/casos \
  --destino postgresql://samuel@127.0.0.1:5432/casos_teste --sim
```

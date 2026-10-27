#!/usr/bin/env python3
"""Migração MySQL -> PostgreSQL com validação prévia e verificação final.

Fases:
  1. validar   - lê o esquema e os dados do MySQL e lista as incongruências
  2. migrar    - cria as tabelas, copia os dados, cria índices e só no fim as FKs
  3. verificar - compara colunas, contagens, cada linha e cada campo

As FKs são criadas depois dos dados. Se houver registos órfãos (filho a apontar
para um pai que não existe), o script insere antes no pai os registos com as
chaves em falta (--orfaos criar-pais, o padrão) e a FK fica válida. Esses
registos ficam anotados em migracao.registos_criados.

As colunas de texto com collation _ci no MySQL recebem no PostgreSQL uma
collation que também ignora maiúsculas e acentos, para WHERE, LIKE, UNIQUE e
FKs se comportarem como no MySQL (--comparacao-texto mysql, o padrão).

Triggers, procedures, funções, eventos e views são traduzidos para PL/pgSQL
(ver objetos.py); o que não tiver tradução segura vai para *_por_rever.sql.
Índices FULLTEXT viram GIN e colunas espaciais usam PostGIS (se existir).

Exemplo:
  python migrar.py --origem mysql://user@127.0.0.1:3306/base \
                   --destino postgresql://user@127.0.0.1:5432/base
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import getpass
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

try:
    import psycopg
    import pymysql
    import pymysql.cursors
    from psycopg import sql

    import objetos
    from objetos import NaoSuportado
except ImportError as e:  # pragma: no cover
    sys.exit(f"Falta uma dependência ({e.name}). Instale com: pip install -r requirements.txt")

UTC = dt.timezone.utc
BIGINT_MAX = 9223372036854775807

# --------------------------------------------------------------------------- #
# Saída no terminal
# --------------------------------------------------------------------------- #
COR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_CORES = {"ERRO": "31", "AVISO": "33", "INFO": "36", "OK": "32", "TITULO": "1"}


def cor(texto: str, tipo: str) -> str:
    return f"\033[{_CORES[tipo]}m{texto}\033[0m" if COR else texto


def progresso(texto: str) -> None:
    if sys.stdout.isatty():
        print(f"\r{texto}", end="", flush=True)


def titulo(texto: str) -> None:
    print("\n" + cor(f"== {texto} ==", "TITULO"))


# --------------------------------------------------------------------------- #
# Conexões
# --------------------------------------------------------------------------- #
@dataclass
class DadosConexao:
    host: str
    porta: int
    utilizador: str
    senha: str
    base: str

    def descricao(self) -> str:
        return f"{self.utilizador}@{self.host}:{self.porta}/{self.base}"


def ler_conexao(url: str | None, nome: str, porta_padrao: int, esquemas: tuple[str, ...]) -> DadosConexao:
    """Lê os dados de conexão de um URL ou pergunta-os no terminal."""
    if not url:
        print(f"\nDados de conexão — {nome}")
        host = input("  Host [127.0.0.1]: ").strip() or "127.0.0.1"
        porta = int(input(f"  Porta [{porta_padrao}]: ").strip() or porta_padrao)
        utilizador = input("  Utilizador: ").strip()
        base = input("  Base de dados: ").strip()
        senha = getpass.getpass("  Senha: ")
        dados = DadosConexao(host, porta, utilizador, senha, base)
    else:
        u = urlparse(url)
        if u.scheme not in esquemas:
            sys.exit(f"URL de {nome} tem de começar por {' ou '.join(s + '://' for s in esquemas)}")
        utilizador = unquote(u.username or "")
        senha = unquote(u.password) if u.password is not None else getpass.getpass(
            f"Senha de {utilizador}@{u.hostname} ({nome}): ")
        dados = DadosConexao(u.hostname or "127.0.0.1", u.port or porta_padrao, utilizador, senha,
                             unquote(u.path.lstrip("/")))
    if not dados.base:
        sys.exit(f"Falta o nome da base de dados de {nome}.")
    return dados


def ligar_mysql(c: DadosConexao):
    con = pymysql.connect(host=c.host, port=c.porta, user=c.utilizador, password=c.senha,
                          database=c.base, charset="utf8mb4", autocommit=True)
    with con.cursor() as cur:
        # UTC para TIMESTAMP sair igual ao que está gravado; sql_mode vazio para ler datas zero.
        cur.execute("SET SESSION time_zone = '+00:00', SESSION sql_mode = ''")
    return con


def ligar_pg(c: DadosConexao):
    con = psycopg.connect(host=c.host, port=c.porta, user=c.utilizador, password=c.senha,
                          dbname=c.base, autocommit=True)
    con.execute("SET TIME ZONE 'UTC'")
    return con


def detalhe_erro(e: Exception) -> str:
    """Linha DETAIL do erro do PostgreSQL, sem o prefixo e o ponto final."""
    linhas = str(e).strip().splitlines()
    linha = next((l for l in linhas if l.startswith("DETAIL:")), linhas[-1])
    return linha.removeprefix("DETAIL:").strip().rstrip(".")


def qm(nome: str) -> str:
    """Identificador MySQL entre acentos graves."""
    return "`" + nome.replace("`", "``") + "`"


# --------------------------------------------------------------------------- #
# Modelo do esquema
# --------------------------------------------------------------------------- #
@dataclass
class Coluna:
    nome: str
    tipo_dado: str
    tipo_coluna: str
    nula: bool
    padrao: str | None
    extra: str
    tamanho: int | None
    precisao: int | None
    escala: int | None
    prec_tempo: int | None
    comentario: str
    colacao: str = ""
    srid: int | None = None
    # Decidido no plano
    nome_pg: str = ""
    colacao_pg: str | None = None
    tipo_pg: str | None = None
    conversao: str = ""
    nula_pg: bool = True
    padrao_pg: sql.Composable | None = None
    identidade: bool = False
    valores_enum: list[str] = field(default_factory=list)
    check_enum: bool = False

    @property
    def unsigned(self) -> bool:
        return "unsigned" in self.tipo_coluna.lower()

    @property
    def auto(self) -> bool:
        return "auto_increment" in self.extra.lower()

    @property
    def on_update(self) -> bool:
        return "on update" in self.extra.lower()

    @property
    def gerada(self) -> bool:
        e = self.extra.upper()
        return "VIRTUAL GENERATED" in e or "STORED GENERATED" in e


@dataclass
class Indice:
    nome: str
    colunas: list[str]
    unico: bool
    prefixo: bool
    tipo: str
    nome_pg: str = ""


@dataclass
class ChaveEstrangeira:
    nome: str
    tabela: str
    colunas: list[str]
    base_ref: str
    tabela_ref: str
    colunas_ref: list[str]
    on_update: str
    on_delete: str
    nome_pg: str = ""
    orfaos: int = 0
    ignorar: bool = False


@dataclass
class Tabela:
    nome: str
    colunas: list[Coluna]
    pk: list[str]
    indices: list[Indice]
    fks: list[ChaveEstrangeira]
    comentario: str
    linhas_estimadas: int
    tamanho_bytes: int
    nome_pg: str = ""

    def coluna(self, nome: str) -> Coluna:
        for c in self.colunas:
            if c.nome == nome:
                return c
        raise KeyError(nome)


@dataclass
class EsquemaMySQL:
    versao: str
    tabelas: dict[str, Tabela]
    objetos: objetos.Objetos

    @property
    def mariadb(self) -> bool:
        return "mariadb" in self.versao.lower()


def ler_esquema_mysql(con, base: str, incluir: set[str] | None, excluir: set[str]) -> EsquemaMySQL:
    cur = con.cursor()
    cur.execute("SELECT VERSION()")
    versao = cur.fetchone()[0]

    def consultar(q: str):
        cur.execute(q, (base,))
        return cur.fetchall()

    tabelas: dict[str, Tabela] = {}
    for nome, comentario, linhas, tamanho in consultar(
            "SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS, COALESCE(DATA_LENGTH,0)+COALESCE(INDEX_LENGTH,0) "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"):
        if (incluir and nome not in incluir) or nome in excluir:
            continue
        tabelas[nome] = Tabela(nome, [], [], [], [], comentario or "", int(linhas or 0), int(tamanho or 0))

    for (t, nome, padrao, nula, tipo_dado, tipo_coluna, tamanho, precisao, escala, prec_tempo, extra,
         comentario, colacao) in consultar(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_DEFAULT, IS_NULLABLE, DATA_TYPE, COLUMN_TYPE, "
            "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION, EXTRA, COLUMN_COMMENT, "
            "COLLATION_NAME "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME, ORDINAL_POSITION"):
        if t in tabelas:
            tabelas[t].colunas.append(Coluna(
                nome, tipo_dado.lower(), tipo_coluna, nula == "YES", padrao, extra or "",
                int(tamanho) if tamanho is not None else None,
                int(precisao) if precisao is not None else None,
                int(escala) if escala is not None else None,
                int(prec_tempo) if prec_tempo is not None else None, comentario or "", colacao or ""))

    indices: dict[tuple[str, str], Indice] = {}
    for t, nome, nao_unico, coluna, sub_part, tipo in consultar(
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SUB_PART, INDEX_TYPE "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"):
        if t not in tabelas:
            continue
        if coluna is None:  # índice funcional (MySQL 8): não há coluna simples
            indices[(t, nome)] = Indice(nome, [], int(nao_unico) == 0, False, "FUNCIONAL")
            continue
        if nome == "PRIMARY":
            tabelas[t].pk.append(coluna)
            continue
        ind = indices.setdefault((t, nome), Indice(nome, [], int(nao_unico) == 0, False, tipo or "BTREE"))
        if ind.tipo != "FUNCIONAL":
            ind.colunas.append(coluna)
            ind.prefixo = ind.prefixo or sub_part is not None
    for (t, _), ind in indices.items():
        tabelas[t].indices.append(ind)

    fks: dict[tuple[str, str], ChaveEstrangeira] = {}
    for nome, t, coluna, base_ref, t_ref, c_ref, upd, dele in consultar(
            "SELECT k.CONSTRAINT_NAME, k.TABLE_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, "
            "k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.UPDATE_RULE, r.DELETE_RULE "
            "FROM information_schema.KEY_COLUMN_USAGE k "
            "JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA "
            " AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME AND r.TABLE_NAME=k.TABLE_NAME "
            "WHERE k.TABLE_SCHEMA=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION"):
        if t not in tabelas:
            continue
        fk = fks.setdefault((t, nome), ChaveEstrangeira(nome, t, [], base_ref, t_ref, [], upd, dele))
        fk.colunas.append(coluna)
        fk.colunas_ref.append(c_ref)
    for (t, _), fk in fks.items():
        tabelas[t].fks.append(fk)

    # SRID das colunas espaciais (só existe no MySQL 8)
    try:
        for t, nome, srid in consultar("SELECT TABLE_NAME, COLUMN_NAME, SRS_ID FROM information_schema.COLUMNS "
                                       "WHERE TABLE_SCHEMA=%s AND SRS_ID IS NOT NULL"):
            if t in tabelas:
                tabelas[t].coluna(nome).srid = int(srid)
    except pymysql.Error:
        pass
    cur.close()
    return EsquemaMySQL(versao, tabelas, objetos.ler_objetos(con, base, set(tabelas)))


# --------------------------------------------------------------------------- #
# Relatório
# --------------------------------------------------------------------------- #
class Relatorio:
    def __init__(self) -> None:
        self.inicio = dt.datetime.now()
        self.achados: list[dict] = []
        self.migracao: dict[str, dict] = {}
        self.verificacao: dict[str, dict] = {}
        self.ddl: list[str] = []
        self.resumo: dict = {}
        self.traducoes: list = []

    def add(self, fase: str, nivel: str, categoria: str, objeto: str, mensagem: str) -> None:
        self.achados.append(dict(fase=fase, nivel=nivel, categoria=categoria, objeto=objeto, mensagem=mensagem))

    def contar(self, fase: str, nivel: str) -> int:
        return sum(1 for a in self.achados if a["fase"] == fase and a["nivel"] == nivel)

    def imprimir_achados(self, fase: str, max_por_categoria: int = 15) -> None:
        lista = [a for a in self.achados if a["fase"] == fase]
        if not lista:
            print(cor("  Sem incongruências.", "OK"))
            return
        for nivel in ("ERRO", "AVISO", "INFO"):
            por_cat: dict[str, list[dict]] = {}
            for a in lista:
                if a["nivel"] == nivel:
                    por_cat.setdefault(a["categoria"], []).append(a)
            for cat, itens in por_cat.items():
                print(f"\n  {cor(nivel, nivel)} · {cat} ({len(itens)})")
                for a in itens[:max_por_categoria]:
                    print(f"    - {a['objeto']}: {a['mensagem']}")
                if len(itens) > max_por_categoria:
                    print(f"    ... e mais {len(itens) - max_por_categoria} (ver relatório)")

    def gravar(self, pasta: str) -> tuple[str, str]:
        os.makedirs(pasta, exist_ok=True)
        base = os.path.join(pasta, "migracao_" + self.inicio.strftime("%Y%m%d_%H%M%S"))
        dados = dict(inicio=self.inicio.isoformat(timespec="seconds"),
                     fim=dt.datetime.now().isoformat(timespec="seconds"), resumo=self.resumo,
                     achados=self.achados, migracao=self.migracao, verificacao=self.verificacao)
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2, default=str)
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write(self._markdown(dados))
        if self.ddl:
            with open(base + ".sql", "w", encoding="utf-8") as f:
                f.write(";\n\n".join(self.ddl) + ";\n")
        por_rever = [d for d in self.traducoes if d.erro]
        if por_rever:
            with open(base + "_por_rever.sql", "w", encoding="utf-8") as f:
                f.write("-- Objetos que precisam de revisão manual antes de os criar no PostgreSQL.\n\n")
                for d in por_rever:
                    f.write(f"-- ==== {d.tipo}: {d.alvo} ====\n-- Motivo: {d.erro}\n-- Original (MySQL):\n")
                    f.write("\n".join("--   " + l for l in d.original.splitlines()) + "\n")
                    if d.comandos:
                        f.write("-- Tradução automática (rever):\n" + ";\n".join(d.comandos) + ";\n")
                    f.write("\n")
        return base + ".md", base + ".json"

    def _markdown(self, d: dict) -> str:
        linhas = [f"# Relatório de migração MySQL → PostgreSQL", "",
                  f"Início: {d['inicio']} · Fim: {d['fim']}", ""]
        for k, v in d["resumo"].items():
            linhas.append(f"- **{k}:** {v}")
        for fase in ("validacao", "migracao", "verificacao"):
            itens = [a for a in d["achados"] if a["fase"] == fase]
            if not itens:
                continue
            linhas += ["", f"## Achados — {fase}", "", "| Nível | Categoria | Objeto | Mensagem |", "|---|---|---|---|"]
            for a in itens:
                msg = a["mensagem"].replace("|", "\\|").replace("\n", " ")
                linhas.append(f"| {a['nivel']} | {a['categoria']} | `{a['objeto']}` | {msg} |")
        if d["verificacao"]:
            linhas += ["", "## Verificação por tabela", "",
                       "| Tabela | Linhas MySQL | Linhas PG | Iguais | Diferentes | Em falta | A mais "
                       "| Criadas p/ FK | Estado |",
                       "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
            for t, v in d["verificacao"].items():
                linhas.append(f"| {t} | {v.get('linhas_mysql')} | {v.get('linhas_pg')} | {v.get('iguais', '-')} | "
                              f"{v.get('diferentes', '-')} | {v.get('em_falta', '-')} | {v.get('a_mais', '-')} | "
                              f"{v.get('criados', 0)} | "
                              f"{'OK' if v.get('ok') else 'FALHOU'} |")
        return "\n".join(linhas) + "\n"


# --------------------------------------------------------------------------- #
# Fase 1 — validação
# --------------------------------------------------------------------------- #
TIPOS_TEXTO = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}
TIPOS_BINARIOS = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}
TIPOS_DATA = {"date", "datetime", "timestamp"}
TIPOS_INTEIROS = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
TIPOS_ESPACIAIS = {"geometry": "", "point": "Point", "linestring": "LineString", "polygon": "Polygon",
                   "multipoint": "MultiPoint", "multilinestring": "MultiLineString", "multipolygon": "MultiPolygon",
                   "geometrycollection": "GeometryCollection", "geomcollection": "GeometryCollection"}


@dataclass
class Estatisticas:
    """Contagens tiradas dos dados de uma coluna, usadas para decidir a conversão."""
    datas_zero: int = 0
    fora_bool: int = 0
    nul_bytes: int = 0
    enum_vazio: int = 0
    hora_fora: int = 0
    acima_bigint: int = 0


def valores_enum(tipo_coluna: str) -> list[str]:
    corpo = tipo_coluna[tipo_coluna.index("(") + 1: tipo_coluna.rindex(")")]
    return [v.replace("''", "'").replace("\\\\", "\\") for v in re.findall(r"'((?:[^']|'')*)'", corpo)]


def estatisticas_tabela(con, t: Tabela, usar_bool: bool) -> dict[str, Estatisticas]:
    """Uma única leitura completa da tabela com todas as contagens necessárias."""
    exprs: list[tuple[str, str, str]] = []
    for c in t.colunas:
        q = qm(c.nome)
        if c.tipo_dado in TIPOS_DATA:
            exprs.append((c.nome, "datas_zero",
                          f"SUM({q} IS NOT NULL AND (YEAR({q})=0 OR MONTH({q})=0 OR DAYOFMONTH({q})=0))"))
        elif c.tipo_dado == "tinyint" and usar_bool and c.tipo_coluna.lower().startswith("tinyint(1)"):
            exprs.append((c.nome, "fora_bool", f"SUM({q} NOT IN (0,1))"))
        elif c.tipo_dado in TIPOS_TEXTO or c.tipo_dado in ("set", "json"):
            exprs.append((c.nome, "nul_bytes", f"SUM(INSTR(CAST({q} AS BINARY), CHAR(0)) > 0)"))
        elif c.tipo_dado == "enum" and "" not in valores_enum(c.tipo_coluna):
            exprs.append((c.nome, "enum_vazio", f"SUM({q} = '')"))
        elif c.tipo_dado == "time":
            exprs.append((c.nome, "hora_fora", f"SUM({q} < '00:00:00' OR {q} >= '24:00:00')"))
        elif c.tipo_dado == "bigint" and c.unsigned:
            exprs.append((c.nome, "acima_bigint", f"SUM({q} > {BIGINT_MAX})"))
    resultado = {c.nome: Estatisticas() for c in t.colunas}
    if not exprs:
        return resultado
    with con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(e for _, _, e in exprs)} FROM {qm(t.nome)}")
        valores = cur.fetchone()
    for (col, campo, _), v in zip(exprs, valores):
        setattr(resultado[col], campo, int(v or 0))
    return resultado


def familia(tipo_pg: str | None) -> str:
    if tipo_pg is None:
        return "?"
    t = tipo_pg.split("(")[0].strip()
    if t in ("smallint", "integer", "bigint", "numeric", "real", "double precision"):
        return "numero"
    if t in ("varchar", "text", "char"):
        return "texto"
    if t.startswith("timestamp") or t == "date":
        return "data"
    return t


# Collations do PostgreSQL que imitam as "_ci" do MySQL (ICU, não determinísticas).
# Com elas, '=', LIKE (PostgreSQL 18+), UNIQUE e FKs ignoram maiúsculas como no MySQL.
COLACOES_PG = {
    "mysql_ci_ai": "und-u-ks-level1",  # ignora maiúsculas e acentos (utf8mb4_general_ci, _0900_ai_ci...)
    "mysql_ci": "und-u-ks-level2",     # ignora só maiúsculas (_as_ci)
}


def colacao_equivalente(colacao_mysql: str) -> str | None:
    """Collation do PostgreSQL com a mesma regra de comparação; None = comparação exata."""
    c = colacao_mysql.lower()
    if not c or c.endswith("_bin") or "_cs" in c:
        return None
    if "_as_ci" in c:
        return "mysql_ci"
    if c.endswith("_ci"):
        return "mysql_ci_ai"
    return None


class Planeador:
    """Decide tipos, valores por defeito e nomes no PostgreSQL."""

    def __init__(self, args, rel: Relatorio, mariadb: bool) -> None:
        self.args = args
        self.rel = rel
        self.mariadb = mariadb
        self.nomes_usados: set[str] = set()
        self.colunas_ci = 0
        self.colunas_fk: set[tuple[str, str]] = set()
        self.tabela_atual = ""

    def nome_pg(self, nome: str) -> str:
        return nome if self.args.manter_maiusculas else nome.lower()

    def nome_unico(self, base: str) -> str:
        """Nomes de índices/constraints: até 63 bytes e únicos no esquema."""
        n = base
        if len(n.encode()) > 63:
            n = n.encode()[:54].decode("utf-8", "ignore") + "_" + hashlib.md5(base.encode()).hexdigest()[:8]
        final, i = n, 2
        while final in self.nomes_usados:
            sufixo = f"_{i}"
            final = n.encode()[:63 - len(sufixo)].decode("utf-8", "ignore") + sufixo
            i += 1
        self.nomes_usados.add(final)
        return final

    def mapear_tipo(self, c: Coluna, est: Estatisticas) -> tuple[str | None, str]:
        t, ct = c.tipo_dado, c.tipo_coluna.lower()
        if t == "tinyint":
            if not self.args.sem_booleanos and ct.startswith("tinyint(1)") and est.fora_bool == 0 \
                    and not self.parece_numero(c):
                return "boolean", "bool"
            return "smallint", ""
        if t == "smallint":
            return ("integer" if c.unsigned else "smallint"), ""
        if t == "mediumint":
            return "integer", ""
        if t in ("int", "integer"):
            return ("bigint" if c.unsigned else "integer"), ""
        if t == "bigint":
            return ("numeric(20)" if c.unsigned and not c.auto else "bigint"), ""
        if t in ("decimal", "numeric"):
            return f"numeric({c.precisao},{c.escala or 0})", ""
        if t == "float":
            return "real", ""
        if t in ("double", "real"):
            return "double precision", ""
        if t == "bit":
            return ("boolean", "bit1") if (c.precisao or 1) == 1 else ("bigint", "bit")
        if t in ("char", "varchar"):
            return f"varchar({c.tamanho})", "texto"
        if t in TIPOS_TEXTO:
            return "text", "texto"
        if t in TIPOS_BINARIOS:
            return "bytea", "bytes"
        if t == "date":
            return "date", "data"
        if t == "datetime":
            return (f"timestamp({c.prec_tempo})" if c.prec_tempo else "timestamp"), "data"
        if t == "timestamp":
            return (f"timestamptz({c.prec_tempo})" if c.prec_tempo else "timestamptz"), "data_tz"
        if t == "time":
            if est.hora_fora:
                return "interval", ""
            return (f"time({c.prec_tempo})" if c.prec_tempo else "time"), "hora"
        if t == "year":
            return "smallint", ""
        if t == "json":
            return "jsonb", "texto"
        if t == "enum":
            vals = valores_enum(c.tipo_coluna)
            return f"varchar({max([len(v) for v in vals] + [1])})", "texto"
        if t == "set":
            return "text", "texto"
        if t in TIPOS_ESPACIAIS and self.args.postgis:
            sub = TIPOS_ESPACIAIS[t]
            if c.srid is not None:
                return f"geometry({sub or 'Geometry'},{c.srid})", "geom"
            return (f"geometry({sub})" if sub else "geometry"), "geom"
        return None, ""

    def parece_numero(self, c: Coluna) -> bool:
        """tinyint(1) que é um id ou faz parte de uma FK não deve virar boolean."""
        return (c.nome.lower().endswith("_id") or c.nome.lower() == "id"
                or (self.tabela_atual, c.nome) in self.colunas_fk)

    def traduzir_padrao(self, tabela: str, c: Coluna) -> sql.Composable | None:
        d = c.padrao
        if d is None:
            return None
        if self.mariadb:
            if d.upper() == "NULL":
                return None
            if len(d) >= 2 and d[0] == d[-1] == "'":
                d = d[1:-1].replace("''", "'")
        up = d.upper().strip()
        alvo = f"{tabela}.{c.nome}"
        if up.startswith("CURRENT_TIMESTAMP") or up in ("NOW()", "LOCALTIMESTAMP", "LOCALTIMESTAMP()"):
            return sql.SQL("CURRENT_DATE" if c.tipo_pg == "date" else "CURRENT_TIMESTAMP")
        if up in ("CURDATE()", "CURRENT_DATE", "CURRENT_DATE()"):
            return sql.SQL("CURRENT_DATE")
        if "DEFAULT_GENERATED" in c.extra.upper():
            self.rel.add("validacao", "AVISO", "Valor por defeito não convertido", alvo,
                         f"expressão {d!r} não tem tradução automática; recrie-a à mão.")
            return None
        if c.conversao in ("bool", "bit1"):
            if d in ("0", "b'0'"):
                return sql.SQL("FALSE")
            if d in ("1", "b'1'"):
                return sql.SQL("TRUE")
        if c.conversao == "bit" and d.startswith("b'"):
            return sql.SQL(str(int(d[2:-1] or "0", 2)))
        if c.conversao in ("data", "data_tz"):
            if d.startswith("0000-00-00"):
                self.rel.add("validacao", "AVISO", "Valor por defeito removido", alvo,
                             f"data zero {d!r} não existe no PostgreSQL.")
                return None
            return sql.Literal(d)
        if familia(c.tipo_pg) == "numero":
            try:
                return sql.SQL(str(decimal.Decimal(d)))
            except decimal.InvalidOperation:
                self.rel.add("validacao", "AVISO", "Valor por defeito removido", alvo, f"{d!r} não é número.")
                return None
        if c.conversao == "bytes":
            return sql.Literal(d.encode())
        return sql.Literal(d)

    def planear(self, esq: EsquemaMySQL, ests: dict[str, dict[str, Estatisticas]]) -> None:
        for t in esq.tabelas.values():
            for fk in t.fks:
                self.colunas_fk.update((t.nome, c) for c in fk.colunas)
                self.colunas_fk.update((fk.tabela_ref, c) for c in fk.colunas_ref)
        for t in esq.tabelas.values():
            t.nome_pg = self.nome_pg(t.nome)
            self.nomes_usados.add(t.nome_pg)
        renomeados = 0
        for t in esq.tabelas.values():
            self.tabela_atual = t.nome
            for c in t.colunas:
                est = ests[t.nome][c.nome]
                alvo = f"{t.nome}.{c.nome}"
                c.nome_pg = self.nome_pg(c.nome)
                c.tipo_pg, c.conversao = self.mapear_tipo(c, est)
                c.nula_pg = c.nula
                if c.tipo_pg is None:
                    if c.tipo_dado not in TIPOS_ESPACIAIS:  # espaciais sem PostGIS já têm o seu próprio erro
                        self.rel.add("validacao", "ERRO", "Tipo não suportado", alvo,
                                     f"tipo {c.tipo_coluna} sem conversão.")
                    continue
                if self.args.usar_ci and familia(c.tipo_pg) == "texto":
                    c.colacao_pg = colacao_equivalente(c.colacao)
                    if c.colacao_pg:
                        self.colunas_ci += 1
                if c.auto and c.tipo_dado in TIPOS_INTEIROS:
                    c.identidade = True
                    if c.tipo_pg not in ("smallint", "integer", "bigint"):
                        c.tipo_pg = "bigint"
                    if est.acima_bigint:
                        self.rel.add("validacao", "ERRO", "Valor fora do limite", alvo,
                                     f"{est.acima_bigint} valor(es) acima do máximo de bigint.")
                if c.tipo_dado == "tinyint" and est.fora_bool:
                    self.rel.add("validacao", "AVISO", "tinyint(1) com valores além de 0/1", alvo,
                                 f"{est.fora_bool} linha(s); a coluna fica smallint em vez de boolean.")
                elif c.tipo_coluna.lower().startswith("tinyint(1)") and c.tipo_pg == "smallint" \
                        and not self.args.sem_booleanos and self.parece_numero(c):
                    self.rel.add("validacao", "INFO", "tinyint(1) mantido como número", alvo,
                                 "o nome termina em _id ou a coluna faz parte de uma FK; fica smallint, não boolean.")
                if c.conversao == "geom":
                    self.rel.add("validacao", "INFO", "Coluna espacial (PostGIS)", alvo,
                                 f"{c.tipo_coluna} → {c.tipo_pg}.")
                if est.datas_zero:
                    extra = ""
                    if not c.nula:
                        c.nula_pg = True
                        extra = " A coluna é NOT NULL no MySQL e fica a aceitar NULL."
                    self.rel.add("validacao", "AVISO", "Datas zero (0000-00-00)", alvo,
                                 f"{est.datas_zero} linha(s) passam a NULL.{extra}")
                if est.nul_bytes:
                    self.rel.add("validacao", "AVISO", "Carácter NUL (\\0) em texto", alvo,
                                 f"{est.nul_bytes} linha(s); o PostgreSQL não aceita \\0 em texto e ele será removido.")
                if est.hora_fora:
                    self.rel.add("validacao", "AVISO", "TIME fora de 00:00–24:00", alvo,
                                 f"{est.hora_fora} linha(s); a coluna fica interval em vez de time.")
                if c.tipo_dado == "enum":
                    c.valores_enum = valores_enum(c.tipo_coluna)
                    c.check_enum = not self.args.enum_sem_check
                    if est.enum_vazio:
                        c.valores_enum.append("")
                        self.rel.add("validacao", "AVISO", "ENUM com valor inválido ''", alvo,
                                     f"{est.enum_vazio} linha(s) com ''; '' foi aceite no CHECK para não perder dados.")
                if c.tipo_dado == "set":
                    self.rel.add("validacao", "INFO", "SET convertido para texto", alvo,
                                 "valores ficam separados por vírgula, sem validação.")
                if c.gerada:
                    self.rel.add("validacao", "AVISO", "Coluna gerada", alvo,
                                 "fica como coluna normal com os valores atuais; recrie a fórmula se precisar.")
                if c.tipo_dado == "bigint" and c.unsigned and not c.auto:
                    self.rel.add("validacao", "INFO", "bigint unsigned", alvo, "fica numeric(20) para caber o intervalo.")
                if len(c.nome_pg.encode()) > 63:
                    self.rel.add("validacao", "ERRO", "Nome demasiado longo", alvo,
                                 "nomes de coluna no PostgreSQL têm no máximo 63 bytes.")
                c.padrao_pg = self.traduzir_padrao(t.nome, c)
            nomes = Counter(c.nome_pg for c in t.colunas)
            for n, k in nomes.items():
                if k > 1:
                    self.rel.add("validacao", "ERRO", "Colunas com o mesmo nome em minúsculas", t.nome,
                                 f"'{n}' aparece {k} vezes; use --manter-maiusculas.")
            for ind in t.indices:
                ind.nome_pg = self.nome_unico(f"{t.nome_pg}_{self.nome_pg(ind.nome)}")
                renomeados += 1
        if renomeados:
            self.rel.add("validacao", "INFO", "Índices renomeados", f"{renomeados} índices",
                         "no PostgreSQL os nomes de índice são únicos por esquema; ficam <tabela>_<índice>.")
        nomes_t = Counter(t.nome_pg for t in esq.tabelas.values())
        for n, k in nomes_t.items():
            if k > 1:
                self.rel.add("validacao", "ERRO", "Tabelas com o mesmo nome em minúsculas", n,
                             f"aparece {k} vezes; use --manter-maiusculas.")
            if len(n.encode()) > 63:
                self.rel.add("validacao", "ERRO", "Nome demasiado longo", n,
                             "nomes de tabela no PostgreSQL têm no máximo 63 bytes.")
        for t in esq.tabelas.values():
            for fk in t.fks:
                fk.nome_pg = self.nome_unico(self.nome_pg(fk.nome))


def validar(esq: EsquemaMySQL, con_my, con_pg, args, rel: Relatorio, plan: Planeador,
            uniques_extra: dict[str, list[list[str]]]) -> None:
    fase = "validacao"
    titulo("Fase 1 — Validação do esquema e dos dados")
    print(f"  MySQL {esq.versao} · {len(esq.tabelas)} tabelas")
    with con_pg.cursor() as cur:
        cur.execute("SELECT current_setting('server_version'), current_setting('server_version_num')::int, "
                    "EXISTS(SELECT 1 FROM pg_collation WHERE collprovider = 'i'), "
                    "EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'postgis'), "
                    "EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_cron')")
        versao_pg, versao_num, tem_icu, tem_postgis, tem_cron = cur.fetchone()
        print(f"  PostgreSQL {versao_pg} · esquema de destino '{args.esquema}'")
    args.versao_pg = versao_num
    args.pg_cron = tem_cron

    # Colunas espaciais: com PostGIS migram; sem ele, são erro.
    espaciais = [f"{t.nome}.{c.nome}" for t in esq.tabelas.values() for c in t.colunas if c.tipo_dado in TIPOS_ESPACIAIS]
    args.postgis = bool(espaciais) and tem_postgis
    if espaciais and not tem_postgis:
        rel.add(fase, "ERRO", "Colunas espaciais sem PostGIS", f"{len(espaciais)} coluna(s)",
                f"{', '.join(espaciais[:5])}: instale o PostGIS no servidor PostgreSQL "
                "(ex.: apt install postgresql-18-postgis-3) e volte a correr.")

    # Comparação de texto: o MySQL (_ci) ignora maiúsculas; o PostgreSQL por defeito não.
    args.usar_ci = args.comparacao_texto == "mysql"
    if args.usar_ci and (versao_num < 120000 or not tem_icu):
        args.usar_ci = False
        rel.add(fase, "AVISO", "Texto vai distinguir maiúsculas", "PostgreSQL",
                "este PostgreSQL não tem ICU (ou é anterior ao 12); WHERE 'Ana' deixa de encontrar 'ana'. "
                "Use lower()/ILIKE na aplicação.")

    ests: dict[str, dict[str, Estatisticas]] = {}
    for i, t in enumerate(esq.tabelas.values(), 1):
        progresso(f"  A analisar dados: {i}/{len(esq.tabelas)} {t.nome[:40]:<40}")
        ests[t.nome] = estatisticas_tabela(con_my, t, not args.sem_booleanos)
    progresso(" " * 70 + "\r")
    plan.planear(esq, ests)
    if plan.colunas_ci:
        rel.add(fase, "INFO", "Texto sem distinguir maiúsculas (como no MySQL)", f"{plan.colunas_ci} colunas",
                "usam uma collation que ignora maiúsculas e acentos: WHERE nome = 'jose' encontra 'José', "
                "e UNIQUE/FK também. ILIKE e expressões regulares (~) dão erro nestas colunas: use LIKE, "
                "que já ignora maiúsculas.")
        if versao_num < 180000:
            rel.add(fase, "AVISO", "LIKE não funciona antes do PostgreSQL 18", f"{plan.colunas_ci} colunas",
                    f"o PostgreSQL {versao_pg} não aceita LIKE em collations que ignoram maiúsculas. "
                    "Atualize para o 18 ou use --comparacao-texto exata.")

    for t in esq.tabelas.values():
        if not t.pk:
            rel.add(fase, "AVISO", "Tabela sem chave primária", t.nome,
                    "a comparação linha a linha é feita por conjunto de linhas (mais lenta, sem indicar o campo).")
        for ind in t.indices:
            if ind.tipo == "FULLTEXT":
                rel.add(fase, "INFO", "Índice FULLTEXT → GIN", f"{t.nome}.{ind.nome}",
                        f"criado como GIN sobre {expressao_fulltext(t, ind)}. As pesquisas MATCH ... AGAINST da "
                        "aplicação têm de usar essa mesma expressão com @@ plainto_tsquery('simple', ...).")
            elif ind.tipo == "SPATIAL":
                if args.postgis:
                    rel.add(fase, "INFO", "Índice SPATIAL → GiST", f"{t.nome}.{ind.nome}", "criado como índice GiST.")
            elif ind.tipo == "FUNCIONAL":
                rel.add(fase, "AVISO", "Índice funcional não migrado", f"{t.nome}.{ind.nome}",
                        "crie o equivalente à mão (CREATE INDEX ... ON tabela ((expressão))).")
            elif ind.prefixo:
                rel.add(fase, "INFO", "Índice com prefixo", f"{t.nome}.{ind.nome}",
                        "o PostgreSQL não indexa prefixos; o índice fica sobre a coluna inteira.")
        if any(c.on_update for c in t.colunas):
            cols = ", ".join(c.nome for c in t.colunas if c.on_update)
            rel.add(fase, "INFO", "ON UPDATE CURRENT_TIMESTAMP", t.nome, f"{cols}: recriado com um trigger.")

    # Chaves estrangeiras
    with con_my.cursor() as cur:
        for t in esq.tabelas.values():
            for fk in t.fks:
                alvo = f"{t.nome}.{fk.nome}"
                if fk.base_ref != args.origem_dados.base or fk.tabela_ref not in esq.tabelas:
                    fk.ignorar = True
                    rel.add(fase, "AVISO", "FK para tabela fora da migração", alvo,
                            f"aponta para {fk.base_ref}.{fk.tabela_ref}; não será criada.")
                    continue
                pai = esq.tabelas[fk.tabela_ref]
                for c, cr in zip(fk.colunas, fk.colunas_ref):
                    fa, fb = familia(t.coluna(c).tipo_pg), familia(pai.coluna(cr).tipo_pg)
                    if fa != fb:
                        rel.add(fase, "ERRO", "FK com tipos incompatíveis", alvo,
                                f"{c} ({t.coluna(c).tipo_pg}) → {fk.tabela_ref}.{cr} ({pai.coluna(cr).tipo_pg}).")
                # O PostgreSQL exige UNIQUE/PK nas colunas referenciadas; o MySQL não.
                chaves = [pai.pk] + [i.colunas for i in pai.indices if i.unico and not i.prefixo]
                if sorted(fk.colunas_ref) not in [sorted(k) for k in chaves]:
                    cols_ref = ", ".join(qm(c) for c in fk.colunas_ref)
                    cur.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {qm(pai.nome)} GROUP BY {cols_ref} "
                                f"HAVING COUNT(*) > 1) x")
                    duplicados = cur.fetchone()[0]
                    if duplicados:
                        fk.ignorar = True
                        rel.add(fase, "ERRO", "FK para colunas sem unicidade", alvo,
                                f"{fk.tabela_ref}({', '.join(fk.colunas_ref)}) tem {duplicados} valor(es) repetido(s); "
                                "o PostgreSQL não permite esta FK.")
                    else:
                        if fk.colunas_ref not in uniques_extra.setdefault(pai.nome, []):
                            uniques_extra[pai.nome].append(list(fk.colunas_ref))
                        rel.add(fase, "AVISO", "FK para colunas sem índice único", alvo,
                                f"vai ser criado UNIQUE em {fk.tabela_ref}({', '.join(fk.colunas_ref)}) "
                                "(o PostgreSQL exige-o; os dados atuais já são únicos).")
                # Órfãos, comparados como o PostgreSQL vai comparar: texto com collation que ignora
                # maiúsculas compara como no MySQL; texto sem ela compara em binário ('ABC' <> 'abc').
                def igual(c: str, cr: str) -> str:
                    col = t.coluna(c)
                    if familia(col.tipo_pg) == "texto" and not (col.colacao_pg and pai.coluna(cr).colacao_pg):
                        return f"CAST(f.{qm(c)} AS BINARY) = CAST(p.{qm(cr)} AS BINARY)"
                    colacao_pai = pai.coluna(cr).colacao
                    if familia(col.tipo_pg) == "texto" and colacao_pai and col.colacao != colacao_pai:
                        return f"f.{qm(c)} COLLATE {colacao_pai} = p.{qm(cr)}"
                    return f"f.{qm(c)} = p.{qm(cr)}"
                cond_join = " AND ".join(igual(c, cr) for c, cr in zip(fk.colunas, fk.colunas_ref))
                nao_nulos = " AND ".join(f"f.{qm(c)} IS NOT NULL" for c in fk.colunas)
                base_q = (f"FROM {qm(t.nome)} f LEFT JOIN {qm(pai.nome)} p ON {cond_join} "
                          f"WHERE {nao_nulos} AND p.{qm(fk.colunas_ref[0])} IS NULL")
                cols = ", ".join(f"f.{qm(c)}" for c in fk.colunas)
                cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT {cols}) {base_q}")
                fk.orfaos, chaves_em_falta = cur.fetchone()
                if fk.orfaos:
                    cur.execute(f"SELECT DISTINCT {cols} {base_q} LIMIT 5")
                    exemplos = ", ".join(str(r[0] if len(r) == 1 else r) for r in cur.fetchall())
                    origem = f"{fk.orfaos} linha(s) em {t.nome}({', '.join(fk.colunas)}) sem par em {fk.tabela_ref}"
                    if args.orfaos == "criar-pais" and not fk.ignorar:
                        rel.add(fase, "AVISO", "Registos órfãos (pais em falta serão criados)", alvo,
                                f"{origem}; ex.: {exemplos}. Antes da FK são inseridos {chaves_em_falta} registo(s) "
                                f"em {fk.tabela_ref} com essas chaves, e a FK fica válida.")
                    else:
                        rel.add(fase, "AVISO", "Registos órfãos (FK fica NOT VALID)", alvo,
                                f"{origem}; ex.: {exemplos}. A FK é criada mas não validada.")

    # Triggers, procedures, funções, eventos e views: traduz já, para mostrar o que fica por rever.
    if args.sem_objetos:
        o = esq.objetos
        total = len(o.triggers) + len(o.rotinas) + len(o.eventos) + len(o.views)
        if total:
            rel.add(fase, "AVISO", "Objetos de código não migrados (--sem-objetos)", f"{total} objeto(s)",
                    "triggers, procedures, funções, eventos e views ficam por recriar à mão.")
    else:
        args.traducoes = traduzir_objetos(esq, args)
        for tr in args.traducoes:
            if tr.erro:
                rel.add(fase, "AVISO", f"Por rever à mão ({tr.tipo})", tr.alvo,
                        f"{tr.erro}. O original e a tentativa ficam no ficheiro *_por_rever.sql.")
            else:
                rel.add(fase, "INFO", f"Tradução automática ({tr.tipo})", tr.alvo, tr.nota or "tradução automática.")

    # Destino
    with con_pg.cursor() as cur:
        cur.execute("SELECT has_database_privilege(current_database(), 'CREATE'), "
                    "EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (args.esquema,))
        pode_criar, esquema_existe = cur.fetchone()
        if esquema_existe:
            cur.execute("SELECT has_schema_privilege(%s, 'CREATE')", (args.esquema,))
            pode_criar = cur.fetchone()[0]
        if not pode_criar:
            rel.add(fase, "ERRO", "Sem permissão no destino", args.esquema,
                    "o utilizador do PostgreSQL não pode criar objetos neste esquema.")
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (args.esquema,))
        existentes = {r[0] for r in cur.fetchall()} & {t.nome_pg for t in esq.tabelas.values()}
    if existentes and args.modo in ("migrar", "tudo"):
        nivel = "AVISO" if args.substituir else "ERRO"
        msg = ("serão apagadas e recriadas (--substituir)." if args.substituir
               else "já existem no destino; use --substituir para as recriar.")
        rel.add(fase, nivel, "Tabelas já existem no destino", f"{len(existentes)} tabela(s)",
                f"{', '.join(sorted(existentes)[:10])}{' ...' if len(existentes) > 10 else ''} {msg}")

    rel.imprimir_achados(fase)
    print(f"\n  Resumo: {cor(str(rel.contar(fase, 'ERRO')) + ' erro(s)', 'ERRO')}, "
          f"{cor(str(rel.contar(fase, 'AVISO')) + ' aviso(s)', 'AVISO')}, {rel.contar(fase, 'INFO')} info")


# --------------------------------------------------------------------------- #
# Objetos de código: triggers, procedures, funções, eventos, views
# --------------------------------------------------------------------------- #
def expressao_fulltext(t: Tabela, ind: Indice) -> str:
    """Expressão do índice GIN equivalente a um FULLTEXT do MySQL."""
    partes = [f"coalesce({sql.Identifier(t.coluna(c).nome_pg).as_string(None)}, '')" for c in ind.colunas]
    juntas = " || ' ' || ".join(partes)
    return f"to_tsvector('simple'::regconfig, {juntas})"


@dataclass
class Traducao:
    tipo: str                 # Trigger / Procedure / Função / Evento / View
    alvo: str                 # nome para mostrar
    nome_pg: str              # nome do objeto principal no PostgreSQL
    original: str
    comandos: list[str] = field(default_factory=list)
    erro: str = ""
    nota: str = ""
    ordem: int = 0            # funções primeiro, depois views, triggers e eventos


def criar_tradutor(esq: EsquemaMySQL, args) -> objetos.Tradutor:
    tipos = {t.nome: {c.nome.lower(): (c.tipo_pg or "") for c in t.colunas} for t in esq.tabelas.values()}
    nomes = {t.nome: t.nome_pg for t in esq.tabelas.values()}
    return objetos.Tradutor(args.origem_dados.base, args.esquema, tipos, nomes, not args.manter_maiusculas)


def traduzir_objetos(esq: EsquemaMySQL, args) -> list[Traducao]:
    tr = criar_tradutor(esq, args)
    o = esq.objetos
    res: list[Traducao] = []

    def tentar(trad: Traducao, gerar) -> None:
        try:
            gerar(trad)
        except NaoSuportado as e:
            trad.erro = str(e)
        res.append(trad)

    for r in o.rotinas:
        tipo = "Função" if r.tipo == "FUNCTION" else "Procedure"
        tentar(Traducao(tipo, r.nome, tr.nome(r.nome), r.corpo, ordem=1),
               lambda d, r=r: d.comandos.append(tr.rotina(r)))
    for v in o.views:
        tentar(Traducao("View", v.nome, tr.nome(v.nome), v.definicao, ordem=2),
               lambda d, v=v: d.comandos.append(tr.view(v, tr.nome(v.nome))))
    for t in o.triggers:
        def gerar_trigger(d, t=t):
            funcao, trigger = tr.trigger(t, esq.tabelas[t.tabela].nome_pg)
            d.comandos += [funcao, trigger]
            if t.ordem > 1:
                d.nota = "no PostgreSQL os triggers da mesma tabela disparam por ordem alfabética do nome."
        tentar(Traducao("Trigger", f"{t.tabela}.{t.nome}", tr.nome(t.nome), t.corpo, ordem=3), gerar_trigger)
    for e in o.eventos:
        def gerar_evento(d, e=e):
            proc, cron, nome = tr.evento(e)
            d.comandos.append(proc)
            chamada = f"CALL {sql.Identifier(args.esquema, nome).as_string(None)}()"
            if not e.ativo:
                d.nota = f"o evento está desativado no MySQL; criada só a procedure {nome}."
            elif cron is None:
                d.nota = (f"criada a procedure {nome}, mas o horário ({e.tipo} {e.intervalo or ''} {e.unidade or ''}) "
                          "não tem equivalente direto em cron; agende-a à mão.")
            elif args.pg_cron:
                d.comandos.append(f"SELECT cron.schedule_in_database({sql.Literal(nome).as_string(None)}, "
                                  f"{sql.Literal(cron).as_string(None)}, {sql.Literal(chamada).as_string(None)}, "
                                  "current_database())")
                d.nota = f"agendado no pg_cron: '{cron}' → {chamada}."
            else:
                d.nota = (f"criada a procedure {nome}. O pg_cron não está instalado; agende no crontab do servidor: "
                          f"{cron} psql \"$DATABASE_URL\" -c '{chamada}'")
        tentar(Traducao("Evento", e.nome, tr.nome(f"evento_{e.nome}"), e.corpo, ordem=4), gerar_evento)
    return sorted(res, key=lambda d: d.ordem)


# --------------------------------------------------------------------------- #
# Colunas espaciais: MySQL guarda SRID + WKB; o PostgreSQL (PostGIS) lê EWKB em hexadecimal
# --------------------------------------------------------------------------- #
def select_mysql(c: Coluna, mariadb: bool) -> str:
    q = qm(c.nome)
    if c.conversao == "geom":
        eixos = "" if mariadb else ", 'axis-order=long-lat'"
        return (f"IF({q} IS NULL, NULL, CONCAT(LPAD(HEX(ST_SRID({q})), 8, '0'), "
                f"HEX(ST_AsBinary({q}{eixos}))))")
    return q


def ewkb_hex(v: str) -> str:
    srid, wkb = int(v[:8], 16), bytes.fromhex(v[8:])
    ordem = "little" if wkb[0] == 1 else "big"
    tipo = int.from_bytes(wkb[1:5], ordem)
    if srid:
        return (wkb[:1] + (tipo | 0x20000000).to_bytes(4, ordem) + srid.to_bytes(4, ordem) + wkb[5:]).hex().upper()
    return wkb.hex().upper()


# --------------------------------------------------------------------------- #
# Conversão de valores (usada na cópia e na verificação)
# --------------------------------------------------------------------------- #
def conversor(c: Coluna, contadores: Counter, chave: str):
    k = c.conversao
    if k == "bool":
        return lambda v: None if v is None else bool(v)
    if k == "bit1":
        return lambda v: None if v is None else int.from_bytes(v, "big") != 0
    if k == "bit":
        return lambda v: None if v is None else int.from_bytes(v, "big")
    if k in ("data", "data_tz"):
        tz = k == "data_tz"

        def data(v):
            if v is None:
                return None
            if isinstance(v, str):  # o pymysql devolve as datas inválidas/zero como texto
                contadores[chave + ":datas_zero"] += 1
                return None
            return v.replace(tzinfo=UTC) if tz and isinstance(v, dt.datetime) else v
        return data
    if k == "hora":
        return lambda v: None if v is None else (dt.datetime.min + v).time()
    if k == "texto":
        def texto(v):
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            if isinstance(v, str) and "\x00" in v:
                contadores[chave + ":nul_removido"] += 1
                return v.replace("\x00", "")
            return v
        return texto
    if k == "bytes":
        return lambda v: v.encode() if isinstance(v, str) else v
    if k == "geom":
        return lambda v: None if v is None else ewkb_hex(v.decode() if isinstance(v, bytes) else v)
    return lambda v: v


def normalizar(v, c: Coluna):
    """Forma comparável do valor, igual para os dois lados."""
    if v is None:
        return None
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else v.normalize()
    if isinstance(v, float):
        return float(f"{v:.6g}") if c.tipo_pg == "real" else float(f"{v:.15g}")
    if isinstance(v, dt.datetime) and v.tzinfo is not None:
        return v.astimezone(UTC).replace(tzinfo=None)
    if c.tipo_pg == "jsonb":
        if isinstance(v, str):
            v = json.loads(v)
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    return v


def tamanho_valor(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (bytes, str)):
        return len(v)
    return len(str(v))


# --------------------------------------------------------------------------- #
# Fase 2 — migração
# --------------------------------------------------------------------------- #
FUNCAO_ON_UPDATE = """
CREATE OR REPLACE FUNCTION {}.migr_on_update_timestamp() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE col text;
BEGIN
  IF NEW IS DISTINCT FROM OLD THEN
    FOREACH col IN ARRAY TG_ARGV LOOP
      IF to_jsonb(NEW) -> col IS NOT DISTINCT FROM to_jsonb(OLD) -> col THEN
        NEW := jsonb_populate_record(NEW, jsonb_build_object(col, statement_timestamp()));
      END IF;
    END LOOP;
  END IF;
  RETURN NEW;
END $$
"""

REGRAS_FK = {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"}

# Registos-pai criados pela migração para as FKs ficarem válidas. Fica num esquema à parte
# para a verificação os reconhecer e para ser fácil de consultar ou apagar depois.
TABELA_CRIADOS = """
CREATE SCHEMA IF NOT EXISTS migracao;
CREATE TABLE IF NOT EXISTS migracao.registos_criados (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  esquema text NOT NULL,
  tabela text NOT NULL,
  colunas text[] NOT NULL,
  chave jsonb NOT NULL,
  motivo text NOT NULL,
  criado_em timestamptz NOT NULL DEFAULT now()
)"""
MARCA_EM_FALTA = "REGISTO EM FALTA (criado na migração)"


class Migrador:
    def __init__(self, esq: EsquemaMySQL, args, rel: Relatorio, con_pg, uniques_extra) -> None:
        self.esq, self.args, self.rel, self.pg = esq, args, rel, con_pg
        self.uniques_extra = uniques_extra
        self.contadores: Counter = Counter()

    def ident(self, *nomes: str) -> sql.Identifier:
        return sql.Identifier(*nomes)

    def tab(self, t: Tabela) -> sql.Identifier:
        return sql.Identifier(self.args.esquema, t.nome_pg)

    def executar(self, comando: sql.Composable) -> None:
        self.rel.ddl.append(comando.as_string(self.pg))
        self.pg.execute(comando)

    def tentar(self, comando: sql.Composable, categoria: str, objeto: str) -> bool:
        try:
            with self.pg.transaction():
                self.executar(comando)
            return True
        except psycopg.Error as e:
            self.rel.add("migracao", "ERRO", categoria, objeto, str(e).strip().splitlines()[0])
            return False

    def ddl_coluna(self, c: Coluna) -> sql.Composable:
        partes: list[sql.Composable] = [self.ident(c.nome_pg), sql.SQL(c.tipo_pg)]
        if c.colacao_pg:
            partes.append(sql.SQL("COLLATE {}").format(self.ident(self.args.esquema, c.colacao_pg)))
        if c.identidade:
            partes.append(sql.SQL("GENERATED BY DEFAULT AS IDENTITY"))
        if not c.nula_pg:
            partes.append(sql.SQL("NOT NULL"))
        if c.padrao_pg is not None and not c.identidade:
            partes += [sql.SQL("DEFAULT"), c.padrao_pg]
        if c.check_enum and c.valores_enum:
            partes.append(sql.SQL("CHECK ({} IN ({}))").format(
                self.ident(c.nome_pg), sql.SQL(", ").join(sql.Literal(v) for v in c.valores_enum)))
        return sql.SQL(" ").join(partes)

    def criar_tabelas(self) -> None:
        with self.pg.transaction():
            self.executar(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(self.ident(self.args.esquema)))
            if self.args.postgis:
                self.executar(sql.SQL("CREATE EXTENSION IF NOT EXISTS postgis"))
            usadas = {c.colacao_pg for t in self.esq.tabelas.values() for c in t.colunas if c.colacao_pg}
            for nome in sorted(usadas):
                self.executar(sql.SQL(
                    "CREATE COLLATION IF NOT EXISTS {} (provider = icu, locale = {}, deterministic = false)").format(
                    self.ident(self.args.esquema, nome), sql.Literal(COLACOES_PG[nome])))
            if self.args.orfaos == "criar-pais":
                self.executar(sql.SQL(TABELA_CRIADOS))
            if self.args.substituir:
                for t in self.esq.tabelas.values():
                    self.executar(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(self.tab(t)))
                if self.args.orfaos == "criar-pais":
                    self.executar(sql.SQL("DELETE FROM migracao.registos_criados WHERE esquema = {} "
                                          "AND tabela = ANY({})").format(
                        sql.Literal(self.args.esquema), sql.Literal([t.nome_pg for t in self.esq.tabelas.values()])))
            for t in self.esq.tabelas.values():
                self.executar(sql.SQL("CREATE TABLE {} (\n  {}\n)").format(
                    self.tab(t), sql.SQL(",\n  ").join(self.ddl_coluna(c) for c in t.colunas)))
                if t.comentario:
                    self.executar(sql.SQL("COMMENT ON TABLE {} IS {}").format(self.tab(t), sql.Literal(t.comentario)))
                for c in t.colunas:
                    if c.comentario:
                        self.executar(sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                            self.tab(t), self.ident(c.nome_pg), sql.Literal(c.comentario)))
        print(f"  {len(self.esq.tabelas)} tabelas criadas")

    def copiar_tabela(self, t: Tabela) -> None:
        inicio = time.time()
        convs = [conversor(c, self.contadores, f"{t.nome}.{c.nome}") for c in t.colunas]
        cols_my = ", ".join(select_mysql(c, self.esq.mariadb) for c in t.colunas)
        cols_pg = sql.SQL(", ").join(self.ident(c.nome_pg) for c in t.colunas)
        linhas = 0
        my = ligar_mysql(self.args.origem_dados)
        try:
            with my.cursor(pymysql.cursors.SSCursor) as cur_my, self.pg.transaction(), self.pg.cursor() as cur_pg:
                cur_my.execute(f"SELECT {cols_my} FROM {qm(t.nome)}")
                with cur_pg.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(self.tab(t), cols_pg)) as cp:
                    while True:
                        lote = cur_my.fetchmany(self.args.lote)
                        if not lote:
                            break
                        for linha in lote:
                            cp.write_row([f(v) for f, v in zip(convs, linha)])
                        linhas += len(lote)
                        total = max(t.linhas_estimadas, linhas)
                        progresso(f"  {t.nome[:40]:<40} {linhas:>10}/{total}")
            estado = "ok"
            print(f"  {t.nome[:40]:<40} {linhas:>10} linhas  {cor('ok', 'OK')} ({time.time() - inicio:.1f}s)")
        except (psycopg.Error, pymysql.Error) as e:
            estado = "erro"
            msg = str(e).strip().splitlines()[0]
            print(f"  {t.nome[:40]:<40} {cor('ERRO', 'ERRO')}: {msg}")
            self.rel.add("migracao", "ERRO", "Falha ao copiar dados", t.nome, msg)
        finally:
            my.close()
        self.rel.migracao[t.nome] = dict(linhas=linhas, estado=estado, segundos=round(time.time() - inicio, 2))

    def criar_chaves_e_indices(self) -> None:
        for t in self.esq.tabelas.values():
            if t.pk:
                nome = self.args.planeador.nome_unico(f"{t.nome_pg}_pkey")
                self.tentar(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} PRIMARY KEY ({})").format(
                    self.tab(t), self.ident(nome),
                    sql.SQL(", ").join(self.ident(t.coluna(c).nome_pg) for c in t.pk)),
                    "Falha ao criar chave primária", t.nome)
            for ind in t.indices:
                if ind.tipo == "FUNCIONAL" or (ind.tipo == "SPATIAL" and not self.args.postgis):
                    continue
                cols = sql.SQL(", ").join(self.ident(t.coluna(c).nome_pg) for c in ind.colunas)
                if ind.tipo == "FULLTEXT":
                    cmd = sql.SQL("CREATE INDEX {} ON {} USING gin ({})").format(
                        self.ident(ind.nome_pg), self.tab(t), sql.SQL(expressao_fulltext(t, ind)))
                elif ind.tipo == "SPATIAL":
                    cmd = sql.SQL("CREATE INDEX {} ON {} USING gist ({})").format(self.ident(ind.nome_pg), self.tab(t), cols)
                elif ind.unico:
                    cmd = sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} UNIQUE ({})").format(
                        self.tab(t), self.ident(ind.nome_pg), cols)
                else:
                    cmd = sql.SQL("CREATE INDEX {} ON {} ({})").format(self.ident(ind.nome_pg), self.tab(t), cols)
                self.tentar(cmd, "Falha ao criar índice", f"{t.nome}.{ind.nome}")
            for cols_ref in self.uniques_extra.get(t.nome, []):
                nome = self.args.planeador.nome_unico(f"{t.nome_pg}_{'_'.join(cols_ref)}_uk")
                self.tentar(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} UNIQUE ({})").format(
                    self.tab(t), self.ident(nome), sql.SQL(", ").join(self.ident(t.coluna(c).nome_pg) for c in cols_ref)),
                    "Falha ao criar UNIQUE para FK", t.nome)
        print("  Chaves primárias e índices criados")

    def acertar_sequencias(self, silencioso: bool = False) -> None:
        n = 0
        for t in self.esq.tabelas.values():
            for c in t.colunas:
                if c.identidade:
                    nome_tab = self.tab(t).as_string(self.pg)
                    self.tentar(sql.SQL(
                        "SELECT setval(pg_get_serial_sequence({}, {}), COALESCE(MAX({}), 1), MAX({}) IS NOT NULL) "
                        "FROM {}").format(sql.Literal(nome_tab), sql.Literal(c.nome_pg), self.ident(c.nome_pg),
                                          self.ident(c.nome_pg), self.tab(t)),
                        "Falha ao acertar auto-incremento", f"{t.nome}.{c.nome}")
                    n += 1
        if not silencioso:
            print(f"  {n} auto-incrementos acertados para o maior id + 1")

    def valor_substituto(self, pai: Tabela, c: Coluna, unico: bool) -> sql.Composable | None:
        """Valor para um campo obrigatório de um registo-pai criado pela migração."""
        col = self.ident(c.nome_pg)
        if c.check_enum and c.valores_enum:
            return sql.Literal(c.valores_enum[0])
        # Campo obrigatório que é FK para outra tabela: usa um registo que já existe lá.
        for fk in pai.fks:
            if fk.colunas == [c.nome] and not fk.ignorar:
                avo = self.esq.tabelas[fk.tabela_ref]
                ref = self.ident(avo.coluna(fk.colunas_ref[0]).nome_pg)
                return sql.SQL("(SELECT MIN({}) FROM {})").format(ref, self.tab(avo))
        fam = familia(c.tipo_pg)
        if fam == "numero":
            if unico:  # maior valor + 1, 2, 3... para não repetir
                return sql.SQL("(SELECT COALESCE(MAX({}), 0) FROM {}) + ROW_NUMBER() OVER ()").format(col, self.tab(pai))
            return sql.SQL("0")
        if fam == "texto":
            limite = c.tamanho if c.tipo_pg.startswith("varchar") else None
            texto = (sql.SQL("'em falta ' || md5(random()::text || ROW_NUMBER() OVER ())") if unico
                     else sql.Literal(MARCA_EM_FALTA))
            return sql.SQL("left({}, {})").format(texto, sql.Literal(limite)) if limite else texto
        return {
            "boolean": sql.SQL("FALSE"), "data": sql.Literal(dt.datetime(1970, 1, 1)),
            "time": sql.Literal(dt.time(0)), "interval": sql.SQL("interval '0'"),
            "jsonb": sql.SQL("'{}'::jsonb"), "bytea": sql.Literal(b""),
        }.get(fam)

    def criar_pais_em_falta(self) -> None:
        """Para cada FK com órfãos, insere no pai os registos com as chaves em falta.

        Repete até não haver mais nada a inserir, porque um registo criado pode precisar
        ele próprio de um pai (ex.: campo obrigatório que é FK).
        """
        fks = [(t, fk) for t in self.esq.tabelas.values() for fk in t.fks if not fk.ignorar]
        falhadas: set[str] = set()
        criados: Counter = Counter()
        for _ in range(5):
            novos = 0
            for t, fk in fks:
                alvo = f"{t.nome}.{fk.nome}"
                if alvo in falhadas:
                    continue
                pai = self.esq.tabelas[fk.tabela_ref]
                chave = pai.pk or fk.colunas_ref
                unicos = {c for i in pai.indices if i.unico for c in i.colunas} | set(pai.pk)
                for cols in self.uniques_extra.get(pai.nome, []):
                    unicos |= set(cols)
                destino = [pai.coluna(c).nome_pg for c in fk.colunas_ref]
                valores: list[sql.Composable] = [sql.SQL("k.{}").format(self.ident(f"k{i}")) for i in range(len(destino))]
                impossivel = None
                for c in pai.colunas:
                    if c.nome in fk.colunas_ref or c.nula_pg or c.identidade or c.padrao_pg is not None:
                        continue
                    v = self.valor_substituto(pai, c, c.nome in unicos)
                    if v is None:
                        impossivel = c.nome
                        break
                    destino.append(c.nome_pg)
                    valores.append(v)
                if impossivel:
                    falhadas.add(alvo)
                    self.rel.add("migracao", "AVISO", "Pai em falta não criado", alvo,
                                 f"não sei que valor pôr em {pai.nome}.{impossivel}; a FK fica NOT VALID.")
                    continue
                sel_chaves = sql.SQL(", ").join(
                    sql.SQL("f.{} AS {}").format(self.ident(t.coluna(c).nome_pg), self.ident(f"k{i}"))
                    for i, c in enumerate(fk.colunas))
                nao_nulos = sql.SQL(" AND ").join(
                    sql.SQL("f.{} IS NOT NULL").format(self.ident(t.coluna(c).nome_pg)) for c in fk.colunas)
                existe = sql.SQL(" AND ").join(
                    sql.SQL("p.{} = f.{}").format(self.ident(pai.coluna(cr).nome_pg), self.ident(t.coluna(c).nome_pg))
                    for c, cr in zip(fk.colunas, fk.colunas_ref))
                cmd = sql.SQL(
                    "WITH novos AS (\n"
                    "  INSERT INTO {pai} ({destino})\n"
                    "  SELECT {valores} FROM (SELECT DISTINCT {sel} FROM {filho} f\n"
                    "    WHERE {nao_nulos} AND NOT EXISTS (SELECT 1 FROM {pai} p WHERE {existe})) k\n"
                    "  ON CONFLICT DO NOTHING\n"
                    "  RETURNING jsonb_build_array({chave}) AS chave)\n"
                    "INSERT INTO migracao.registos_criados (esquema, tabela, colunas, chave, motivo)\n"
                    "SELECT {esquema}, {nome_pai}, {colunas}, chave, {motivo} FROM novos").format(
                    pai=self.tab(pai), destino=sql.SQL(", ").join(self.ident(d) for d in destino),
                    valores=sql.SQL(", ").join(valores), sel=sel_chaves, filho=self.tab(t),
                    nao_nulos=nao_nulos, existe=existe,
                    chave=sql.SQL(", ").join(self.ident(pai.coluna(c).nome_pg) for c in chave),
                    esquema=sql.Literal(self.args.esquema), nome_pai=sql.Literal(pai.nome_pg),
                    colunas=sql.Literal([pai.coluna(c).nome_pg for c in chave]),
                    motivo=sql.Literal(f"FK {fk.nome_pg}: {t.nome_pg}({', '.join(fk.colunas)})"))
                try:
                    with self.pg.transaction(), self.pg.cursor() as cur:
                        cur.execute(cmd)
                        n = cur.rowcount
                    if n:
                        self.rel.ddl.append(cmd.as_string(self.pg))
                        criados[(pai.nome, alvo)] += n
                        novos += n
                except psycopg.Error as e:
                    falhadas.add(alvo)
                    self.rel.add("migracao", "AVISO", "Pai em falta não criado", alvo,
                                 f"{str(e).strip().splitlines()[0]}; a FK fica NOT VALID.")
            if not novos:
                break
        for (pai, alvo), n in sorted(criados.items()):
            self.rel.add("migracao", "INFO", "Registos-pai criados para a FK ficar válida", alvo,
                         f"{n} registo(s) inseridos em {pai} (ver migracao.registos_criados).")
        total = sum(criados.values())
        print(f"  {total} registo(s)-pai em falta criados para as FKs ficarem válidas"
              + (" (ver migracao.registos_criados)" if total else ""))

    def criar_triggers_on_update(self) -> None:
        tabelas = [t for t in self.esq.tabelas.values() if any(c.on_update for c in t.colunas)]
        if not tabelas:
            return
        self.tentar(sql.SQL(FUNCAO_ON_UPDATE).format(self.ident(self.args.esquema)),
                    "Falha ao criar função ON UPDATE", "migr_on_update_timestamp")
        for t in tabelas:
            cols = [sql.Literal(c.nome_pg) for c in t.colunas if c.on_update]
            nome = self.args.planeador.nome_unico(f"{t.nome_pg}_on_update")
            self.tentar(sql.SQL("CREATE TRIGGER {} BEFORE UPDATE ON {} FOR EACH ROW "
                                "EXECUTE FUNCTION {}.migr_on_update_timestamp({})").format(
                self.ident(nome), self.tab(t), self.ident(self.args.esquema), sql.SQL(", ").join(cols)),
                "Falha ao criar trigger ON UPDATE", t.nome)
        print(f"  {len(tabelas)} trigger(s) de ON UPDATE CURRENT_TIMESTAMP criados")

    def criar_fks(self) -> None:
        criadas = validadas = 0
        for t in self.esq.tabelas.values():
            for fk in t.fks:
                if fk.ignorar:
                    continue
                pai = self.esq.tabelas[fk.tabela_ref]
                regras = []
                for acao, regra in (("UPDATE", fk.on_update), ("DELETE", fk.on_delete)):
                    if regra.upper() in REGRAS_FK:
                        regras.append(sql.SQL(f"ON {acao} {regra.upper()}"))
                cmd = sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) {} NOT VALID").format(
                    self.tab(t), self.ident(fk.nome_pg),
                    sql.SQL(", ").join(self.ident(t.coluna(c).nome_pg) for c in fk.colunas),
                    self.tab(pai), sql.SQL(", ").join(self.ident(pai.coluna(c).nome_pg) for c in fk.colunas_ref),
                    sql.SQL(" ").join(regras))
                if not self.tentar(cmd, "Falha ao criar FK", f"{t.nome}.{fk.nome}"):
                    continue
                criadas += 1
                try:
                    with self.pg.transaction():
                        self.executar(sql.SQL("ALTER TABLE {} VALIDATE CONSTRAINT {}").format(
                            self.tab(t), self.ident(fk.nome_pg)))
                    validadas += 1
                except psycopg.Error as e:
                    self.rel.add("migracao", "AVISO", "FK criada mas não validada (NOT VALID)", f"{t.nome}.{fk.nome}",
                                 f"há registos órfãos ({detalhe_erro(e)}). A FK protege os dados "
                                 "novos; corrija os órfãos e rode ALTER TABLE ... VALIDATE CONSTRAINT.")
        print(f"  {criadas} FKs criadas ({validadas} validadas, {criadas - validadas} NOT VALID)")

    def criar_objetos(self) -> None:
        """Cria funções, procedures, views, triggers e eventos traduzidos (depois dos dados)."""
        traducoes = [d for d in getattr(self.args, "traducoes", []) if not d.erro]
        pendentes, criados = traducoes, 0
        # Uma view pode depender de outra: repete enquanto houver progresso.
        for _ in range(len(traducoes) + 1):
            falhados = []
            for d in pendentes:
                try:
                    with self.pg.transaction():
                        for cmd in d.comandos:
                            self.executar(sql.SQL(cmd))
                    criados += 1
                except psycopg.Error as e:
                    d.erro = str(e).strip().splitlines()[0]
                    falhados.append(d)
            if not falhados or len(falhados) == len(pendentes):
                pendentes = falhados
                break
            for d in falhados:
                d.erro = ""
            pendentes = falhados
        for d in pendentes:
            self.rel.add("migracao", "AVISO", f"Não foi criado no PostgreSQL ({d.tipo})", d.alvo,
                         f"a tradução deu erro ao executar ({d.erro}); fica no ficheiro *_por_rever.sql.")
        for d in getattr(self.args, "traducoes", []):
            if d.nota and not d.erro:
                self.rel.add("migracao", "INFO", f"Criado no PostgreSQL ({d.tipo})", d.alvo, d.nota)
        total = len(getattr(self.args, "traducoes", []))
        if total:
            print(f"  {criados}/{total} triggers, procedures, funções, eventos e views criados"
                  + (f" ({total - criados} por rever à mão)" if criados < total else ""))

    def executar_tudo(self) -> None:
        titulo("Fase 2 — Migração")
        self.criar_tabelas()
        print("  A copiar dados (sem FKs ainda, por isso nada bloqueia a cópia):")
        for t in self.esq.tabelas.values():
            self.copiar_tabela(t)
        self.criar_chaves_e_indices()
        self.acertar_sequencias()
        if self.args.orfaos == "criar-pais":
            self.criar_pais_em_falta()
            self.acertar_sequencias(silencioso=True)  # os ids criados podem passar o maior id
        self.criar_triggers_on_update()
        self.criar_fks()
        self.criar_objetos()
        self.pg.execute("ANALYZE")
        for chave, n in sorted(self.contadores.items()):
            alvo, tipo = chave.rsplit(":", 1)
            texto = {"datas_zero": "data(s) zero passaram a NULL", "nul_removido": "valor(es) com \\0 limpos"}[tipo]
            self.rel.add("migracao", "INFO", "Transformações aplicadas", alvo, f"{n} {texto}")
        self.rel.imprimir_achados("migracao")


# --------------------------------------------------------------------------- #
# Fase 3 — verificação
# --------------------------------------------------------------------------- #
class Verificador:
    def __init__(self, esq: EsquemaMySQL, args, rel: Relatorio, con_my, con_pg) -> None:
        self.esq, self.args, self.rel, self.my, self.pg = esq, args, rel, con_my, con_pg

    def tipos_esperados(self) -> dict[str, str]:
        """Pede ao PostgreSQL a forma canónica de cada tipo planeado (ex.: varchar(20) → character varying(20))."""
        tipos = sorted({c.tipo_pg for t in self.esq.tabelas.values() for c in t.colunas if c.tipo_pg})
        with self.pg.transaction(), self.pg.cursor() as cur:
            cols = ", ".join(f"c{i} {tp}" for i, tp in enumerate(tipos))
            cur.execute(f"CREATE TEMP TABLE migr_tipos ({cols}) ON COMMIT DROP")
            cur.execute("SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
                        "WHERE attrelid='migr_tipos'::regclass AND attnum > 0")
            canon = dict(cur.fetchall())
        return {tp: canon[f"c{i}"] for i, tp in enumerate(tipos)}

    def verificar_colunas(self, t: Tabela, canon: dict[str, str], res: dict) -> bool:
        with self.pg.cursor() as cur:
            cur.execute("SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
                        "a.atthasdef OR a.attidentity <> '', co.collname FROM pg_attribute a "
                        "LEFT JOIN pg_collation co ON co.oid = a.attcollation AND co.collname <> 'default' "
                        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
                        (sql.Identifier(self.args.esquema, t.nome_pg).as_string(self.pg),))
            pg_cols = {r[0]: r[1:] for r in cur.fetchall()}
        ok = True
        for c in t.colunas:
            alvo = f"{t.nome}.{c.nome}"
            if c.nome_pg not in pg_cols:
                self.rel.add("verificacao", "ERRO", "Coluna em falta", alvo, "não existe no PostgreSQL.")
                ok = False
                continue
            tipo, nula, tem_padrao, colacao = pg_cols.pop(c.nome_pg)
            if familia(c.tipo_pg) == "texto" and colacao != c.colacao_pg:
                self.rel.add("verificacao", "ERRO", "Comparação de texto diferente do MySQL", alvo,
                             f"collation {colacao or 'padrão'} no PostgreSQL, esperada {c.colacao_pg or 'padrão'} "
                             f"(MySQL {c.colacao}).")
                ok = False
            if c.tipo_pg and tipo != canon[c.tipo_pg]:
                self.rel.add("verificacao", "ERRO", "Tipo diferente do esperado", alvo, f"{tipo} ≠ {canon[c.tipo_pg]}")
                ok = False
            if nula != c.nula_pg:
                self.rel.add("verificacao", "ERRO", "NULL/NOT NULL diferente", alvo,
                             f"PostgreSQL {'aceita' if nula else 'não aceita'} NULL.")
                ok = False
            if (c.padrao_pg is not None or c.identidade) and not tem_padrao:
                self.rel.add("verificacao", "ERRO", "Valor por defeito em falta", alvo, "")
                ok = False
        for extra in pg_cols:
            self.rel.add("verificacao", "AVISO", "Coluna a mais no PostgreSQL", f"{t.nome}.{extra}", "")
        res["colunas_ok"] = ok
        return ok

    def linhas_mysql(self, t: Tabela, ordenar: bool):
        contadores: Counter = Counter()
        convs = [conversor(c, contadores, "") for c in t.colunas]
        ordem = ""
        if ordenar:
            ordem = " ORDER BY " + ", ".join(
                (f"CAST({qm(c)} AS BINARY)" if familia(t.coluna(c).tipo_pg) == "texto" else qm(c)) for c in t.pk)
        my = ligar_mysql(self.args.origem_dados)
        try:
            with my.cursor(pymysql.cursors.SSCursor) as cur:
                cur.execute(f"SELECT {', '.join(select_mysql(c, self.esq.mariadb) for c in t.colunas)} "
                            f"FROM {qm(t.nome)}{ordem}")
                while True:
                    lote = cur.fetchmany(self.args.lote)
                    if not lote:
                        break
                    for linha in lote:
                        yield tuple(normalizar(f(v), c) for f, v, c in zip(convs, linha, t.colunas))
        finally:
            my.close()

    def linhas_pg(self, t: Tabela, ordenar: bool):
        cols = sql.SQL(", ").join(
            sql.SQL("upper(encode(ST_AsEWKB({}), 'hex'))").format(sql.Identifier(c.nome_pg)) if c.conversao == "geom"
            else sql.Identifier(c.nome_pg) for c in t.colunas)
        ordem = sql.SQL("")
        if ordenar:
            ordem = sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                (sql.SQL("{} COLLATE \"C\"").format(sql.Identifier(t.coluna(c).nome_pg))
                 if familia(t.coluna(c).tipo_pg) == "texto" else sql.Identifier(t.coluna(c).nome_pg))
                for c in t.pk)
        with self.pg.transaction(), self.pg.cursor(name=f"verif_{abs(hash(t.nome))}") as cur:
            cur.itersize = self.args.lote
            cur.execute(sql.SQL("SELECT {} FROM {}").format(cols, sql.Identifier(self.args.esquema, t.nome_pg)) + ordem)
            for linha in cur:
                yield tuple(normalizar(v, c) for v, c in zip(linha, t.colunas))

    def registos_criados(self, t: Tabela) -> list[tuple[list[int], set[tuple]]]:
        """Chaves dos registos-pai que a migração criou nesta tabela (não existem no MySQL)."""
        if self.pg.execute("SELECT to_regclass('migracao.registos_criados')").fetchone()[0] is None:
            return []
        grupos = self.pg.execute("SELECT DISTINCT colunas FROM migracao.registos_criados "
                                 "WHERE esquema = %s AND tabela = %s", (self.args.esquema, t.nome_pg)).fetchall()
        por_nome = {c.nome_pg: i for i, c in enumerate(t.colunas)}
        resultado = []
        for (colunas,) in grupos:
            if any(c not in por_nome for c in colunas):
                continue
            idx = [por_nome[c] for c in colunas]
            cols = sql.SQL(", ").join(sql.Identifier(c) for c in colunas)
            linhas = self.pg.execute(sql.SQL(
                "SELECT {cols} FROM {tab} WHERE jsonb_build_array({cols}) IN "
                "(SELECT chave FROM migracao.registos_criados WHERE esquema = %s AND tabela = %s AND colunas = %s)").format(
                cols=cols, tab=sql.Identifier(self.args.esquema, t.nome_pg)),
                (self.args.esquema, t.nome_pg, colunas)).fetchall()
            resultado.append((idx, {tuple(normalizar(v, t.colunas[i]) for v, i in zip(r, idx)) for r in linhas}))
        return resultado

    def comparar_linhas(self, t: Tabela, res: dict) -> None:
        n = len(t.colunas)
        tam_my, tam_pg = [0] * n, [0] * n
        nn_my, nn_pg = [0] * n, [0] * n
        amostras: list[str] = []
        cont_my = cont_pg = iguais = diferentes = em_falta = a_mais = criados = 0
        campos_dif: Counter = Counter()
        grupos_criados = self.registos_criados(t)

        def criado(linha) -> bool:
            return any(tuple(linha[i] for i in idx) in chaves for idx, chaves in grupos_criados)

        def medir(linha, tam, nn):
            for i, v in enumerate(linha):
                if v is not None:
                    nn[i] += 1
                    tam[i] += tamanho_valor(v)

        if t.pk:
            idx = [next(i for i, c in enumerate(t.colunas) if c.nome == p) for p in t.pk]
            chave = lambda r: tuple(r[i] for i in idx)  # noqa: E731
            src, dst = self.linhas_mysql(t, True), self.linhas_pg(t, True)
            a, b = next(src, None), next(dst, None)
            while a is not None or b is not None:
                if b is None or (a is not None and chave(a) < chave(b)):
                    cont_my += 1
                    medir(a, tam_my, nn_my)
                    em_falta += 1
                    if len(amostras) < self.args.amostras:
                        amostras.append(f"linha {chave(a)} não existe no PostgreSQL")
                    a = next(src, None)
                elif a is None or chave(b) < chave(a):
                    cont_pg += 1
                    if criado(b):  # registo-pai criado de propósito para a FK ficar válida
                        criados += 1
                    else:
                        medir(b, tam_pg, nn_pg)
                        a_mais += 1
                        if len(amostras) < self.args.amostras:
                            amostras.append(f"linha {chave(b)} existe só no PostgreSQL")
                    b = next(dst, None)
                else:
                    cont_my += 1
                    cont_pg += 1
                    medir(a, tam_my, nn_my)
                    medir(b, tam_pg, nn_pg)
                    if a == b:
                        iguais += 1
                    else:
                        diferentes += 1
                        for i in range(n):
                            if a[i] != b[i]:
                                campos_dif[t.colunas[i].nome] += 1
                                if len(amostras) < self.args.amostras:
                                    amostras.append(f"linha {chave(a)} campo {t.colunas[i].nome}: "
                                                    f"MySQL={a[i]!r} PG={b[i]!r}")
                    a, b = next(src, None), next(dst, None)
        else:
            def contar(gerador, tam, nn, ignorar_criados=False):
                nonlocal criados
                c = Counter()
                for linha in gerador:
                    if ignorar_criados and criado(linha):
                        criados += 1
                        continue
                    medir(linha, tam, nn)
                    c[repr(linha)] += 1
                return c
            c_my = contar(self.linhas_mysql(t, False), tam_my, nn_my)
            c_pg = contar(self.linhas_pg(t, False), tam_pg, nn_pg, ignorar_criados=True)
            cont_my, cont_pg = sum(c_my.values()), sum(c_pg.values()) + criados
            so_my, so_pg = c_my - c_pg, c_pg - c_my
            em_falta, a_mais = sum(so_my.values()), sum(so_pg.values())
            iguais = cont_my - em_falta
            for r in list(so_my)[: self.args.amostras]:
                amostras.append(f"linha só no MySQL: {r[:200]}")
            for r in list(so_pg)[: self.args.amostras]:
                amostras.append(f"linha só no PostgreSQL: {r[:200]}")

        campos_tam = {}
        for i, c in enumerate(t.colunas):
            if tam_my[i] != tam_pg[i] or nn_my[i] != nn_pg[i]:
                campos_tam[c.nome] = dict(nao_nulos_mysql=nn_my[i], nao_nulos_pg=nn_pg[i],
                                          tamanho_mysql=tam_my[i], tamanho_pg=tam_pg[i])
        res.update(linhas_mysql=cont_my, linhas_pg=cont_pg, iguais=iguais, diferentes=diferentes,
                   em_falta=em_falta, a_mais=a_mais, criados=criados, campos_diferentes=dict(campos_dif),
                   campos_tamanho_diferente=campos_tam, amostras=amostras,
                   volume_mysql=sum(tam_my), volume_pg=sum(tam_pg))
        if diferentes or em_falta or a_mais:
            res["ok"] = False
            self.rel.add("verificacao", "ERRO", "Dados diferentes", t.nome,
                         f"{diferentes} diferente(s), {em_falta} em falta, {a_mais} a mais"
                         + (f"; campos: {', '.join(campos_dif)}" if campos_dif else ""))
        if campos_tam:
            res["ok"] = False
            self.rel.add("verificacao", "ERRO", "Tamanho de campo diferente", t.nome, ", ".join(campos_tam))

    def contar(self, t: Tabela, res: dict) -> None:
        with self.my.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {qm(t.nome)}")
            res["linhas_mysql"] = cur.fetchone()[0]
        res["linhas_pg"] = self.pg.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(
            sql.Identifier(self.args.esquema, t.nome_pg))).fetchone()[0]
        res["criados"] = sum(len(chaves) for _, chaves in self.registos_criados(t))
        if res["linhas_mysql"] != res["linhas_pg"] - res["criados"]:
            res["ok"] = False
            self.rel.add("verificacao", "ERRO", "Número de linhas diferente", t.nome,
                         f"MySQL {res['linhas_mysql']} ≠ PostgreSQL {res['linhas_pg']}")

    def verificar_fks_e_indices(self) -> None:
        with self.pg.cursor() as cur:
            cur.execute("""
                SELECT cl.relname, array_agg(a.attname ORDER BY u.n), cf.relname,
                       array_agg(af.attname ORDER BY u.n), c.convalidated
                FROM pg_constraint c
                JOIN pg_class cl ON cl.oid = c.conrelid JOIN pg_class cf ON cf.oid = c.confrelid
                JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS u(k, fk, n) ON true
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.k
                JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = u.fk
                WHERE c.contype = 'f' AND cl.relnamespace = %s::regnamespace
                GROUP BY c.oid, cl.relname, cf.relname, c.convalidated""",
                        (sql.Identifier(self.args.esquema).as_string(self.pg),))
            fks_pg = {(r[0], tuple(r[1]), r[2], tuple(r[3])): r[4] for r in cur.fetchall()}
            cur.execute("SELECT tablename, COUNT(*) FROM pg_indexes WHERE schemaname=%s GROUP BY 1", (self.args.esquema,))
            ind_pg = dict(cur.fetchall())
        total = ok = nao_validadas = 0
        for t in self.esq.tabelas.values():
            for fk in t.fks:
                if fk.base_ref != self.args.origem_dados.base or fk.tabela_ref not in self.esq.tabelas:
                    continue
                total += 1
                pai = self.esq.tabelas[fk.tabela_ref]
                k = (t.nome_pg, tuple(t.coluna(c).nome_pg for c in fk.colunas), pai.nome_pg,
                     tuple(pai.coluna(c).nome_pg for c in fk.colunas_ref))
                if k not in fks_pg:
                    self.rel.add("verificacao", "ERRO", "FK em falta no PostgreSQL", f"{t.nome}.{fk.nome}",
                                 f"({', '.join(fk.colunas)}) → {fk.tabela_ref}({', '.join(fk.colunas_ref)})")
                else:
                    ok += 1
                    if not fks_pg[k]:
                        nao_validadas += 1
                        self.rel.add("verificacao", "AVISO", "FK NOT VALID (órfãos)", f"{t.nome}.{fk.nome}",
                                     "existe no esquema, mas há linhas antigas que não respeitam a FK.")
            esperados = (1 if t.pk else 0) + sum(
                1 for i in t.indices if i.tipo != "FUNCIONAL" and (i.tipo != "SPATIAL" or self.args.postgis))
            obtidos = ind_pg.get(t.nome_pg, 0)
            if obtidos < esperados:
                self.rel.add("verificacao", "ERRO", "Índices em falta", t.nome, f"esperados {esperados}, existem {obtidos}")
        self.rel.resumo["FKs"] = f"{ok}/{total} presentes ({nao_validadas} NOT VALID)"
        print(f"  FKs: {ok}/{total} presentes no PostgreSQL ({nao_validadas} NOT VALID)")

    def verificar_objetos(self) -> None:
        """Confirma que os objetos traduzidos existem e que as views devolvem os mesmos dados."""
        traducoes = getattr(self.args, "traducoes", [])
        if not traducoes:
            return
        esq = self.args.esquema
        triggers = {r[0] for r in self.pg.execute(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relnamespace = %s::regnamespace", (sql.Identifier(esq).as_string(self.pg),))}
        rotinas = {r[0] for r in self.pg.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = %s::regnamespace", (sql.Identifier(esq).as_string(self.pg),))}
        views = {r[0] for r in self.pg.execute("SELECT viewname FROM pg_views WHERE schemaname = %s", (esq,))}
        ok = 0
        for d in traducoes:
            existe = {"Trigger": triggers, "View": views}.get(d.tipo, rotinas)
            if d.nome_pg not in existe:
                nivel = "AVISO" if d.erro else "ERRO"
                self.rel.add("verificacao", nivel, f"Em falta no PostgreSQL ({d.tipo})", d.alvo,
                             "por rever à mão (ver *_por_rever.sql)." if d.erro else "devia ter sido criado.")
                continue
            if d.tipo == "View" and not self.comparar_view(d):
                continue
            ok += 1
        self.rel.resumo["Objetos de código"] = f"{ok}/{len(traducoes)} presentes e iguais"
        print(f"  Triggers, procedures, funções, eventos e views: {ok}/{len(traducoes)} presentes"
              + (" (views com os mesmos dados)" if any(d.tipo == "View" for d in traducoes) else ""))

    def comparar_view(self, d) -> bool:
        def norm(v):
            if isinstance(v, memoryview):
                return bytes(v)
            if isinstance(v, decimal.Decimal):
                return int(v) if v == v.to_integral_value() else float(v)
            if isinstance(v, float):
                return float(f"{v:.12g}")
            if isinstance(v, dt.datetime) and v.tzinfo is not None:
                return v.astimezone(UTC).replace(tzinfo=None)
            if isinstance(v, dt.timedelta):
                return (dt.datetime.min + v).time()
            return v
        with self.my.cursor() as cur:
            cur.execute(f"SELECT * FROM {qm(d.alvo)}")
            c_my = Counter(tuple(norm(v) for v in r) for r in cur.fetchall())
        consulta = sql.SQL("SELECT * FROM {}").format(sql.Identifier(self.args.esquema, d.nome_pg))
        sem_criados = False
        # Os registos-pai criados pela migração não existem no MySQL e mudam o resultado de JOINs.
        # Compara sem eles: apaga-os dentro de uma transação que é sempre desfeita.
        criados = self.pg.execute("SELECT to_regclass('migracao.registos_criados') IS NOT NULL").fetchone()[0] and \
            self.pg.execute("SELECT EXISTS(SELECT 1 FROM migracao.registos_criados WHERE esquema = %s)",
                            (self.args.esquema,)).fetchone()[0]
        if criados:
            try:
                with self.pg.transaction(force_rollback=True):
                    self.pg.execute("SET LOCAL session_replication_role = replica")  # sem FKs nem triggers
                    for tabela, colunas in self.pg.execute(
                            "SELECT DISTINCT tabela, colunas FROM migracao.registos_criados WHERE esquema = %s",
                            (self.args.esquema,)).fetchall():
                        cols = sql.SQL(", ").join(sql.Identifier(c) for c in colunas)
                        self.pg.execute(sql.SQL(
                            "DELETE FROM {} WHERE jsonb_build_array({}) IN (SELECT chave FROM migracao.registos_criados "
                            "WHERE esquema = %s AND tabela = %s)").format(
                            sql.Identifier(self.args.esquema, tabela), cols), (self.args.esquema, tabela))
                    c_pg = Counter(tuple(norm(v) for v in r) for r in self.pg.execute(consulta).fetchall())
                    sem_criados = True
            except psycopg.Error:
                pass  # sem permissão de superutilizador: compara com eles
        if not sem_criados:
            c_pg = Counter(tuple(norm(v) for v in r) for r in self.pg.execute(consulta).fetchall())
        if c_my != c_pg:
            nivel, extra = "ERRO", ""
            if criados and not sem_criados:
                nivel = "AVISO"
                extra = (" A diferença pode vir dos registos-pai criados pela migração "
                         "(sem superutilizador não é possível compará-la sem eles).")
            self.rel.add("verificacao", nivel, "View devolve dados diferentes", d.alvo,
                         f"MySQL {sum(c_my.values())} linha(s), PostgreSQL {sum(c_pg.values())}; "
                         f"{sum((c_my - c_pg).values())} só no MySQL, {sum((c_pg - c_my).values())} só no PostgreSQL."
                         + extra)
            return False
        return True

    def verificar_sequencias(self) -> None:
        for t in self.esq.tabelas.values():
            for c in t.colunas:
                if not c.identidade:
                    continue
                nome_tab = sql.Identifier(self.args.esquema, t.nome_pg).as_string(self.pg)
                r = self.pg.execute(sql.SQL(
                    "SELECT (SELECT MAX({}) FROM {}), pg_sequence_last_value(pg_get_serial_sequence({}, {}))").format(
                    sql.Identifier(c.nome_pg), sql.Identifier(self.args.esquema, t.nome_pg),
                    sql.Literal(nome_tab), sql.Literal(c.nome_pg))).fetchone()
                maximo, ultimo = r
                if maximo is not None and (ultimo is None or ultimo < maximo):
                    self.rel.add("verificacao", "ERRO", "Auto-incremento atrasado", f"{t.nome}.{c.nome}",
                                 f"próximo id colidiria (maior id {maximo}, sequência em {ultimo}).")

    def executar_tudo(self) -> bool:
        titulo("Fase 3 — Verificação")
        canon = self.tipos_esperados()
        with self.pg.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (self.args.esquema,))
            existentes = {r[0] for r in cur.fetchall()}
        for t in self.esq.tabelas.values():
            res: dict = {"ok": True}
            self.rel.verificacao[t.nome] = res
            if t.nome_pg not in existentes:
                res["ok"] = False
                self.rel.add("verificacao", "ERRO", "Tabela em falta", t.nome, "não existe no PostgreSQL.")
                print(f"  {t.nome[:40]:<40} {cor('FALTA', 'ERRO')}")
                continue
            progresso(f"  {t.nome[:40]:<40} a comparar...")
            if not self.verificar_colunas(t, canon, res):
                res["ok"] = False
            if self.args.sem_comparar_linhas:
                self.contar(t, res)
            else:
                self.comparar_linhas(t, res)
            estado = cor("ok", "OK") if res["ok"] else cor("DIFERENTE", "ERRO")
            extra = "" if self.args.sem_comparar_linhas else f"  {res.get('iguais', 0)} linhas iguais"
            if res.get("criados"):
                extra += f" (+{res['criados']} criada(s) para FKs)"
            print(f"  {t.nome[:40]:<40} {res['linhas_mysql']:>8} → {res['linhas_pg']:<8} {estado}{extra}")
        self.verificar_fks_e_indices()
        self.verificar_objetos()
        self.verificar_sequencias()
        falhas = [t for t, r in self.rel.verificacao.items() if not r["ok"]]
        self.rel.resumo["Tabelas verificadas"] = len(self.rel.verificacao)
        self.rel.resumo["Tabelas com diferenças"] = len(falhas)
        self.rel.resumo["Linhas MySQL"] = sum(r.get("linhas_mysql", 0) for r in self.rel.verificacao.values())
        self.rel.resumo["Linhas PostgreSQL"] = sum(r.get("linhas_pg", 0) for r in self.rel.verificacao.values())
        criados = sum(r.get("criados", 0) for r in self.rel.verificacao.values())
        if criados:
            self.rel.resumo["Registos-pai criados para FKs"] = f"{criados} (em migracao.registos_criados)"
        self.rel.imprimir_achados("verificacao")
        return not falhas and self.rel.contar("verificacao", "ERRO") == 0


# --------------------------------------------------------------------------- #
# Programa principal
# --------------------------------------------------------------------------- #
def ler_argumentos(argv=None):
    p = argparse.ArgumentParser(
        description="Migra uma base MySQL para PostgreSQL com validação prévia e verificação linha a linha.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="As senhas podem ficar fora do URL (são pedidas no terminal) ou nas variáveis "
               "MIGRAR_ORIGEM / MIGRAR_DESTINO, para não ficarem no histórico do shell.")
    p.add_argument("--origem", default=os.environ.get("MIGRAR_ORIGEM"),
                   help="mysql://utilizador[:senha]@host[:porta]/base (se faltar, é perguntado)")
    p.add_argument("--destino", default=os.environ.get("MIGRAR_DESTINO"),
                   help="postgresql://utilizador[:senha]@host[:porta]/base (se faltar, é perguntado)")
    p.add_argument("--modo", choices=["validar", "migrar", "verificar", "tudo"], default="tudo",
                   help="validar: só a fase 1; migrar: fases 1 e 2; verificar: fases 1 e 3; tudo (padrão)")
    p.add_argument("--esquema", default="public", help="esquema de destino no PostgreSQL (padrão: public)")
    p.add_argument("--tabelas", help="migrar só estas tabelas (separadas por vírgula)")
    p.add_argument("--excluir", default="", help="tabelas a ignorar (separadas por vírgula)")
    p.add_argument("--substituir", action="store_true", help="apaga e recria as tabelas que já existem no destino")
    p.add_argument("--forcar", action="store_true", help="migra mesmo com erros na validação")
    p.add_argument("--sim", action="store_true", help="não pede confirmação antes de migrar")
    p.add_argument("--lote", type=int, default=5000, help="linhas lidas de cada vez (padrão: 5000)")
    p.add_argument("--sem-booleanos", action="store_true", help="tinyint(1) fica smallint em vez de boolean")
    p.add_argument("--enum-sem-check", action="store_true", help="ENUM vira varchar sem CHECK com os valores")
    p.add_argument("--manter-maiusculas", action="store_true", help="não passa os nomes para minúsculas")
    p.add_argument("--orfaos", choices=["criar-pais", "not-valid"], default="criar-pais",
                   help="registos órfãos: criar-pais (padrão) insere no pai as chaves em falta antes da FK, "
                        "e a FK fica válida; not-valid deixa os dados como estão e cria a FK NOT VALID")
    p.add_argument("--sem-objetos", action="store_true",
                   help="não traduz triggers, procedures, funções, eventos nem views")
    p.add_argument("--comparacao-texto", choices=["mysql", "exata"], default="mysql",
                   help="mysql (padrão): colunas _ci ignoram maiúsculas e acentos como no MySQL; "
                        "exata: comparação normal do PostgreSQL ('Ana' <> 'ana')")
    p.add_argument("--sem-comparar-linhas", action="store_true", help="na verificação, só conta linhas")
    p.add_argument("--amostras", type=int, default=20, help="diferenças de exemplo por tabela (padrão: 20)")
    p.add_argument("--relatorios", default="relatorios", help="pasta dos relatórios (padrão: ./relatorios)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = ler_argumentos(argv)
    args.origem_dados = ler_conexao(args.origem, "origem (MySQL)", 3306, ("mysql",))
    args.destino_dados = ler_conexao(args.destino, "destino (PostgreSQL)", 5432, ("postgresql", "postgres"))
    rel = Relatorio()
    rel.resumo["Origem"] = "mysql://" + args.origem_dados.descricao()
    rel.resumo["Destino"] = f"postgresql://{args.destino_dados.descricao()} (esquema {args.esquema})"

    try:
        con_my = ligar_mysql(args.origem_dados)
    except pymysql.Error as e:
        print(cor(f"Não consegui ligar ao MySQL: {e}", "ERRO"))
        return 3
    try:
        con_pg = ligar_pg(args.destino_dados)
    except psycopg.Error as e:
        print(cor(f"Não consegui ligar ao PostgreSQL: {e}", "ERRO"))
        return 3

    codigo = 0
    try:
        incluir = {t.strip() for t in args.tabelas.split(",")} if args.tabelas else None
        excluir = {t.strip() for t in args.excluir.split(",") if t.strip()}
        esq = ler_esquema_mysql(con_my, args.origem_dados.base, incluir, excluir)
        if not esq.tabelas:
            print(cor("Nenhuma tabela para migrar.", "ERRO"))
            return 2
        uniques_extra: dict[str, list[list[str]]] = {}
        args.planeador = Planeador(args, rel, esq.mariadb)
        validar(esq, con_my, con_pg, args, rel, args.planeador, uniques_extra)
        rel.traducoes = getattr(args, "traducoes", [])
        erros = rel.contar("validacao", "ERRO")
        rel.resumo["Validação"] = f"{erros} erro(s), {rel.contar('validacao', 'AVISO')} aviso(s)"

        if args.modo in ("migrar", "tudo"):
            if erros and not args.forcar:
                print(cor(f"\nMigração cancelada: {erros} erro(s) na validação. Corrija-os ou use --forcar.", "ERRO"))
                codigo = 2
            else:
                if not args.sim:
                    r = input(f"\nMigrar {len(esq.tabelas)} tabelas para {args.destino_dados.descricao()} "
                              f"(esquema {args.esquema})? [s/N] ").strip().lower()
                    if r not in ("s", "sim", "y"):
                        print("Cancelado.")
                        return 0
                Migrador(esq, args, rel, con_pg, uniques_extra).executar_tudo()
                rel.resumo["Migração"] = f"{rel.contar('migracao', 'ERRO')} erro(s)"
                if rel.contar("migracao", "ERRO"):
                    codigo = 1
        if args.modo in ("verificar", "tudo") and codigo != 2:
            if not Verificador(esq, args, rel, con_my, con_pg).executar_tudo():
                codigo = 1
    except KeyboardInterrupt:
        print(cor("\nInterrompido.", "ERRO"))
        codigo = 130
    finally:
        md, js = rel.gravar(args.relatorios)
        con_my.close()
        con_pg.close()

    titulo("Resultado")
    avisos = sum(1 for a in rel.achados if a["nivel"] == "AVISO")
    if codigo == 0 and args.modo == "validar":
        texto = cor("Validação concluída sem erros", "OK") + (f" ({avisos} aviso(s) para rever)." if avisos else ".")
    elif codigo == 0:
        texto = cor("Concluído: os dados no PostgreSQL são iguais aos do MySQL", "OK") + (
            f" ({avisos} aviso(s) para rever)." if avisos else ".")
    else:
        texto = {1: cor("Concluído com diferenças ou erros.", "ERRO"), 2: cor("Parado na validação.", "ERRO"),
                 130: cor("Interrompido.", "ERRO")}[codigo]
    print(f"  {texto}\n  Relatório: {md}\n  Dados completos: {js}")
    return codigo


if __name__ == "__main__":
    sys.exit(main())

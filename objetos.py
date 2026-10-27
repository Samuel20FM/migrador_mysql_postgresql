"""Tradução dos objetos de código do MySQL para PostgreSQL.

Triggers, procedures, funções, eventos e views são traduzidos para PL/pgSQL / SQL do
PostgreSQL. O SQL "normal" (expressões, SELECT, INSERT, ...) é traduzido com o sqlglot;
a parte procedimental (DECLARE, SET, IF, WHILE, SIGNAL...) é traduzida aqui.

Quando um objeto usa algo que não sabemos traduzir com segurança (cursores, handlers,
variáveis @), é levantado NaoSuportado e o objeto vai para o ficheiro "por rever".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


class NaoSuportado(Exception):
    """Construção do MySQL sem tradução automática segura."""


# --------------------------------------------------------------------------- #
# Objetos lidos do MySQL
# --------------------------------------------------------------------------- #
@dataclass
class Trigger:
    nome: str
    tabela: str
    momento: str      # BEFORE / AFTER
    evento: str       # INSERT / UPDATE / DELETE
    corpo: str
    ordem: int


@dataclass
class Parametro:
    nome: str
    modo: str         # IN / OUT / INOUT
    tipo: str


@dataclass
class Rotina:
    nome: str
    tipo: str         # PROCEDURE / FUNCTION
    corpo: str
    parametros: list[Parametro] = field(default_factory=list)
    retorno: str | None = None


@dataclass
class Evento:
    nome: str
    corpo: str
    tipo: str         # ONE TIME / RECURRING
    intervalo: str | None
    unidade: str | None
    inicio: object
    ativo: bool


@dataclass
class View:
    nome: str
    definicao: str


@dataclass
class Objetos:
    triggers: list[Trigger] = field(default_factory=list)
    rotinas: list[Rotina] = field(default_factory=list)
    eventos: list[Evento] = field(default_factory=list)
    views: list[View] = field(default_factory=list)


def ler_objetos(con, base: str, tabelas: set[str]) -> Objetos:
    obj = Objetos()
    with con.cursor() as cur:
        cur.execute("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, "
                    "ACTION_STATEMENT, ACTION_ORDER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s "
                    "ORDER BY EVENT_OBJECT_TABLE, ACTION_ORDER", (base,))
        for nome, tabela, momento, evento, corpo, ordem in cur.fetchall():
            if tabela in tabelas:
                obj.triggers.append(Trigger(nome, tabela, momento, evento, corpo or "", int(ordem or 0)))

        cur.execute("SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION, DTD_IDENTIFIER "
                    "FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=%s ORDER BY ROUTINE_NAME", (base,))
        rotinas = {(n, t): Rotina(n, t, c or "", [], r) for n, t, c, r in cur.fetchall()}
        cur.execute("SELECT SPECIFIC_NAME, ROUTINE_TYPE, PARAMETER_NAME, PARAMETER_MODE, DTD_IDENTIFIER "
                    "FROM information_schema.PARAMETERS WHERE SPECIFIC_SCHEMA=%s AND ORDINAL_POSITION > 0 "
                    "ORDER BY SPECIFIC_NAME, ORDINAL_POSITION", (base,))
        for nome, tipo, param, modo, dtd in cur.fetchall():
            if (nome, tipo) in rotinas:
                rotinas[(nome, tipo)].parametros.append(Parametro(param, modo or "IN", dtd))
        obj.rotinas = list(rotinas.values())

        cur.execute("SELECT EVENT_NAME, EVENT_DEFINITION, EVENT_TYPE, INTERVAL_VALUE, INTERVAL_FIELD, "
                    "COALESCE(STARTS, EXECUTE_AT), STATUS FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s", (base,))
        for nome, corpo, tipo, valor, unidade, inicio, estado in cur.fetchall():
            obj.eventos.append(Evento(nome, corpo or "", tipo, valor, unidade, inicio, estado == "ENABLED"))

        cur.execute("SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA=%s", (base,))
        obj.views = [View(n, d or "") for n, d in cur.fetchall()]
    return obj


# --------------------------------------------------------------------------- #
# Tokens do código procedimental do MySQL
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r"""
  (?P<ws>\s+)
 |(?P<comment>--[^\n]*|\#[^\n]*|/\*.*?\*/)
 |(?P<str>'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*")
 |(?P<qid>`(?:[^`]|``)*`)
 |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)
 |(?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
 |(?P<op><=>|:=|<=|>=|<>|!=|\|\||&&|[-+*/%=<>(),.;:@!~^&|?{}\[\]])
""", re.S | re.X)


@dataclass
class Token:
    tipo: str
    texto: str

    @property
    def palavra(self) -> str:
        return self.texto.upper() if self.tipo == "word" else ""


def tokenizar(texto: str) -> list[Token]:
    tokens, pos = [], 0
    while pos < len(texto):
        m = _TOKEN.match(texto, pos)
        if not m:
            raise NaoSuportado(f"carácter inesperado {texto[pos]!r}")
        tipo = m.lastgroup
        tokens.append(Token("ws" if tipo == "comment" else tipo, " " if tipo == "comment" else m.group()))
        pos = m.end()
    return tokens


# --------------------------------------------------------------------------- #
# Tradutor
# --------------------------------------------------------------------------- #
INTERVALO_CRON = {"MINUTE": "*/{n} * * * *", "HOUR": "{m} */{n} * * *", "DAY": "{m} {h} */{n} * *",
                  "WEEK": "{m} {h} * * {dow}", "MONTH": "{m} {h} {d} */{n} *"}


class Tradutor:
    """Traduz expressões, SQL e código procedimental do MySQL para PostgreSQL.

    `tipos` mapeia tabela (nome MySQL) → {coluna (minúsculas) → tipo no PostgreSQL}; serve para
    trocar 0/1 por FALSE/TRUE quando a coluna passou a boolean.
    """

    def __init__(self, base_mysql: str, esquema: str, tipos: dict[str, dict[str, str]],
                 nomes_pg: dict[str, str], minusculas: bool) -> None:
        self.base = base_mysql.lower()
        self.esquema = esquema
        self.tipos = {t.lower(): cols for t, cols in tipos.items()}
        self.nomes_pg = {t.lower(): n for t, n in nomes_pg.items()}
        self.minusculas = minusculas

    # ---------------------------- SQL e expressões ---------------------------- #
    def _tipo_coluna(self, col: exp.Column, tabela_new: str | None) -> str | None:
        nome, tab = col.name.lower(), col.table.lower()
        if tab in ("new", "old"):
            return self.tipos.get((tabela_new or "").lower(), {}).get(nome)
        escopo = col.find_ancestor(exp.Select, exp.Update, exp.Delete, exp.Insert)
        tabelas: dict[str, str] = {}
        if escopo is not None:
            for t in escopo.find_all(exp.Table):
                if t.find_ancestor(exp.Select, exp.Update, exp.Delete, exp.Insert) is escopo:
                    tabelas[(t.alias or t.name).lower()] = t.name.lower()
        if tab:
            return self.tipos.get(tabelas.get(tab, tab), {}).get(nome)
        candidatas = [t for t in tabelas.values() if nome in self.tipos.get(t, {})]
        if len(candidatas) == 1:
            return self.tipos[candidatas[0]][nome]
        return None  # variável local ou coluna ambígua

    def _ajustar(self, arvore: exp.Expression, tabela_new: str | None, tipo_alvo: str | None = None) -> exp.Expression:
        def booleano(no, tipo):
            if tipo == "boolean" and isinstance(no, exp.Literal) and not no.is_string and no.this in ("0", "1"):
                return exp.Boolean(this=no.this == "1")
            return no

        def troca(no):
            # `base`.`funcao`(...) → funcao(...) no esquema de destino
            if isinstance(no, exp.Dot) and isinstance(no.this, exp.Identifier) \
                    and no.this.name.lower() == self.base and isinstance(no.expression, exp.Anonymous):
                funcao = no.expression
                if self.esquema != "public":
                    return exp.Dot(this=exp.to_identifier(self.esquema), expression=funcao)
                return funcao
            # funções do utilizador: o sqlglot escreve-as em maiúsculas; o nome no PostgreSQL é minúsculo
            if isinstance(no, exp.Anonymous):
                nome = no.this.name if isinstance(no.this, exp.Identifier) else str(no.this)
                no.set("this", exp.to_identifier(nome.lower() if self.minusculas else nome, quoted=True))
            if isinstance(no, exp.Identifier) and self.minusculas:
                no.set("this", no.this.lower())
            if isinstance(no, (exp.Table, exp.Column)):
                # tira o nome da base MySQL (`g_processes`.`tabela`) e põe o esquema de destino
                db = no.args.get("db")
                if db is not None and db.name.lower() == self.base:
                    no.set("db", exp.to_identifier(self.esquema) if self.esquema != "public" else None)
                if isinstance(no, exp.Table) and no.name.lower() in self.nomes_pg:
                    no.set("this", exp.to_identifier(self.nomes_pg[no.name.lower()]))
            if isinstance(no, (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ)):
                for a, b in (("this", "expression"), ("expression", "this")):
                    lado, outro = no.args.get(a), no.args.get(b)
                    if isinstance(lado, exp.Column):
                        no.set(b, booleano(outro, self._tipo_coluna(lado, tabela_new)))
            if isinstance(no, exp.Insert) and isinstance(no.this, exp.Schema):
                tabela = no.this.this.name.lower() if isinstance(no.this.this, exp.Table) else ""
                cols = [c.name.lower() for c in no.this.expressions]
                valores = no.expression
                if isinstance(valores, exp.Values):
                    for tupla in valores.expressions:
                        for i, v in enumerate(tupla.expressions):
                            if i < len(cols):
                                tupla.expressions[i] = booleano(v, self.tipos.get(tabela, {}).get(cols[i]))
            return no

        arvore = arvore.transform(troca, copy=False)
        return booleano(arvore, tipo_alvo)

    def expressao(self, texto: str, tabela_new: str | None = None, tipo_alvo: str | None = None) -> str:
        try:
            arvore = sqlglot.parse_one(texto, read="mysql")
        except sqlglot.errors.ParseError as e:
            raise NaoSuportado(f"expressão não reconhecida: {texto.strip()[:80]} ({str(e).splitlines()[0]})")
        return self._ajustar(arvore, tabela_new, tipo_alvo).sql(dialect="postgres", normalize_functions=False)

    def comando_sql(self, texto: str, tabela_new: str | None = None) -> str:
        texto = texto.strip()
        if re.match(r"(?i)call\b", texto):
            m = re.match(r"(?is)call\s+([`\w.]+)\s*(\((.*)\))?\s*$", texto)
            if not m:
                raise NaoSuportado(f"CALL não reconhecido: {texto[:80]}")
            nome = m.group(1).replace("`", "")
            nome = nome.split(".")[-1].lower() if self.minusculas else nome.split(".")[-1]
            args = m.group(3)
            args_pg = ", ".join(self.expressao(a, tabela_new) for a in _separar_virgulas(args)) if args else ""
            return f"CALL {nome}({args_pg})"
        try:
            arvore = sqlglot.parse_one(texto, read="mysql")
        except sqlglot.errors.ParseError as e:
            raise NaoSuportado(f"comando não reconhecido: {texto[:80]} ({str(e).splitlines()[0]})")
        if isinstance(arvore, exp.Command):
            raise NaoSuportado(f"comando sem tradução: {texto[:80]}")
        sql = self._ajustar(arvore, tabela_new).sql(dialect="postgres", normalize_functions=False)
        # Em PL/pgSQL um SELECT sem destino tem de ser PERFORM.
        if isinstance(arvore, exp.Select) and not arvore.args.get("into"):
            sql = re.sub(r"(?i)^select\b", "PERFORM", sql)
        return sql

    def tipo(self, texto: str) -> str:
        t = re.sub(r"(?i)\b(unsigned|zerofill|signed)\b", "", texto).strip()
        t = re.sub(r"(?i)\s+(character set|charset|collate)\s+\w+", "", t).strip()
        sem_sinal = re.search(r"(?i)\bunsigned\b", texto) is not None
        base = t.split("(")[0].strip().lower()
        if base == "tinyint" and t.replace(" ", "").lower() == "tinyint(1)":
            return "boolean"
        if sem_sinal and base in ("int", "integer", "mediumint"):
            return "bigint"
        if sem_sinal and base == "bigint":
            return "numeric(20)"
        if base in ("enum", "set"):
            return "text"
        try:
            return exp.DataType.build(t, dialect="mysql").sql(dialect="postgres", normalize_functions=False).lower()
        except Exception:
            raise NaoSuportado(f"tipo {texto!r} sem tradução")

    def nome(self, n: str) -> str:
        n = n.replace("`", "")
        return n.lower() if self.minusculas else n

    # ---------------------------- objetos completos ---------------------------- #
    def trigger(self, t: Trigger, tabela_pg: str) -> tuple[str, str]:
        """Devolve (função, trigger)."""
        decl, corpo = Procedimento(self, t.corpo, t.tabela).traduzir()
        retorno = "NULL" if t.momento == "AFTER" else ("OLD" if t.evento == "DELETE" else "NEW")
        funcao = self.nome(f"{t.nome}_fn")
        sq = self._q(funcao)
        codigo_funcao = (f"CREATE OR REPLACE FUNCTION {sq}() RETURNS trigger LANGUAGE plpgsql AS $migr$\n"
                         f"{_bloco(decl, corpo + [f'RETURN {retorno};'])}\n$migr$")
        codigo_trigger = (f"CREATE TRIGGER {_qi(self.nome(t.nome))} {t.momento} {t.evento} ON {self._q(tabela_pg)} "
                          f"FOR EACH ROW EXECUTE FUNCTION {sq}()")
        return codigo_funcao, codigo_trigger

    def rotina(self, r: Rotina) -> str:
        decl, corpo = Procedimento(self, r.corpo, None).traduzir()
        params = ", ".join(
            (f"{p.modo} " if p.modo != "IN" else "") + f"{_qi(self.nome(p.nome))} {self.tipo(p.tipo)}"
            for p in r.parametros)
        if r.tipo == "FUNCTION":
            return (f"CREATE OR REPLACE FUNCTION {self._q(self.nome(r.nome))}({params}) RETURNS "
                    f"{self.tipo(r.retorno or 'text')} LANGUAGE plpgsql AS $migr$\n{_bloco(decl, corpo)}\n$migr$")
        return (f"CREATE OR REPLACE PROCEDURE {self._q(self.nome(r.nome))}({params}) LANGUAGE plpgsql AS $migr$\n"
                f"{_bloco(decl, corpo)}\n$migr$")

    def evento(self, e: Evento) -> tuple[str, str | None, str]:
        """Devolve (procedure, expressão cron ou None, nome da procedure)."""
        decl, corpo = Procedimento(self, e.corpo, None).traduzir()
        nome = self.nome(f"evento_{e.nome}")
        proc = (f"CREATE OR REPLACE PROCEDURE {self._q(nome)}() LANGUAGE plpgsql AS $migr$\n"
                f"{_bloco(decl, corpo)}\n$migr$")
        cron = None
        if e.tipo == "RECURRING" and e.unidade in INTERVALO_CRON and e.intervalo:
            ini = e.inicio
            m, h = (getattr(ini, "minute", 0), getattr(ini, "hour", 0))
            d = getattr(ini, "day", 1)
            dow = (ini.isoweekday() % 7) if hasattr(ini, "isoweekday") else 0
            n = int(str(e.intervalo).strip("'"))
            if e.unidade == "WEEK" and n != 1:
                cron = None
            else:
                cron = INTERVALO_CRON[e.unidade].format(n=n, m=m, h=h, d=d, dow=dow).replace("*/1 ", "* ")
        return proc, cron, nome

    def view(self, v: View, nome_pg: str) -> str:
        try:
            arvore = sqlglot.parse_one(v.definicao, read="mysql")
        except sqlglot.errors.ParseError as e:
            raise NaoSuportado(f"definição não reconhecida ({str(e).splitlines()[0]})")
        return f"CREATE OR REPLACE VIEW {self._q(nome_pg)} AS\n{self._ajustar(arvore, None).sql(dialect="postgres", pretty=True, normalize_functions=False)}"

    def _q(self, nome: str) -> str:
        return f"{_qi(self.esquema)}.{_qi(nome)}"


def _qi(nome: str) -> str:
    return '"' + nome.replace('"', '""') + '"'


def _bloco(declaracoes: list[str], corpo: list[str]) -> str:
    partes = []
    if declaracoes:
        partes.append("DECLARE\n" + "\n".join("  " + d for d in declaracoes))
    partes.append("BEGIN\n" + "\n".join("  " + l for l in "\n".join(corpo).splitlines()) + "\nEND")
    return "\n".join(partes)


def _separar_virgulas(texto: str) -> list[str]:
    """Separa por vírgulas de topo (fora de parênteses e strings)."""
    partes, nivel, atual = [], 0, []
    for tk in tokenizar(texto):
        if tk.texto == "(":
            nivel += 1
        elif tk.texto == ")":
            nivel -= 1
        if tk.texto == "," and nivel == 0:
            partes.append("".join(atual))
            atual = []
        else:
            atual.append(tk.texto)
    if "".join(atual).strip():
        partes.append("".join(atual))
    return partes


# --------------------------------------------------------------------------- #
# Código procedimental: BEGIN ... END do MySQL → PL/pgSQL
# --------------------------------------------------------------------------- #
class Procedimento:
    def __init__(self, tradutor: Tradutor, texto: str, tabela_new: str | None) -> None:
        self.tr = tradutor
        self.tabela_new = tabela_new
        self.tokens = tokenizar(texto)
        self.sig = [i for i, t in enumerate(self.tokens) if t.tipo != "ws"]
        self.pos = 0

    # --- navegação ---
    def tk(self, k: int = 0) -> Token | None:
        i = self.pos + k
        return self.tokens[self.sig[i]] if i < len(self.sig) else None

    def palavra(self, k: int = 0) -> str:
        t = self.tk(k)
        return t.palavra if t else ""

    def espera(self, *palavras: str) -> None:
        for p in palavras:
            if self.palavra() != p:
                atual = self.tk().texto if self.tk() else "fim"
                raise NaoSuportado(f"esperava {p}, encontrei {atual!r}")
            self.pos += 1

    def ponto_virgula(self) -> None:
        if self.tk() is not None and self.tk().texto == ";":
            self.pos += 1

    def texto(self, a: int, b: int) -> str:
        if a >= b:
            return ""
        return "".join(t.texto for t in self.tokens[self.sig[a]:self.sig[b - 1] + 1])

    def ate(self, paragens: set[str]) -> str:
        """Lê uma expressão até uma palavra de paragem (ou ';') fora de parênteses e de CASE ... END."""
        inicio, nivel, casos = self.pos, 0, 0
        while self.tk() is not None:
            t = self.tk()
            if t.texto == "(":
                nivel += 1
            elif t.texto == ")":
                nivel -= 1
            elif nivel == 0 and t.palavra == "CASE":
                casos += 1
            elif nivel == 0 and casos and t.palavra == "END":
                casos -= 1
            elif nivel == 0 and not casos and (t.texto == ";" or t.palavra in paragens):
                break
            self.pos += 1
        return self.texto(inicio, self.pos)

    # --- tradução ---
    def traduzir(self) -> tuple[list[str], list[str]]:
        if self.palavra() == "BEGIN" or (self.tk(1) and self.tk(1).texto == ":" and self.palavra(2) == "BEGIN"):
            if self.palavra() != "BEGIN":
                self.pos += 2
            self.pos += 1
            decl, corpo = self.bloco_begin()
            self.ponto_virgula()
        else:  # corpo de um só comando, sem BEGIN
            decl, corpo = [], self.comando()
        if self.tk() is not None:
            raise NaoSuportado(f"texto a mais depois do fim: {self.tk().texto!r}")
        return decl, corpo

    def bloco_begin(self) -> tuple[list[str], list[str]]:
        """Já passou o BEGIN; lê até ao END correspondente."""
        decl: list[str] = []
        while self.palavra() == "DECLARE":
            decl += self.declare()
        corpo = self.comandos({"END"})
        self.espera("END")
        if self.tk() is not None and self.tk().tipo == "word" and self.palavra() not in ("IF", "LOOP", "WHILE", "REPEAT", "CASE"):
            self.pos += 1  # END rótulo
        return decl, corpo

    def comandos(self, paragens: set[str]) -> list[str]:
        linhas: list[str] = []
        while self.tk() is not None and self.palavra() not in paragens:
            if self.tk().texto == ";":
                self.pos += 1
                continue
            linhas += self.comando()
        return linhas

    def declare(self) -> list[str]:
        self.espera("DECLARE")
        if self.palavra() in ("CONTINUE", "EXIT", "UNDO") or self.palavra(1) in ("CURSOR", "CONDITION") \
                or self.palavra(2) in ("CURSOR", "CONDITION"):
            raise NaoSuportado("DECLARE de cursor, condição ou handler (reescreva com FOR ... IN SELECT / EXCEPTION)")
        nomes = []
        while True:
            nomes.append(self.tr.nome(self.tk().texto))
            self.pos += 1
            if self.tk() is not None and self.tk().texto == ",":
                self.pos += 1
                continue
            break
        tipo_txt = self.ate({"DEFAULT"})
        tipo = self.tr.tipo(tipo_txt)
        padrao = ""
        if self.palavra() == "DEFAULT":
            self.pos += 1
            padrao = " := " + self.tr.expressao(self.ate(set()), self.tabela_new, tipo)
        self.ponto_virgula()
        return [f"{n} {tipo}{padrao};" for n in nomes]

    def comando(self) -> list[str]:
        p = self.palavra()
        rotulo = ""
        if self.tk(1) is not None and self.tk(1).texto == ":" and self.palavra(2) in ("BEGIN", "LOOP", "WHILE", "REPEAT"):
            rotulo = f"<<{self.tr.nome(self.tk().texto)}>>\n"
            self.pos += 2
            p = self.palavra()
        if p == "BEGIN":
            self.pos += 1
            decl, corpo = self.bloco_begin()
            self.ponto_virgula()
            return (rotulo + _bloco(decl, corpo) + ";").splitlines()
        if p == "IF":
            return self.se()
        if p == "CASE":
            return self.caso()
        if p == "WHILE":
            self.pos += 1
            cond = self.tr.expressao(self.ate({"DO"}), self.tabela_new)
            self.espera("DO")
            corpo = self.comandos({"END"})
            self.espera("END", "WHILE")
            self._rotulo_final()
            return (rotulo + f"WHILE {cond} LOOP").splitlines() + _indentar(corpo) + ["END LOOP;"]
        if p == "LOOP":
            self.pos += 1
            corpo = self.comandos({"END"})
            self.espera("END", "LOOP")
            self._rotulo_final()
            return (rotulo + "LOOP").splitlines() + _indentar(corpo) + ["END LOOP;"]
        if p == "REPEAT":
            self.pos += 1
            corpo = self.comandos({"UNTIL"})
            self.espera("UNTIL")
            cond = self.tr.expressao(self.ate({"END"}), self.tabela_new)
            self.espera("END", "REPEAT")
            self._rotulo_final()
            return (rotulo + "LOOP").splitlines() + _indentar(corpo + [f"EXIT WHEN {cond};"]) + ["END LOOP;"]
        if p in ("LEAVE", "ITERATE"):
            self.pos += 1
            alvo = self.tr.nome(self.tk().texto)
            self.pos += 1
            self.ponto_virgula()
            return [f"{'EXIT' if p == 'LEAVE' else 'CONTINUE'} {alvo};"]
        if p == "SET":
            return self.atribuicao()
        if p == "SIGNAL":
            return self.sinal()
        if p in ("RESIGNAL", "OPEN", "FETCH", "CLOSE", "PREPARE", "EXECUTE", "DEALLOCATE"):
            raise NaoSuportado(f"{p} (cursores e SQL dinâmico precisam de revisão manual)")
        if p == "RETURN":
            self.pos += 1
            valor = self.tr.expressao(self.ate(set()), self.tabela_new)
            self.ponto_virgula()
            return [f"RETURN {valor};"]
        if p == "DECLARE":
            raise NaoSuportado("DECLARE fora do início do bloco")
        if self.tk() is not None and self.tk().texto == "@":
            raise NaoSuportado("variáveis de sessão (@var)")
        sql = self.ate(set())
        self.ponto_virgula()
        if "@" in sql:
            raise NaoSuportado("variáveis de sessão (@var)")
        return [self.tr.comando_sql(sql, self.tabela_new) + ";"]

    def _rotulo_final(self) -> None:
        if self.tk() is not None and self.tk().tipo == "word":
            self.pos += 1
        self.ponto_virgula()

    def se(self) -> list[str]:
        self.espera("IF")
        linhas: list[str] = []
        chave = "IF"
        while True:
            cond = self.tr.expressao(self.ate({"THEN"}), self.tabela_new)
            self.espera("THEN")
            corpo = self.comandos({"ELSEIF", "ELSE", "END"})
            linhas += [f"{chave} {cond} THEN"] + _indentar(corpo)
            if self.palavra() == "ELSEIF":
                self.pos += 1
                chave = "ELSIF"
                continue
            if self.palavra() == "ELSE":
                self.pos += 1
                linhas += ["ELSE"] + _indentar(self.comandos({"END"}))
            self.espera("END", "IF")
            self.ponto_virgula()
            return linhas + ["END IF;"]

    def caso(self) -> list[str]:
        self.espera("CASE")
        alvo = ""
        if self.palavra() != "WHEN":
            alvo = " " + self.tr.expressao(self.ate({"WHEN"}), self.tabela_new)
        linhas = [f"CASE{alvo}"]
        while self.palavra() == "WHEN":
            self.pos += 1
            valor = self.tr.expressao(self.ate({"THEN"}), self.tabela_new)
            self.espera("THEN")
            linhas += [f"  WHEN {valor} THEN"] + _indentar(self.comandos({"WHEN", "ELSE", "END"}), 4)
        if self.palavra() == "ELSE":
            self.pos += 1
            linhas += ["  ELSE"] + _indentar(self.comandos({"END"}), 4)
        self.espera("END", "CASE")
        self.ponto_virgula()
        return linhas + ["END CASE;"]

    def atribuicao(self) -> list[str]:
        self.espera("SET")
        if self.palavra() in ("GLOBAL", "SESSION", "NAMES", "CHARACTER", "TRANSACTION") or (
                self.tk() is not None and self.tk().texto == "@"):
            raise NaoSuportado("SET de variável de sessão ou de sistema")
        texto = self.ate(set())
        self.ponto_virgula()
        linhas = []
        for parte in _separar_virgulas(texto):
            m = re.match(r"(?s)\s*([`\w.]+)\s*(?::=|=)\s*(.*)$", parte)
            if not m:
                raise NaoSuportado(f"SET não reconhecido: {parte.strip()[:60]}")
            alvo = m.group(1).replace("`", "")
            tipo_alvo = None
            if "." in alvo:
                tab, col = alvo.split(".", 1)
                if tab.upper() in ("NEW", "OLD"):
                    alvo = f"{tab.upper()}.{self.tr.nome(col)}"
                    tipo_alvo = self.tr.tipos.get((self.tabela_new or "").lower(), {}).get(col.lower())
            else:
                alvo = self.tr.nome(alvo)
            linhas.append(f"{alvo} := {self.tr.expressao(m.group(2), self.tabela_new, tipo_alvo)};")
        return linhas

    def sinal(self) -> list[str]:
        self.espera("SIGNAL")
        if self.palavra() != "SQLSTATE":
            raise NaoSuportado("SIGNAL com condição nomeada")
        self.pos += 1
        if self.palavra() == "VALUE":
            self.pos += 1
        estado = self.tk().texto.strip("'\"")
        self.pos += 1
        mensagem = None
        if self.palavra() == "SET":
            self.pos += 1
            for parte in _separar_virgulas(self.ate(set())):
                m = re.match(r"(?s)\s*(\w+)\s*=\s*(.*)$", parte)
                if m and m.group(1).upper() == "MESSAGE_TEXT":
                    mensagem = self.tr.expressao(m.group(2), self.tabela_new)
        self.ponto_virgula()
        using = f"ERRCODE = '{estado}'" + (f", MESSAGE = {mensagem}" if mensagem else "")
        return [f"RAISE EXCEPTION USING {using};"]


def _indentar(linhas: list[str], n: int = 2) -> list[str]:
    return [" " * n + l for l in "\n".join(linhas).splitlines()]

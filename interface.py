#!/usr/bin/env python3
"""Interface web do migrador.

Abre o navegador numa página local onde se preenchem as ligações e as opções.
A migração corre o migrar.py num processo à parte e a saída aparece na página,
como num terminal, à medida que é escrita.

    python interface.py                # abre o navegador
    python interface.py --porta 8765 --sem-navegador

Só escuta em 127.0.0.1. Cada arranque gera um token que vai no link aberto;
sem ele a API não responde, para que outra página aberta no navegador não
consiga lançar migrações.
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

PASTA = Path(__file__).resolve().parent
MIGRAR = PASTA / "migrar.py"
PAGINA = PASTA / "web" / "index.html"

MODOS = ("validar", "migrar", "verificar", "tudo")
# Opções do migrar.py que a página pode ligar (chave do JSON -> flag).
FLAGS = {
    "substituir": "--substituir",
    "forcar": "--forcar",
    "sem_objetos": "--sem-objetos",
    "sem_booleanos": "--sem-booleanos",
    "enum_sem_check": "--enum-sem-check",
    "manter_maiusculas": "--manter-maiusculas",
    "sem_comparar_linhas": "--sem-comparar-linhas",
}
ESCOLHAS = {"orfaos": ("criar-pais", "not-valid"), "comparacao_texto": ("mysql", "exata")}
TEXTOS = {"esquema": "--esquema", "tabelas": "--tabelas", "excluir": "--excluir"}
NUMEROS = {"lote": "--lote", "amostras": "--amostras"}
RESULTADOS = {0: "ok", 1: "diferencas", 2: "parado", 3: "ligacao", 130: "interrompido"}


class Pedido(Exception):
    """Erro nos dados enviados pela página (responde 400)."""


def url_conexao(esquema: str, d: dict, porta_padrao: int) -> str:
    """Monta o URL com a senha incluída (vazia se não houver), para o migrar.py não a pedir."""
    host = str(d.get("host") or "127.0.0.1").strip()
    porta = int(d.get("porta") or porta_padrao)
    utilizador = str(d.get("utilizador") or "").strip()
    base = str(d.get("base") or "").strip()
    if not utilizador or not base:
        raise Pedido(f"Preencha o utilizador e a base de dados ({esquema}).")
    senha = str(d.get("senha") or "")
    return f"{esquema}://{quote(utilizador, safe='')}:{quote(senha, safe='')}@{host}:{porta}/{quote(base, safe='')}"


def montar_comando(pedido: dict) -> tuple[list[str], dict]:
    modo = pedido.get("modo")
    if modo not in MODOS:
        raise Pedido("Modo inválido.")
    opcoes = pedido.get("opcoes") or {}
    cmd = [sys.executable, "-u", str(MIGRAR), "--modo", modo, "--sim"]
    for chave, flag in FLAGS.items():
        if opcoes.get(chave):
            cmd.append(flag)
    for chave, validas in ESCOLHAS.items():
        valor = opcoes.get(chave) or validas[0]
        if valor not in validas:
            raise Pedido(f"Valor inválido para {chave}.")
        cmd += ["--" + chave.replace("_", "-"), valor]
    for chave, flag in TEXTOS.items():
        valor = str(opcoes.get(chave) or "").strip()
        if valor:
            cmd += [flag, valor]
    for chave, flag in NUMEROS.items():
        valor = opcoes.get(chave)
        if valor not in (None, ""):
            try:
                n = int(valor)
            except (TypeError, ValueError):
                raise Pedido(f"{chave} tem de ser um número.") from None
            if n < 1:
                raise Pedido(f"{chave} tem de ser maior que zero.")
            cmd += [flag, str(n)]
    # As senhas vão por variável de ambiente, não pela linha de comando (que aparece no ps).
    env = dict(os.environ,
               MIGRAR_ORIGEM=url_conexao("mysql", pedido.get("origem") or {}, 3306),
               MIGRAR_DESTINO=url_conexao("postgresql", pedido.get("destino") or {}, 5432),
               MIGRAR_TERMINAL="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env.pop("NO_COLOR", None)
    return cmd, env


def relatorios_existentes() -> dict[str, float]:
    pasta = PASTA / "relatorios"
    if not pasta.is_dir():
        return {}
    return {p.name: p.stat().st_mtime for p in pasta.iterdir() if p.is_file()}


class Execucao:
    """Uma execução do migrar.py e tudo o que escreveu até agora."""

    def __init__(self, cmd: list[str], env: dict, modo: str):
        self.modo = modo
        self.inicio = time.time()
        self.fim: float | None = None
        self.codigo: int | None = None
        self.partes: list[str] = []
        self.novos_relatorios: list[str] = []
        self.cond = threading.Condition()
        self._antes = relatorios_existentes()
        self._interrupcoes = 0
        self.proc = subprocess.Popen(cmd, cwd=PASTA, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        threading.Thread(target=self._ler, daemon=True).start()

    def _ler(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self.proc.stdout.fileno()
        while True:
            bloco = os.read(fd, 4096)
            texto = dec.decode(bloco, final=not bloco)
            if texto:
                with self.cond:
                    self.partes.append(texto)
                    self.cond.notify_all()
            if not bloco:
                break
        codigo = self.proc.wait()
        antes = self._antes
        novos = sorted(n for n, m in relatorios_existentes().items() if antes.get(n) != m)
        with self.cond:
            self.codigo = codigo if codigo >= 0 else 130
            self.fim = time.time()
            self.novos_relatorios = novos
            self.cond.notify_all()

    @property
    def a_correr(self) -> bool:
        return self.codigo is None

    def parar(self) -> str:
        """1.º pedido: Ctrl+C (o migrar.py grava o relatório). 2.º: termina à força."""
        if not self.a_correr:
            return "já terminou"
        self._interrupcoes += 1
        if self._interrupcoes == 1:
            self.proc.send_signal(signal.SIGINT)
            return "interrompido"
        os.killpg(self.proc.pid, signal.SIGKILL)
        return "terminado à força"

    def estado(self) -> dict:
        return {
            "modo": self.modo,
            "a_correr": self.a_correr,
            "codigo": self.codigo,
            "resultado": RESULTADOS.get(self.codigo, "erro") if self.codigo is not None else None,
            "duracao": round((self.fim or time.time()) - self.inicio, 1),
            "relatorios": self.novos_relatorios,
        }


class Estado:
    token = secrets.token_urlsafe(24)
    hosts: set[str] = set()
    execucao: Execucao | None = None
    trinco = threading.Lock()


def testar_ligacao(lado: str, d: dict) -> dict:
    """Liga à base e devolve a versão e as bases existentes (para sugerir nomes)."""
    # Os drivers são importados aqui para a interface arrancar mesmo sem eles instalados.
    if lado == "origem":
        import pymysql
        try:
            con = pymysql.connect(host=str(d.get("host") or "127.0.0.1"), port=int(d.get("porta") or 3306),
                                  user=str(d.get("utilizador") or ""), password=str(d.get("senha") or ""),
                                  charset="utf8mb4", connect_timeout=5)
        except pymysql.Error as e:
            return {"ok": False, "mensagem": str(e.args[-1] if e.args else e)}
        with con, con.cursor() as cur:
            cur.execute("SELECT VERSION()")
            versao = cur.fetchone()[0]
            cur.execute("SHOW DATABASES")
            sistema = {"information_schema", "mysql", "performance_schema", "sys"}
            bases = [r[0] for r in cur.fetchall() if r[0] not in sistema]
        return {"ok": True, "mensagem": f"MySQL {versao}", "bases": bases}

    import psycopg
    base = str(d.get("base") or "").strip() or "postgres"
    params = dict(host=str(d.get("host") or "127.0.0.1"), port=int(d.get("porta") or 5432),
                  user=str(d.get("utilizador") or ""), password=str(d.get("senha") or ""),
                  connect_timeout=5)
    try:
        con = psycopg.connect(dbname=base, autocommit=True, **params)
        aviso = ""
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            return {"ok": False, "mensagem": str(e).strip().splitlines()[-1]}
        # A base de destino ainda não existe: liga à "postgres" só para listar.
        try:
            con = psycopg.connect(dbname="postgres", autocommit=True, **params)
        except psycopg.Error as e2:
            return {"ok": False, "mensagem": str(e2).strip().splitlines()[-1]}
        aviso = f" · a base '{base}' não existe; crie-a com: createdb {base}"
    with con:
        versao = con.execute("SHOW server_version").fetchone()[0]
        bases = [r[0] for r in con.execute(
            "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname")]
    return {"ok": not aviso, "mensagem": f"PostgreSQL {versao}{aviso}", "bases": bases}


class Manipulador(BaseHTTPRequestHandler):
    server_version = "Migrador"

    def log_message(self, fmt, *args):  # sem ruído no terminal a cada pedido
        pass

    # -- utilitários -------------------------------------------------------
    def _enviar(self, estado: int, corpo: bytes, tipo: str, extra: dict | None = None) -> None:
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados, estado: int = 200) -> None:
        self._enviar(estado, json.dumps(dados, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_valido(self) -> bool:
        # Protege contra DNS rebinding: só aceita os nomes locais desta porta.
        return self.headers.get("Host", "") in Estado.hosts

    def _token_valido(self, consulta: dict) -> bool:
        recebido = self.headers.get("X-Token") or (consulta.get("t") or [""])[0]
        return secrets.compare_digest(recebido, Estado.token)

    def _ler_json(self) -> dict:
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > 100_000:
            raise Pedido("Pedido demasiado grande.")
        try:
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            raise Pedido("JSON inválido.") from None
        if not isinstance(dados, dict):
            raise Pedido("JSON inválido.")
        return dados

    # -- rotas -------------------------------------------------------------
    def do_GET(self):
        if not self._host_valido():
            return self._enviar(403, b"Host invalido", "text/plain")
        url = urlparse(self.path)
        consulta = parse_qs(url.query)
        if url.path == "/":
            return self._enviar(200, PAGINA.read_bytes(), "text/html; charset=utf-8",
                                {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})
        if not self._token_valido(consulta):
            return self._json({"erro": "Token em falta ou errado. Abra o link mostrado no terminal."}, 403)
        if url.path == "/api/estado":
            ex = Estado.execucao
            return self._json({"execucao": ex.estado() if ex else None})
        if url.path == "/api/eventos":
            return self._eventos(consulta)
        if url.path == "/api/relatorios":
            existentes = relatorios_existentes()
            nomes = sorted((n for n in existentes if n.endswith(".md")), key=existentes.get, reverse=True)
            return self._json({"relatorios": [
                {"md": n, "outros": [o for o in (n[:-3] + ".json", n[:-3] + ".sql", n[:-3] + "_por_rever.sql")
                                     if o in existentes],
                 "data": existentes[n]} for n in nomes[:15]]})
        if url.path.startswith("/relatorio/"):
            nome = url.path.rsplit("/", 1)[-1]
            if nome not in relatorios_existentes():  # só nomes da pasta: nada de ../
                return self._json({"erro": "Relatório não encontrado."}, 404)
            tipo = "application/json" if nome.endswith(".json") else "text/plain"
            return self._enviar(200, (PASTA / "relatorios" / nome).read_bytes(), f"{tipo}; charset=utf-8")
        return self._json({"erro": "Não encontrado."}, 404)

    def do_POST(self):
        if not self._host_valido():
            return self._enviar(403, b"Host invalido", "text/plain")
        url = urlparse(self.path)
        if not self._token_valido({}):  # no POST o token só vale no cabeçalho
            return self._json({"erro": "Token em falta ou errado."}, 403)
        try:
            if url.path == "/api/executar":
                return self._executar(self._ler_json())
            if url.path == "/api/parar":
                ex = Estado.execucao
                return self._json({"mensagem": ex.parar() if ex else "nada a correr"})
            if url.path == "/api/testar":
                dados = self._ler_json()
                lado = dados.get("lado")
                if lado not in ("origem", "destino"):
                    raise Pedido("Lado inválido.")
                return self._json(testar_ligacao(lado, dados.get("dados") or {}))
        except Pedido as e:
            return self._json({"erro": str(e)}, 400)
        except (ValueError, TypeError) as e:
            return self._json({"erro": f"Dados inválidos: {e}"}, 400)
        return self._json({"erro": "Não encontrado."}, 404)

    def _executar(self, pedido: dict):
        with Estado.trinco:
            if Estado.execucao and Estado.execucao.a_correr:
                return self._json({"erro": "Já há uma execução a correr. Pare-a ou espere que termine."}, 409)
            cmd, env = montar_comando(pedido)
            Estado.execucao = Execucao(cmd, env, pedido["modo"])
        return self._json({"ok": True})

    def _eventos(self, consulta: dict):
        """Server-Sent Events: envia a saída a partir da parte `desde` e depois o fim."""
        ex = Estado.execucao
        if ex is None:
            return self._json({"erro": "Nenhuma execução."}, 404)
        try:
            i = int(self.headers.get("Last-Event-ID") or (consulta.get("desde") or ["0"])[0])
        except ValueError:
            i = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                with ex.cond:
                    while i >= len(ex.partes) and ex.a_correr:
                        if not ex.cond.wait(timeout=15):
                            break
                    novas = ex.partes[i:]
                    terminou = not ex.a_correr and i + len(novas) >= len(ex.partes)
                for parte in novas:
                    i += 1
                    self.wfile.write(f"id: {i}\ndata: {json.dumps(parte)}\n\n".encode())
                if not novas:
                    self.wfile.write(b": ainda aqui\n\n")
                if terminou:
                    estado = json.dumps(ex.estado(), ensure_ascii=False)
                    self.wfile.write(f"event: fim\ndata: {estado}\n\n".encode())
                    self.wfile.flush()
                    return
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def porta_livre(preferida: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferida))
            return preferida
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Interface web do migrador MySQL → PostgreSQL.")
    p.add_argument("--porta", type=int, default=8765, help="porta local (padrão: 8765; se ocupada, outra livre)")
    p.add_argument("--sem-navegador", action="store_true", help="não abre o navegador sozinho")
    args = p.parse_args(argv)
    # Se foi lançado em segundo plano, o Ctrl+C pode vir desligado (SIG_IGN) e passaria
    # assim ao migrar.py, que deixaria de responder ao botão Parar. Repõe o normal.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    porta = porta_livre(args.porta)
    Estado.hosts = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
    servidor = ThreadingHTTPServer(("127.0.0.1", porta), Manipulador)
    servidor.daemon_threads = True
    link = f"http://127.0.0.1:{porta}/?t={Estado.token}"
    print(f"Migrador — interface web\n  Abra: {link}\n  Ctrl+C para fechar.", flush=True)
    if not args.sem_navegador:
        threading.Timer(0.5, webbrowser.open, (link,)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nA fechar...")
        ex = Estado.execucao
        if ex and ex.a_correr:
            ex.parar()
            try:
                ex.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(ex.proc.pid, signal.SIGKILL)
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

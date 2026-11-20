"""Agente Central de Salas: servidor A2A v1.0 por fora, host MCP por dentro.

    python agente/agente.py

- GET  /.well-known/agent-card.json  Agent Card (v1.0)
- POST /a2a                          JSON-RPC 2.0: SendMessage e GetTask

Sem LLM. O pedido chega em formato fixo e o agente decide por regra:

    reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
    escolha=<id da sala> | escolha=recusar

O agente nao conhece regra de sala (conflito, politica, alternativas): isso e
do servidor MCP. Ele traduz protocolo. A traducao central, a ponte, esta em
`Ponte._aplicar_resultado` (input_required -> TASK_STATE_INPUT_REQUIRED) e em
`Ponte._continuar` (escolha -> retry MCP com inputResponses + requestState).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cliente_mcp import ClienteMCP, ErroMCP, Trace, log

HOST = os.environ.get("AGENTE_HOST")  # padrao: os dois loopbacks (127.0.0.1 e ::1)
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
CAMINHO_A2A = "/a2a"
URL_PUBLICA = os.environ.get("AGENTE_URL_PUBLICA")  # padrao: derivada do Host do request
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED, "TASK_STATE_REJECTED"}

# Erros JSON-RPC (gerais e os especificos do A2A).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

USO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
_PEDIDO = re.compile(r"^\s*reservar\s+(?P<resto>.+?)\s*$", re.DOTALL)
_PAR = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)", re.DOTALL)
_ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


class ErroA2A(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Pausa:
    """O que a Task precisa para retomar o tools/call. Nunca sai do agente.

    `request_state` e opaco: e guardado e ecoado byte a byte, nunca aberto.
    """

    tool: str
    argumentos: dict[str, Any]
    chave: str
    campo: str
    alternativas: list[str]
    request_state: str
    politica: str | None


@dataclass
class Registro:
    task: dict[str, Any]
    trace: Trace
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)


class Ponte:
    """Maquina de estados das Tasks e a costura com o MCP."""

    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        # Estado das Tasks em memoria do processo, indexado pelo id da Task.
        # A pausa (com o requestState) fica no Registro, fora do dict `task`
        # que e serializado para o cliente, entao nao tem como vazar.
        self.tasks: dict[str, Registro] = {}

    # -- A2A: metodos ----------------------------------------------------

    async def send_message(self, params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
        mensagem = params.get("message")
        if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
            raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
        if not mensagem.get("messageId"):
            raise ErroA2A(INVALID_PARAMS, "params.message.messageId e obrigatorio")
        texto = " ".join(p.get("text", "") for p in mensagem["parts"] if isinstance(p, dict)).strip()

        task_id = mensagem.get("taskId")
        if task_id:
            registro = self.tasks.get(task_id)
            if registro is None:
                raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
            async with registro.trava:
                estado = registro.task["status"]["state"]
                if estado in TERMINAIS:
                    raise ErroA2A(
                        UNSUPPORTED_OPERATION,
                        f"Task {task_id} esta em estado terminal ({estado}) e nao aceita novas mensagens",
                    )
                if estado != INPUT_REQUIRED or registro.pausa is None:
                    raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando resposta ({estado})")
                registro.task["history"].append(mensagem)
                await self._continuar(registro, texto)
                return {"task": registro.task}

        registro = self._abrir(mensagem, traceparent)
        async with registro.trava:
            await self._executar(registro, texto)
        return {"task": registro.task}

    def get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id")
        if not task_id:
            raise ErroA2A(INVALID_PARAMS, "params.id e obrigatorio")
        registro = self.tasks.get(task_id)
        if registro is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        task = dict(registro.task)
        limite = params.get("historyLength")
        if isinstance(limite, int) and limite >= 0:
            task["history"] = task["history"][-limite:] if limite else []
        return {"task": task}

    # -- ciclo de vida da Task -----------------------------------------

    def _abrir(self, mensagem: dict[str, Any], traceparent: str | None) -> Registro:
        task_id = _id("task")
        task = {
            "id": task_id,
            "contextId": mensagem.get("contextId") or _id("ctx"),
            "status": {"state": SUBMITTED},
            "history": [mensagem],
            "artifacts": [],
        }
        registro = Registro(task=task, trace=Trace.do_header(traceparent))
        self.tasks[task_id] = registro
        log(f"A2A task {task_id} aberta em {SUBMITTED} trace-id={registro.trace.trace_id}")
        return registro

    def _estado(self, registro: Registro, estado: str, texto: str | None = None) -> None:
        task = registro.task
        anterior = task["status"]["state"]
        status: dict[str, Any] = {"state": estado}
        if texto is not None:
            resposta = {
                "messageId": _id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": task["id"],
                "contextId": task["contextId"],
            }
            status["message"] = resposta
            task["history"].append(resposta)
        task["status"] = status
        log(f"A2A task {task['id']}: {anterior} -> {estado}" + (f" | {texto}" if texto else ""))

    def _falhar(self, registro: Registro, texto: str) -> None:
        registro.pausa = None
        self._estado(registro, FAILED, texto)

    async def _executar(self, registro: Registro, texto: str) -> None:
        """Primeira mensagem: interpreta o pedido, descobre o servidor e chama a tool."""
        self._estado(registro, WORKING)
        pedido = _interpretar_pedido(texto)
        if pedido is None:
            self._falhar(registro, f"Pedido invalido: use {USO}")
            return
        trace = registro.trace
        try:
            # Descoberta em runtime: a tool e seus argumentos vem do tools/list.
            tools = {t.get("name"): t for t in await self.mcp.listar_tools(trace)}
            tool = tools.get(TOOL_RESERVA)
            if tool is None:
                self._falhar(registro, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            argumentos, faltando = _argumentos_pelo_schema(tool.get("inputSchema") or {}, pedido)
            if faltando:
                self._falhar(registro, f"Pedido invalido: falta {', '.join(faltando)}. Use {USO}")
                return
            # Resource e escolha da aplicacao: o agente le a politica para citar a versao.
            politica = _versao_da_politica(await self.mcp.ler_resource(URI_POLITICA, trace))
            resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, trace)
        except ErroMCP as e:
            self._falhar(registro, str(e))
            return
        except Exception as e:  # rede, timeout: a Task falha, o agente nao cai
            self._falhar(registro, f"Servidor MCP indisponivel: {type(e).__name__}: {e}")
            return
        self._aplicar_resultado(registro, TOOL_RESERVA, argumentos, politica, resultado)

    async def _continuar(self, registro: Registro, texto: str) -> None:
        """A PONTE, volta: a resposta A2A vira o retry do tools/call original."""
        pausa = registro.pausa
        assert pausa is not None
        casou = _ESCOLHA.match(texto)
        valor = casou.group("valor") if casou else None
        if valor != "recusar" and valor not in pausa.alternativas:
            # Escolha fora do enum: continua pausada e repete a mesma linha.
            self._estado(registro, INPUT_REQUIRED, _linha_alternativas(pausa.alternativas))
            return

        if valor == "recusar":
            resposta: dict[str, Any] = {"action": "decline"}
        else:
            resposta = {"action": "accept", "content": {pausa.campo: valor}}

        self._estado(registro, WORKING)
        registro.pausa = None
        try:
            # Mesmo tools/call, id novo (o cliente gera), a mesma chave do
            # inputRequests e o requestState ecoado sem modificacao.
            resultado = await self.mcp.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                registro.trace,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except ErroMCP as e:
            self._falhar(registro, str(e))
            return
        except Exception as e:
            self._falhar(registro, f"Servidor MCP indisponivel: {type(e).__name__}: {e}")
            return
        self._aplicar_resultado(registro, pausa.tool, pausa.argumentos, pausa.politica, resultado)

    def _aplicar_resultado(
        self,
        registro: Registro,
        tool: str,
        argumentos: dict[str, Any],
        politica: str | None,
        resultado: dict[str, Any],
    ) -> None:
        """A PONTE, ida: traduz o resultado MCP em estado da Task A2A."""
        if resultado.get("resultType") == "input_required":
            pergunta = _ler_elicitation(resultado)
            request_state = resultado.get("requestState")
            if pergunta is None or not isinstance(request_state, str):
                self._falhar(registro, "O servidor MCP pediu uma informacao que este agente nao sabe pedir")
                return
            chave, campo, alternativas = pergunta
            # Guarda o requestState ligado a esta Task (e so a ela) e pausa.
            registro.pausa = Pausa(
                tool=tool,
                argumentos=argumentos,
                chave=chave,
                campo=campo,
                alternativas=alternativas,
                request_state=request_state,
                politica=politica,
            )
            self._estado(registro, INPUT_REQUIRED, _linha_alternativas(alternativas))
            return

        if resultado.get("isError"):
            texto = " ".join(c.get("text", "") for c in resultado.get("content") or []).strip()
            self._falhar(registro, texto or "A tool falhou sem mensagem")
            return

        dados = resultado.get("structuredContent")
        if not isinstance(dados, dict):
            try:
                dados = json.loads(" ".join(c.get("text", "") for c in resultado.get("content") or []))
            except ValueError:
                dados = {}
        if dados.get("reservado") is False:
            self._estado(registro, CANCELED, "Reserva recusada: nenhuma sala foi reservada.")
            return
        if not dados.get("reserva"):
            self._falhar(registro, "O servidor MCP concluiu sem devolver a reserva")
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        registro.task["artifacts"].append(
            {"artifactId": _id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}]}
        )
        self._estado(registro, COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


# -- traducoes de formato (protocolo, nao dominio) ---------------------------


def _interpretar_pedido(texto: str) -> dict[str, str] | None:
    casou = _PEDIDO.match(texto)
    if not casou:
        return None
    pares = {k: v.strip() for k, v in _PAR.findall(casou.group("resto"))}
    return pares or None


def _argumentos_pelo_schema(schema: dict[str, Any], pedido: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Monta os argumentos a partir do inputSchema descoberto, nao de uma lista fixa."""
    propriedades = schema.get("properties") or {}
    argumentos = {nome: pedido[nome] for nome in propriedades if nome in pedido}
    faltando = [nome for nome in schema.get("required") or [] if nome not in argumentos]
    return argumentos, faltando


def _versao_da_politica(texto: str) -> str | None:
    primeira = (texto.splitlines() or [""])[0]
    chave, _, valor = primeira.partition(":")
    return valor.strip() if chave.strip() == "versao" and valor.strip() else None


def _ler_elicitation(resultado: dict[str, Any]) -> tuple[str, str, list[str]] | None:
    """Extrai (chave, campo, alternativas) de um input_required com uma elicitation form.

    Aceita o requestedSchema plano com `enum` ou, quando so ha uma opcao, `const`.
    """
    pedidos = resultado.get("inputRequests") or {}
    if not isinstance(pedidos, dict) or len(pedidos) != 1:
        return None
    chave, pedido = next(iter(pedidos.items()))
    params = (pedido or {}).get("params") or {}
    if pedido.get("method") != "elicitation/create" or params.get("mode", "form") != "form":
        return None
    propriedades = (params.get("requestedSchema") or {}).get("properties") or {}
    if len(propriedades) != 1:
        return None
    campo, definicao = next(iter(propriedades.items()))
    opcoes = definicao.get("enum") if isinstance(definicao.get("enum"), list) else None
    if opcoes is None and "const" in definicao:
        opcoes = [definicao["const"]]
    if not opcoes:
        return None
    return chave, campo, [str(o) for o in opcoes]


def _linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


# -- HTTP -------------------------------------------------------------------


def agent_card(url_a2a: str) -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [{"url": url_a2a, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


def criar_app() -> Starlette:
    estado: dict[str, Any] = {}

    @asynccontextmanager
    async def ciclo(_: Starlette):
        cliente = ClienteMCP(MCP_URL)
        estado["ponte"] = Ponte(cliente)
        log(f"Central de Salas (A2A) na porta {PORTA}{CAMINHO_A2A}, servidor MCP em {MCP_URL}")
        try:
            yield
        finally:
            await cliente.fechar()

    async def card(request: Request) -> JSONResponse:
        base = URL_PUBLICA or str(request.base_url).rstrip("/")
        return JSONResponse(agent_card(f"{base}{CAMINHO_A2A}"))

    async def rpc(request: Request) -> JSONResponse:
        def erro(req_id: Any, code: int, message: str) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

        try:
            corpo = json.loads(await request.body())
        except ValueError:
            return erro(None, PARSE_ERROR, "Parse error")
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
            return erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Invalid Request")
        req_id, metodo = corpo.get("id"), corpo["method"]
        params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
        traceparent = request.headers.get("traceparent")
        log(f"A2A <- {metodo} id={req_id} traceparent={traceparent or '-'}")
        ponte: Ponte = estado["ponte"]
        try:
            if metodo == "SendMessage":
                resultado = await ponte.send_message(params, traceparent)
            elif metodo == "GetTask":
                resultado = ponte.get_task(params)
            else:
                return erro(req_id, METHOD_NOT_FOUND, f"Method not found: {metodo}")
        except ErroA2A as e:
            log(f"A2A -> id={req_id} erro {e.code}: {e.message}")
            return erro(req_id, e.code, e.message)
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": resultado})

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", card, methods=["GET"]),
            Route(CAMINHO_A2A, rpc, methods=["POST"]),
        ],
        lifespan=ciclo,
    )


def _sockets(host: str | None, porta: int) -> list[socket.socket]:
    """Sem host explicito, escuta em 127.0.0.1 e ::1, para `localhost` responder
    de primeira nos dois (no Windows a tentativa em ::1 recusada custa ~2s)."""
    alvos = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")] if host is None else [
        (socket.AF_INET6 if ":" in host else socket.AF_INET, host)
    ]
    abertos = []
    for familia, endereco in alvos:
        try:
            s = socket.socket(familia, socket.SOCK_STREAM)
            if familia == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((endereco, porta))
            s.listen(128)
            s.setblocking(False)
            abertos.append(s)
        except OSError as e:
            if host is not None or familia == socket.AF_INET:
                raise
            log(f"sem IPv6 em {endereco}: {e}")
    return abertos


def main() -> None:
    config = uvicorn.Config(criar_app(), log_level="warning")
    uvicorn.Server(config).run(sockets=_sockets(HOST, PORTA))


if __name__ == "__main__":
    main()

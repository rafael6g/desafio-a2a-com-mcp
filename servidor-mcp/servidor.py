"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

    REQUEST_STATE_SECRET=<hex de 32 bytes> python servidor-mcp/servidor.py

Endpoint unico em http://127.0.0.1:7301/mcp. Sem sessao: cada POST e um request
autocontido, com versao e capabilities no _meta (o transporte do SDK recusa com
-32602/HTTP 400 o que vier sem eles).

O MRTR da reserva e conduzido pelo corpo da tool: no conflito ela devolve um
InputRequiredResult com a elicitation e um requestState em texto claro, e o
RequestStateBoundary do SDK sela esse texto (AES-256-GCM, expiracao, vinculo ao
metodo, a tool e aos argumentos) antes de ir para o fio. No retry o boundary
verifica e abre o token antes de a tool rodar; adulterado ou expirado vira
-32602 sem nunca chegar aqui.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime
from typing import Any

import uvicorn
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
)
from pydantic import BaseModel

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError

from dominio import ERRO_SEM_ALTERNATIVA, Agenda, Intervalo, RegraVioladaError, ler_politica, versao_politica

HOST = os.environ.get("MCP_HOST")  # padrao: os dois loopbacks (127.0.0.1 e ::1)
PORTA = int(os.environ.get("MCP_PORT", "7301"))
CAMINHO = os.environ.get("MCP_PATH", "/mcp")

# Validade do requestState: 10 minutos (o enunciado aceita de 5 a 30).
TTL_REQUEST_STATE = 600.0
# Chave que o servidor atribui a unica entrada de inputRequests.
CHAVE_ESCOLHA = "escolha_de_sala"
VERSAO_ESTADO = 1


def log(mensagem: str) -> None:
    """Log de operacao no stderr (o logging do protocolo MCP esta depreciado)."""
    agora = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{agora} [mcp] {mensagem}", file=sys.stderr, flush=True)


def segredo_do_ambiente() -> bytes:
    """Le REQUEST_STATE_SECRET e exige no minimo 32 bytes de aleatoriedade.

    O formato recomendado e o de secrets.token_hex(32): 64 caracteres hex que
    decodificam para 32 bytes. Qualquer outro texto precisa ter >= 32 bytes.
    """
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        sys.exit(
            "REQUEST_STATE_SECRET nao definida. Gere uma com:\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = bruto.encode()
    if len(chave) < 32:
        sys.exit(f"REQUEST_STATE_SECRET tem {len(chave)} bytes; o minimo e 32 (use secrets.token_hex(32)).")
    return chave


# -- schemas de saida ------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# -- servidor --------------------------------------------------------------

agenda = Agenda()

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[segredo_do_ambiente()], ttl=TTL_REQUEST_STATE),
    log_level="WARNING",
    # Subscriptions estao fora de escopo: sem subscriptions/listen, o SDK
    # tambem nao anuncia listChanged nem resources.subscribe.
    subscriptions=False,
)

# Prompts estao fora de escopo. O MCPServer registra os handlers de prompts
# sempre, e o SDK anuncia a capability de prompts a partir deles; como nao ha
# API publica para desregistrar, eles sao retirados aqui. Resultado: o servidor
# anuncia so `tools` e `resources`, e prompts/* responde -32601.
for _metodo in ("prompts/list", "prompts/get"):
    mcp._lowlevel_server._request_handlers.pop(_metodo, None)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in agenda.salas])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        _, intervalo = agenda.validar(sala, inicio, fim)
    except RegraVioladaError as e:
        raise ToolError(str(e)) from None
    conflitos = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in agenda.conflitos(sala, intervalo)]
    return Disponibilidade(sala=sala, livre=not conflitos, conflitos=conflitos)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    try:
        if ctx.request_state is not None:
            return _retomar(ctx)
        return _reservar_ou_perguntar(sala, inicio, fim, responsavel, ctx)
    except RegraVioladaError as e:
        raise ToolError(str(e)) from None


def _reservar_ou_perguntar(sala_id: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    """Primeira rodada: reserva se livre, senao termina a resposta pedindo a escolha."""
    sala, intervalo = agenda.validar(sala_id, inicio, fim)
    reserva = agenda.reservar(sala_id, intervalo, responsavel)
    if reserva is not None:
        return _concluida(reserva)
    return _perguntar(sala, intervalo, responsavel, ctx)


def _perguntar(sala: dict, intervalo: Intervalo, responsavel: str, ctx: Context) -> InputRequiredResult:
    alternativas = agenda.alternativas(sala, intervalo)
    if not alternativas:
        raise RegraVioladaError(ERRO_SEM_ALTERNATIVA)
    _exigir_elicitation_form(ctx)
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(
            message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": alternativas,
                    }
                },
                "required": ["sala"],
            },
        )
    )
    # Texto claro de proposito: o RequestStateBoundary do SDK sela (AES-GCM) na
    # saida e abre/verifica na entrada. Carrega o pedido inteiro, entao o retry
    # nao depende de nada em memoria nem dos argumentos que o cliente reenviar.
    estado = {
        "v": VERSAO_ESTADO,
        "pedido": {
            "sala": sala["id"],
            "inicio": intervalo.inicio_iso,
            "fim": intervalo.fim_iso,
            "responsavel": responsavel,
        },
        "chave": CHAVE_ESCOLHA,
        "alternativas": alternativas,
    }
    return InputRequiredResult(
        input_requests={CHAVE_ESCOLHA: pergunta},
        request_state=json.dumps(estado, separators=(",", ":")),
    )


def _retomar(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Retry: reconstroi o pedido a partir do requestState ja verificado pelo SDK."""
    try:
        estado = json.loads(ctx.request_state or "")
        pedido = estado["pedido"]
        alternativas = list(estado["alternativas"])
        chave = estado["chave"]
        if estado.get("v") != VERSAO_ESTADO:
            raise ValueError("versao")
    except (ValueError, KeyError, TypeError):
        # So chega aqui texto que o proprio servidor selou; formato estranho e
        # deriva de versao, tratada como estado invalido.
        raise MCPError(code=-32602, message="Invalid or expired requestState") from None

    sala, intervalo = agenda.validar(pedido["sala"], pedido["inicio"], pedido["fim"])
    resposta = (ctx.input_responses or {}).get(chave)
    if resposta is None:
        # Retry sem resposta: pergunta de novo, com as alternativas de agora.
        return _perguntar(sala, intervalo, pedido["responsavel"], ctx)

    acao = getattr(resposta, "action", None)
    if acao in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado" if acao == "decline" else "cancelado")

    escolhida = (getattr(resposta, "content", None) or {}).get("sala")
    if acao != "accept" or escolhida not in alternativas:
        # Resposta fora do que foi oferecido: nao toma efeito, pergunta de novo.
        return _perguntar(sala, intervalo, pedido["responsavel"], ctx)

    reserva = agenda.reservar(escolhida, intervalo, pedido["responsavel"])
    if reserva is None:
        # A alternativa foi ocupada entre as rodadas: recalcula e pergunta de novo.
        return _perguntar(sala, intervalo, pedido["responsavel"], ctx)
    return _concluida(reserva)


def _exigir_elicitation_form(ctx: Context) -> None:
    """-32021 se o cliente nao declarou elicitation em form mode neste request.

    Segue a regra da spec: `elicitation: {}` sem modos equivale a form (compat.
    com 2025-11-25); declarar so `url` nao conta.
    """
    caps = ctx.client_capabilities
    elicitation = caps.elicitation if caps is not None else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data={"requiredCapabilities": {"elicitation": {"form": {}}}},
    )


def _concluida(reserva: dict) -> ReservaOut:
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=versao_politica(),
    )


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.", mime_type="text/markdown")
def politica_de_uso() -> str:
    return ler_politica()


# -- log de cada request no stderr -----------------------------------------


class LogDeRequests:
    """ASGI por fora do transporte: registra todo POST, inclusive os recusados.

    Uma linha na chegada (metodo, id, traceparent do _meta, nome da tool/URI e
    se veio requestState) e uma na saida (HTTP, resultType ou codigo de erro).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        corpo = b""
        mensagens = []
        while True:
            msg = await receive()
            mensagens.append(msg)
            corpo += msg.get("body", b"")
            if not msg.get("more_body"):
                break

        async def replay() -> dict:
            if mensagens:
                return mensagens.pop(0)
            return await receive()

        req_id = self._registrar_entrada(corpo)
        status = {"http": 0}
        saida = bytearray()

        async def espiar(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                status["http"] = msg["status"]
            elif msg["type"] == "http.response.body":
                saida.extend(msg.get("body", b""))
                if not msg.get("more_body"):
                    self._registrar_saida(req_id, status["http"], bytes(saida))
            await send(msg)

        await self.app(scope, replay, espiar)

    @staticmethod
    def _registrar_entrada(corpo: bytes) -> Any:
        try:
            req = json.loads(corpo)
        except ValueError:
            log("<- corpo que nao e JSON")
            return None
        if not isinstance(req, dict):
            log("<- corpo JSON que nao e um objeto")
            return None
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        partes = [f"<- {req.get('method')}", f"id={req.get('id')}"]
        if "name" in params:
            partes.append(f"name={params['name']}")
        if "uri" in params:
            partes.append(f"uri={params['uri']}")
        if "requestState" in params:
            partes.append("retry(requestState+inputResponses)")
        partes.append(f"traceparent={meta.get('traceparent', '-')}")
        log(" ".join(partes))
        return req.get("id")

    @staticmethod
    def _registrar_saida(req_id: Any, http: int, corpo: bytes) -> None:
        try:
            resp = json.loads(corpo)
        except ValueError:
            log(f"-> id={req_id} http={http}")
            return
        if isinstance(resp, dict) and "error" in resp:
            erro = resp["error"] or {}
            log(f"-> id={req_id} http={http} error={erro.get('code')} {erro.get('message')}")
        elif isinstance(resp, dict):
            resultado = resp.get("result") or {}
            extra = " isError=true" if resultado.get("isError") else ""
            log(f"-> id={req_id} http={http} resultType={resultado.get('resultType')}{extra}")


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
    app = mcp.streamable_http_app(
        streamable_http_path=CAMINHO,
        stateless_http=True,
        json_response=True,
        host=HOST or "127.0.0.1",
    )
    log(f"central-de-salas em http://localhost:{PORTA}{CAMINHO} (politica {versao_politica()})")
    config = uvicorn.Config(LogDeRequests(app), log_level="warning")
    uvicorn.Server(config).run(sockets=_sockets(HOST, PORTA))


if __name__ == "__main__":
    main()

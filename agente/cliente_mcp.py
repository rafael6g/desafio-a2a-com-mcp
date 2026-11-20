"""Lado host MCP do agente: um cliente Streamable HTTP (revisao 2026-07-28).

Cada request e autocontido, sem sessao: carrega no _meta a versao do protocolo,
o clientInfo e as capabilities (elicitation em form mode), e nos headers o
espelho que o transporte exige (MCP-Protocol-Version, Mcp-Method e, quando o
metodo nomeia um alvo, Mcp-Name).

O cliente devolve o resultado cru. Em particular, um `input_required` NAO e
respondido aqui: quem decide o que fazer com ele e a ponte, no agente A2A. Por
isso este cliente nao tem callback de elicitation.

Todo request ganha um id de JSON-RPC novo, inclusive o retry do MRTR, que e um
request independente do original.
"""

from __future__ import annotations

import itertools
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx2

PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}

# Metodos cujo alvo o transporte espelha no header Mcp-Name.
_ALVO = {"tools/call": "name", "resources/read": "uri", "prompts/get": "name"}


def log(mensagem: str) -> None:
    agora = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{agora} [agente] {mensagem}", file=sys.stderr, flush=True)


class ErroMCP(Exception):
    """Erro de protocolo (JSON-RPC error) devolvido pelo servidor MCP."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"Erro MCP {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Trace:
    """Contexto W3C de uma Task: o trace-id fica, o span-id muda por request."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def do_header(cls, valor: str | None) -> Trace:
        partes = (valor or "").strip().split("-")
        if (
            len(partes) == 4
            and len(partes[1]) == 32
            and len(partes[2]) == 16
            and len(partes[3]) == 2
            and all(c in "0123456789abcdef" for c in "".join(partes[1:]))
            and partes[1] != "0" * 32
        ):
            return cls(trace_id=partes[1], flags=partes[3])
        # Sem traceparent valido do cliente A2A: a Task abre um trace proprio.
        return cls(trace_id=secrets.token_hex(16))

    def novo_traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


class ClienteMCP:
    """Cliente MCP de vida longa (o objeto vive; o protocolo nao tem sessao)."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self._http = httpx2.AsyncClient(timeout=timeout)
        self._ids = itertools.count(1)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def request(self, metodo: str, params: dict[str, Any], trace: Trace) -> dict[str, Any]:
        req_id = next(self._ids)
        traceparent = trace.novo_traceparent()
        corpo = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": metodo,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
                    "traceparent": traceparent,
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if metodo in _ALVO:
            headers["Mcp-Name"] = str(params[_ALVO[metodo]])

        alvo = f" {headers['Mcp-Name']}" if "Mcp-Name" in headers else ""
        retry = " (retry MRTR)" if "requestState" in params else ""
        log(f"MCP -> {metodo}{alvo} id={req_id}{retry} traceparent={traceparent}")

        resposta = await self._http.post(self.url, json=corpo, headers=headers)
        mensagem = self._ler_resposta(resposta, req_id)
        if "error" in mensagem:
            erro = mensagem["error"] or {}
            log(f"MCP <- id={req_id} erro {erro.get('code')}: {erro.get('message')}")
            raise ErroMCP(erro.get("code"), str(erro.get("message")))
        resultado = mensagem.get("result") or {}
        log(f"MCP <- id={req_id} resultType={resultado.get('resultType')}")
        return resultado

    @staticmethod
    def _ler_resposta(resposta: httpx2.Response, req_id: int) -> dict[str, Any]:
        tipo = resposta.headers.get("content-type", "")
        if tipo.startswith("text/event-stream"):
            # O servidor pode escolher SSE; a resposta e o evento com o nosso id.
            for linha in resposta.text.splitlines():
                if linha.startswith("data:"):
                    try:
                        evento = json.loads(linha[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(evento, dict) and evento.get("id") == req_id:
                        return evento
            raise ErroMCP(None, f"stream SSE sem resposta para o id {req_id}")
        try:
            mensagem = resposta.json()
        except ValueError:
            raise ErroMCP(None, f"resposta HTTP {resposta.status_code} sem JSON-RPC") from None
        if not isinstance(mensagem, dict):
            raise ErroMCP(None, f"resposta HTTP {resposta.status_code} sem JSON-RPC")
        return mensagem

    # -- atalhos ----------------------------------------------------------

    async def listar_tools(self, trace: Trace) -> list[dict[str, Any]]:
        return list((await self.request("tools/list", {}, trace)).get("tools") or [])

    async def ler_resource(self, uri: str, trace: Trace) -> str:
        resultado = await self.request("resources/read", {"uri": uri}, trace)
        return "".join(c.get("text", "") for c in resultado.get("contents") or [])

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace: Trace,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self.request("tools/call", params, trace)

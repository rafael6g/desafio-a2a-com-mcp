"""Regras do dominio das salas.

Tudo que e decisao de negocio mora aqui, do lado do servidor MCP: validacao de
sala e de politica, deteccao de conflito e calculo de alternativas. O agente
nunca importa este modulo; ele so conhece as tools pelo protocolo.

As reservas vivem em memoria: as de dados/reservas.json sao carregadas na
subida e as criadas durante a execucao somem no restart (fora de escopo).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(os.environ.get("DADOS_DIR") or Path(__file__).resolve().parent.parent / "dados")

# Horario de Sao Paulo, como a politica declara (-03:00, sem horario de verao).
FUSO = timezone(timedelta(hours=-3))
ABRE = time(8, 0)
FECHA = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class RegraVioladaError(Exception):
    """Erro de execucao da tool: vira isError: true no resultado."""


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime

    def sobrepoe(self, outro: Intervalo) -> bool:
        return self.inicio < outro.fim and outro.inicio < self.fim

    @property
    def inicio_iso(self) -> str:
        return self.inicio.isoformat()

    @property
    def fim_iso(self) -> str:
        return self.fim.isoformat()


def _carregar_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def ler_politica() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_politica() -> str:
    primeira = ler_politica().splitlines()[0]
    chave, _, valor = primeira.partition(":")
    if chave.strip() != "versao" or not valor.strip():
        raise RuntimeError("dados/politica-de-uso.md nao declara a versao na primeira linha")
    return valor.strip()


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise RegraVioladaError(f"Data invalida: {valor} nao esta em ISO 8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO)
    return dt.astimezone(FUSO)


class Agenda:
    """Salas fixas e reservas em memoria do processo."""

    def __init__(self) -> None:
        self.salas: list[dict] = _carregar_json("salas.json")
        self._por_id = {s["id"]: s for s in self.salas}
        self.reservas: list[dict] = _carregar_json("reservas.json")
        self._trava = threading.Lock()
        numeros = [int(r["id"].split("-")[-1]) for r in self.reservas if r["id"].split("-")[-1].isdigit()]
        self._proximo = max(numeros, default=0) + 1

    # -- validacao -------------------------------------------------------

    def sala(self, sala_id: str) -> dict:
        sala = self._por_id.get(sala_id)
        if sala is None:
            raise RegraVioladaError(f"Sala inexistente: {sala_id}")
        return sala

    def validar(self, sala_id: str, inicio: str, fim: str) -> tuple[dict, Intervalo]:
        """Aplica as validacoes de sala e de politica, na ordem do enunciado."""
        sala = self.sala(sala_id)
        intervalo = Intervalo(_instante(inicio), _instante(fim))
        if intervalo.fim <= intervalo.inicio:
            raise RegraVioladaError(ERRO_INTERVALO)
        if not self._dentro_da_janela(intervalo):
            raise RegraVioladaError(ERRO_JANELA)
        if intervalo.fim - intervalo.inicio > DURACAO_MAXIMA:
            raise RegraVioladaError(ERRO_DURACAO)
        return sala, intervalo

    @staticmethod
    def _dentro_da_janela(intervalo: Intervalo) -> bool:
        dia = intervalo.inicio.date()
        abre = datetime.combine(dia, ABRE, tzinfo=FUSO)
        fecha = datetime.combine(dia, FECHA, tzinfo=FUSO)
        return abre <= intervalo.inicio <= fecha and abre <= intervalo.fim <= fecha

    # -- consulta --------------------------------------------------------

    def conflitos(self, sala_id: str, intervalo: Intervalo) -> list[dict]:
        return [
            r
            for r in self.reservas
            if r["sala"] == sala_id and intervalo.sobrepoe(Intervalo(_instante(r["inicio"]), _instante(r["fim"])))
        ]

    def alternativas(self, sala: dict, intervalo: Intervalo) -> list[str]:
        """Salas livres no intervalo com capacidade >= a pedida.

        No maximo tres, por capacidade crescente e, no empate, por id.
        """
        candidatas = [
            s
            for s in self.salas
            if s["id"] != sala["id"] and s["capacidade"] >= sala["capacidade"] and not self.conflitos(s["id"], intervalo)
        ]
        candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
        return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]

    # -- escrita ---------------------------------------------------------

    def reservar(self, sala_id: str, intervalo: Intervalo, responsavel: str) -> dict | None:
        """Cria a reserva se a sala continua livre; devolve None se ocupada."""
        with self._trava:
            if self.conflitos(sala_id, intervalo):
                return None
            reserva = {
                "id": f"res-{self._proximo:04d}",
                "sala": sala_id,
                "inicio": intervalo.inicio_iso,
                "fim": intervalo.fim_iso,
                "responsavel": responsavel,
            }
            self._proximo += 1
            self.reservas.append(reserva)
            return reserva

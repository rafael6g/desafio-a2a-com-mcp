# A Ponte: Central de Salas

Entrega do desafio **A Ponte: um agente A2A com MCP por dentro**. São dois processos separados:

| Processo | Pasta | Porta | Endpoint |
|---|---|---|---|
| Servidor MCP (SDK oficial `mcp` 2.3.0, revisão 2026-07-28, Streamable HTTP sem sessão) | [servidor-mcp/](servidor-mcp/) | 7301 | `POST /mcp` |
| Agente: host MCP por dentro, servidor A2A v1.0 (JSON-RPC 2.0) por fora | [agente/](agente/) | 7300 | `GET /.well-known/agent-card.json`, `POST /a2a` |

O agente não usa LLM. Ele interpreta o pedido em formato fixo e decide por regra. Não implementa nenhuma regra de sala: conflito, política e alternativas são decisões do servidor MCP, e o agente só traduz protocolo.

```
cliente A2A ──SendMessage/GetTask──▶ agente (7300) ──tools/list, resources/read, tools/call──▶ servidor MCP (7301)
            ◀── Task (estado, artifact) ──        ◀── complete | input_required + requestState ──
```

## Como rodar

Pré-requisitos: Python 3.10 ou superior e, de preferência, o [uv](https://docs.astral.sh/uv/). O validador também roda com Python 3.10+ e não tem dependência. A entrega foi validada em Python 3.10 e 3.13.

### 1. Clonar e instalar (versões travadas em `pyproject.toml` / `uv.lock`)

```bash
git clone https://github.com/rafael6g/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
uv sync --locked
```

Sem uv, com pip (o `requirements.txt` é o `uv.lock` exportado):

```bash
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Gerar a chave de integridade do requestState

O servidor MCP lê a chave de `REQUEST_STATE_SECRET`, nunca do código, e se recusa a subir sem ela ou com menos de 32 bytes. Gere uma chave sua (32 bytes aleatórios em hex) **no terminal do servidor MCP**:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

```powershell
# PowerShell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

Não commite essa chave. Para o teste de restart (passo 12 do fluxo do avaliador), reinicie o servidor **no mesmo terminal**: a chave continua no ambiente e o `requestState` emitido antes do restart continua válido.

### 3. Subir os dois processos, cada um em um terminal

```bash
# terminal 1: servidor MCP (o stderr é o log de requests)
uv run python servidor-mcp/servidor.py

# terminal 2: agente
uv run python agente/agente.py
```

Com pip, rode os mesmos comandos sem `uv run`, com o `.venv` ativado em cada terminal.

### 4. Rodar o validador (com os dois processos recém-iniciados)

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

### Variáveis opcionais

| Variável | Padrão | Uso |
|---|---|---|
| `MCP_PORT` / `MCP_HOST` / `MCP_PATH` | `7301` / `127.0.0.1` e `::1` / `/mcp` | onde o servidor MCP escuta |
| `AGENTE_PORT` / `AGENTE_HOST` | `7300` / `127.0.0.1` e `::1` | onde o agente escuta |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | servidor MCP que o agente consome |
| `AGENTE_URL_PUBLICA` | derivada do `Host` do request | base da URL publicada no Agent Card |
| `DADOS_DIR` | `dados/` do repositório | origem de salas, reservas e política |

Sem host explícito, os dois processos escutam em `127.0.0.1` e em `::1`. No Windows, `localhost` tenta primeiro o IPv6, e uma porta só em IPv4 custava cerca de 2 s por request.

### Conferências manuais do fluxo do avaliador

```bash
curl -s http://localhost:7300/.well-known/agent-card.json

# passo 7: SendMessage de sala ocupada (corpo de exemplos/wire/08)
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
  -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d "$(python3 -c "import json;print(json.dumps(json.load(open('exemplos/wire/08-a2a-send-message.json'))['request']['body']))")"
# -> TASK_STATE_INPUT_REQUIRED, "alternativas: sala-fusca, sala-mirante"

# passo 8: continuação (troque <task-id>)
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"m2","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"<task-id>"}}}'
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"<task-id>"}}'
```

## Onde a ponte acontece

**Ida (MCP para A2A).** O `tools/call` de `reservar_sala` volta do servidor MCP com `resultType: "input_required"`, e o agente transforma isso em pausa em [`Ponte._aplicar_resultado`](agente/agente.py#L265-L291). Ele lê a única entrada de `inputRequests` (a chave atribuída pelo servidor, o campo e o `enum` do `requestedSchema`) e guarda numa `Pausa` ligada àquela Task: nome da tool, argumentos, chave, alternativas e o `requestState` intacto ([agente/agente.py:282](agente/agente.py#L282)). Depois põe a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids>`, na ordem do enum ([agente/agente.py:291](agente/agente.py#L291)). O cliente MCP do agente ([agente/cliente_mcp.py](agente/cliente_mcp.py)) não tem callback de elicitation: ele devolve o `input_required` cru, e quem decide é a Task.

**Volta (A2A para MCP).** O `SendMessage` com `taskId` e `escolha=<valor>` cai em [`Ponte._continuar`](agente/agente.py#L229-L263). Uma escolha fora do enum mantém a Task pausada e repete a linha. `escolha=recusar` vira `{"action": "decline"}`, e um id válido vira `{"action": "accept", "content": {"sala": <id>}}`. O agente então repete o **mesmo** `tools/call`, com um id de JSON-RPC novo (o contador do `ClienteMCP` nunca repete), `inputResponses` com a mesma chave e o `requestState` ecoado byte a byte ([agente/agente.py:255](agente/agente.py#L255)).

**No servidor.** Do lado do servidor, a pergunta nasce em [`_perguntar`](servidor-mcp/servidor.py#L174-L213), que devolve o `InputRequiredResult`. O retry é atendido em [`_retomar`](servidor-mcp/servidor.py#L216-L249), que reconstrói o pedido original a partir do `requestState` já verificado pelo SDK e conclui a reserva, ou devolve `reservado: false` na recusa.

O estado pausado é por Task: cada `Registro` tem a sua `Pausa` e um lock próprio. Duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`.

## Decisões técnicas

**Stack.** Python com o SDK oficial `mcp==2.3.0` (`MCPServer`) no servidor, servido com `stateless_http=True` e `json_response=True`. O próprio transporte do SDK recusa com -32602/HTTP 400 o request sem `io.modelcontextprotocol/protocolVersion` ou sem `clientCapabilities` no `_meta`, e com -32020 o header que não bate com o corpo. Nenhum lado infere versão ou capability de request anterior.

**Como o requestState é protegido.** O MRTR é conduzido pelo corpo da tool, que devolve um `InputRequiredResult` com um `requestState` em texto claro: o pedido inteiro (sala, início, fim, responsável), a chave da pergunta e as alternativas oferecidas. Antes de ir para o fio, o `RequestStateBoundary` do SDK sela esse texto com `RequestStateSecurity(keys=[REQUEST_STATE_SECRET])` ([servidor-mcp/servidor.py:125](servidor-mcp/servidor.py#L125)):
- AES-256-GCM, com chave derivada por HKDF-SHA256 (cifra e autentica);
- envelope com `iat`/`exp`, vínculo ao método, à tool e ao digest dos argumentos, e audiência igual ao nome do servidor.

No retry, o boundary verifica o token antes de a tool rodar. Token adulterado, expirado, ou apresentado com argumentos diferentes dos selados recebe -32602 (`Invalid or expired requestState`), e o motivo real vai só para o stderr. Mesmo que passasse, `_retomar` usa os valores selados, não os reenviados. A escolha também precisa estar entre as alternativas seladas: se não estiver, ou se a sala foi ocupada entre as rodadas, o servidor pergunta de novo. A chave exige no mínimo 32 bytes ([servidor-mcp/servidor.py:59](servidor-mcp/servidor.py#L59)), e o hex de `token_hex(32)` é decodificado para os 32 bytes aleatórios. Não há segredo no código.

**Por quanto tempo vale.** 10 minutos (`TTL_REQUEST_STATE = 600`, [servidor-mcp/servidor.py:47](servidor-mcp/servidor.py#L47)). O servidor não guarda nada entre o `input_required` e o retry: tudo viaja no token, e por isso um retry depois de reiniciar o processo funciona, desde que a mesma chave esteja no ambiente.

**Capability.** Antes de emitir a elicitation, o servidor exige elicitation em form mode no `_meta` daquele request. Sem ela, responde -32021 com `data.requiredCapabilities = {"elicitation": {"form": {}}}` e HTTP 400 ([servidor-mcp/servidor.py:252](servidor-mcp/servidor.py#L252)). Pela spec, `elicitation: {}` sem modos equivale a form, e declarar só `url` não conta. O agente declara exatamente `{"elicitation": {"form": {}}}` em todo request.

**Capabilities anunciadas.** O servidor anuncia só o que o escopo pede: `{"tools": {...}, "resources": {...}}`. Prompts e subscriptions estão fora de escopo. Com `subscriptions=False`, o SDK não anuncia `listChanged` nem `resources.subscribe`. Os handlers de prompts, que o `MCPServer` registra sempre, são retirados logo depois da criação do servidor, porque o SDK não tem API pública para isso, e `prompts/*` passa a responder -32601.

**Onde fica o estado das Tasks.** Em memória do agente, no dicionário `Ponte.tasks` ([agente/agente.py:114](agente/agente.py#L114)), indexado pelo id da Task. O `requestState` fica na `Pausa` do `Registro`, fora do objeto `task` que é serializado nas respostas: não aparece no card, no artifact nem em mensagem nenhuma, e também não é logado. Estado terminal (COMPLETED, CANCELED, FAILED) é definitivo, e um `SendMessage` para ele recebe o erro -32004 (`UnsupportedOperationError`). Uma Task inexistente recebe -32001. As reservas criadas também ficam em memória, no servidor MCP, e não sobrevivem a restart (fora de escopo).

**Host MCP.** O cliente MCP do agente é um cliente HTTP escrito à mão sobre `httpx2`, e não o `mcp.Client` do SDK, por dois motivos. Primeiro, o `Client`, quando recebe um callback de elicitation, declara `{"form": {}, "url": {}}` e conduz o `input_required` sozinho, e o desafio exige a declaração exata de form mode e a pausa visível na Task. Segundo, assim cada request carrega explicitamente `_meta` (`protocolVersion`, `clientInfo`, `clientCapabilities`, `traceparent`) e os headers espelhados (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`). A cada Task o agente descobre a tool por `tools/list`, monta os argumentos a partir do `inputSchema` descoberto, lê `politica://uso` para extrair a versão da primeira linha e só então chama `reservar_sala`. A versão lida vai no campo `politica` do artifact.

**traceparent.** O `traceparent` do header A2A é guardado na Task ao abri-la. Todo request MCP daquela Task, inclusive o retry, leva o mesmo trace-id com um span-id novo no `_meta`. Sem header, a Task abre um trace próprio. O servidor registra no stderr, por request: método, id, nome da tool ou URI, se é retry, o `traceparent`, e na saída o HTTP e o `resultType` ou o código de erro. O log é feito num middleware ASGI por fora do transporte, então registra também os requests recusados. Exemplo do par pausa e retry:

```
<- tools/call id=6 name=reservar_sala traceparent=00-<trace-id>-621bd449f883866b-01
-> id=6 http=200 resultType=input_required
<- tools/call id=7 name=reservar_sala retry(requestState+inputResponses) traceparent=00-<trace-id>-b1a5d24e1907a1b5-01
-> id=7 http=200 resultType=complete
```

**Erros de execução.** As regras levantam `ToolError`, e o SDK devolve `isError: true` com o texto prefixado por `Error executing tool <nome>: `, prefixo que o enunciado aceita. O agente leva esse texto, sem mudar nada, para a mensagem de status e o histórico da Task em `TASK_STATE_FAILED`, por exemplo `Error executing tool reservar_sala: Sala inexistente: sala-inexistente`.

**Respostas A2A.** Seguem a forma dos exemplos em `exemplos/wire/`: `SendMessage` e `GetTask` devolvem `result.task`. A Task passa por `TASK_STATE_SUBMITTED` e `TASK_STATE_WORKING` antes de pausar ou terminar, e as transições ficam no stderr do agente.

## Saída do validador

Última execução, a partir de um clone limpo do fork seguindo este README (processos e validador em Python 3.10.21), com os dois processos recém-iniciados:

```
trace-id desta execucao: afabf1d6b0cac8ac23b968d10128eaaf
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

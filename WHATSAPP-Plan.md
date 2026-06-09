# WHATSAPP-Plan.md — Plano de Implementação do Canal WhatsApp (WPPConnect)

> **Objetivo:** adicionar o canal **WhatsApp** ao gSage AI com funcionamento equivalente ao
> canal Telegram existente (`src/telegram_worker/`), usando o
> **[WPPConnect Server](https://github.com/wppconnect-team/wppconnect-server)** exclusivamente
> como **API REST interna** (sem uso da UI/Swagger dele por operadores ou usuários finais).
> Inclui interface web de administração com **status da conexão (conectado / pareando /
> desconectado)** e exibição do **QR Code** quando o pareamento for necessário.

---

## 1. Visão Geral da Arquitetura

```
                                       gsage-internal (Docker network)
┌─────────────┐  webhooks (onmessage,   ┌──────────────────┐
│  wppconnect  │  status-find, qrcode)  │ whatsapp-worker  │── agent.arun() ──► Ollama/LLM
│  (Node, API  │ ──────────────────────►│ (FastAPI + loop  │── Postgres (sessões/mensagens)
│  REST :21465)│ ◄────────────────────── │  de lifecycle)   │── Redis (rate limit, status/QR cache)
└──────┬───────┘   REST: start-session,  └──────────────────┘
       │           send-message, status…
       │ REST (proxy admin)
┌──────┴───────┐        ┌─────────────┐        ┌──────────────┐
│ backend_api  │◄───────│  frontend   │        │ channel_sender│ (continuação HITL/
│ /admin/whats…│  HTTPS │ (React SPA) │        │ _deliver_whatsapp) → wppconnect
└──────────────┘        └─────────────┘        └──────────────┘
```

Decisões principais (espelhando os padrões já existentes no projeto):

| Decisão | Escolha | Racional |
|---|---|---|
| Integração WhatsApp | WPPConnect Server como container interno, **somente API** | Sem porta exposta ao host; `backend_api` é o único gateway para a UI; o worker é o único consumidor de webhooks. |
| Recebimento de mensagens | **Webhook** do wppconnect → `whatsapp-worker` (HTTP interno) | Diferente do Telegram (long-poll), o wppconnect empurra eventos. Padrão análogo ao Teams, mas isolado num worker dedicado (como o telegram-worker) para não bloquear o `backend_api` durante `agent.arun()`. |
| Identidade da sessão WhatsApp | 1 sessão wppconnect por `GSageInterfaceProfile` org-wide (`interface='whatsapp'`) | Mesmo modelo do Telegram (1 bot token por profile). Nome da sessão = `org_<org_slug>` armazenado em `interface_config.session_name`. |
| Resolução de remetente | Novo campo `GSageUser.whatsapp_id` (telefone E.164, só dígitos) | Mesmo modelo do `telegram_id` / `teams_aad_object_id`. |
| Persistência de conversas | Reuso de `GSageChannelConversation` / `GSageChannelMessage` / `GSageTenantSession` com `channel="whatsapp"` | Os modelos já preveem WhatsApp nos comentários do schema (`telegram | discord | slack | whatsapp`). Nenhuma migração nessas tabelas. |
| Status/QR para a UI | Worker mantém cache em Redis (`whatsapp:status:{profile_id}`, `whatsapp:qrcode:{profile_id}`); `backend_api` lê o cache e faz proxy de ações (start/logout) | A UI nunca fala com o wppconnect; QR chega por webhook/start-session e fica disponível em < 1 RTT. |
| Escopo v1 | Apenas mensagens de **texto**, conversas **1:1** (ignorar grupos `@g.us`, mídia, `fromMe`) | Igual ao Telegram v1 (`filters.TEXT & ~filters.COMMAND`). |

---

## 2. Container `wppconnect` (API-only)

### 2.1 docker-compose.yml — novo serviço

```yaml
  wppconnect:
    image: wppconnect/server-cli:latest
    container_name: gsage-wppconnect
    restart: unless-stopped
    environment:
      SECRET_KEY: ${WPPCONNECT_SECRET_KEY}
    volumes:
      - wppconnect_tokens:/usr/src/wpp-server/tokens     # sessões persistidas (sobrevive a restart)
      - wppconnect_userdata:/usr/src/wpp-server/userDataDir
    # SEM "ports:" — acessível apenas na rede interna (API-only, UI/Swagger inacessíveis de fora)
    networks:
      - gsage-internal
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:21465/healthz || exit 1"]
      interval: 60s
      timeout: 10s
      retries: 5
      start_period: 60s
    deploy:
      resources:
        limits:
          memory: 1G   # Chromium headless por sessão; revisar para multi-org

  whatsapp-worker:
    image: gsage-python-dev-image
    container_name: gsage-whatsapp-worker
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./src:/app/src
      - ./requirements.txt:/app/requirements.txt
    command: >
      bash -c "pip install --quiet -r requirements.txt
      && python -m src.whatsapp_worker.main"
    # SEM "ports:" — webhook recebido apenas via rede interna (http://whatsapp-worker:8002)
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      backend_api:
        condition: service_healthy
      wppconnect:
        condition: service_healthy
    networks:
      - gsage-internal
    deploy:
      resources:
        limits:
          memory: 512M
```

Novos volumes: `wppconnect_tokens`, `wppconnect_userdata`.

> Nota: confirmar na imagem escolhida o endpoint de healthcheck (algumas versões expõem
> `/healthz`, outras apenas `/api-docs`). Ajustar o healthcheck no momento da implementação.

### 2.2 Endpoints do WPPConnect usados (contrato da integração)

| Uso | Endpoint | Quem chama |
|---|---|---|
| Gerar token da sessão | `POST /api/{session}/{SECRET_KEY}/generate-token` | worker (lifecycle) |
| Iniciar sessão (com webhook) | `POST /api/{session}/start-session` body `{ "webhook": "http://whatsapp-worker:8002/webhook/{profile_id}", "waitQrCode": false }` | worker / backend (ação "Conectar") |
| Status da sessão | `GET /api/{session}/status-session` → `CLOSED \| INITIALIZING \| QRCODE \| CONNECTED` | worker (poll de reconciliação) |
| QR Code atual | `GET /api/{session}/qrcode-session` (e evento webhook `qrcode`) | worker → cache Redis |
| Enviar mensagem | `POST /api/{session}/send-message` body `{ "phone": "<E.164>", "message": "...", "isGroup": false }` | worker (resposta) e `channel_sender` (HITL) |
| Logout (desparear) | `POST /api/{session}/logout-session` | backend (ação "Desconectar") via worker |
| Encerrar sessão | `POST /api/{session}/close-session` | worker (profile desativado) |

Token de sessão (Bearer) gerado via `SECRET_KEY` é cacheado em Redis
(`whatsapp:token:{profile_id}`) e regenerado sob demanda (idempotente).

---

## 3. Novo pacote `src/whatsapp_worker/`

Estrutura espelhando `src/telegram_worker/`, com os módulos extras exigidos pelo modelo
webhook + lifecycle de pareamento:

```
src/whatsapp_worker/
├── __init__.py
├── main.py                 # entry point: FastAPI (webhooks) + loop de lifecycle/hot-reload
├── wpp_client.py           # cliente HTTP (httpx) do WPPConnect Server — única camada que conhece a API dele
├── session_manager.py      # reconcilia InterfaceProfiles ativos ↔ sessões wppconnect; publica status/QR no Redis
├── webhook.py              # rotas FastAPI: POST /webhook/{profile_id} (onmessage, status-find, qrcode)
├── handler.py              # pipeline de mensagem (clone do telegram_worker/handler.py, 3 fases)
├── resolver.py             # resolve_whatsapp_sender(session, phone, org_id) → GSageUser via whatsapp_id
├── rate_limiter.py         # Redis: org/dia + usuário/hora (chaves ratelimit:*:whatsapp:*)
├── formatting.py           # Markdown LLM → formatação WhatsApp (*negrito*, _itálico_, ```mono```) + split_text
└── channel_spec.py         # ChannelSpec p/ scripts/generate_channels_docs.py
```

### 3.1 `main.py` — lifecycle (análogo ao `TelegramWorker`)

- Sobe um app FastAPI/uvicorn em `0.0.0.0:8002` (interno) com as rotas de webhook.
- Loop de hot-reload a cada `WHATSAPP_RELOAD_INTERVAL` (default 300 s), igual ao
  `_hot_reload_loop` do Telegram:
  1. Carrega `GSageInterfaceProfile` ativos com `interface='whatsapp'`.
  2. Para cada profile: garante token (`generate-token`), consulta `status-session` e
     publica em Redis. Se `CLOSED`/`DISCONNECTED` e o profile estiver ativo, chama
     `start-session` passando o webhook (re-pareamento gera evento `qrcode`).
  3. Profiles removidos/desativados → `close-session` + limpeza das chaves Redis.
- `SIGTERM`/`SIGINT` → shutdown gracioso (uvicorn + cancelamento do loop), como no Telegram.

### 3.2 `webhook.py` — eventos inbound

`POST /webhook/{profile_id}` — valida que `profile_id` corresponde a um profile ativo
conhecido (cache do session_manager); responde `200` imediatamente e processa em
`asyncio.create_task` (o `agent.arun()` pode levar minutos — não bloquear o ack):

| Evento wppconnect | Ação |
|---|---|
| `onmessage` | Filtros: ignorar `fromMe=true`, `isGroupMsg=true`/ids `@g.us`, tipos ≠ `chat` (mídia → responder aviso "apenas texto suportado" 1×/conversa). Extrai `from` (`5511999999999@c.us` → dígitos), `body`, `id` → chama `handler.handle_message(...)`. |
| `status-find` / `onstatechange` | Atualiza `whatsapp:status:{profile_id}` no Redis (TTL 24 h) com `{status, phone_number?, updated_at}`. Em `CONNECTED`, apaga o QR do cache. |
| `qrcode` | Salva `{base64, generated_at, attempt}` em `whatsapp:qrcode:{profile_id}` (TTL 120 s — QR expira ~60 s e o wppconnect emite um novo). |
| Demais eventos | Log debug e descarta. |

Segurança do webhook: rede interna apenas + verificação do `profile_id` + (opcional v1.1)
segredo compartilhado em query string registrado no `start-session`.

### 3.3 `handler.py` — pipeline (paridade 1:1 com o Telegram)

Mesmas 14 etapas e a mesma estrutura de **3 fases transacionais** do
`telegram_worker/handler.py` (manter o split Fase 1 commit → Fase 2 agent → Fase 3 persist,
que evita o deadlock do post-hook do Agno):

1. Extrair `phone`, `chat_id` (= `from` completo, ex. `5511...@c.us`), `text`, `message_id`.
2. Resolver profile/org (recebido do webhook — sem ambiguidade, 1 sessão = 1 profile).
3. `resolve_whatsapp_sender()` — desconhecido → responder aviso de não cadastrado com o
   número detectado (espelho da mensagem do Telegram).
4. Rate limits org/dia e usuário/hora (Redis).
5. Indicador de digitação: `wpp_client.set_typing(session, phone, True)`
   (endpoint `typing` do wppconnect) — equivalente ao `ChatAction.TYPING`.
6. `get_or_create_conversation(channel="whatsapp")` — **reutilizar** o
   `telegram_worker/conversation_manager.py` (já é genérico via parâmetro `channel`;
   ver §8 "Refatoração opcional").
7. Persistir `GSageChannelMessage` inbound (`status=PROCESSING`).
8–10. Org, membership, dept, `TenantContext(interface="whatsapp")`, `load_interface_profiles(..., "whatsapp", ...)`, `build_agent(...)` — o `agent_factory` já possui o prompt de canal `whatsapp` (`agent_factory.py:395`) e já trata `whatsapp` como interface plain-text (`agent_factory.py:1494`).
11. `agent.arun()` com retry/backoff (copiar `_MAX_AGENT_RETRIES`).
11b. HITL paused → `process_approval_delegations(...)` idêntico ao Telegram.
12. `apply_filters_to_text(FilterContext(interface="whatsapp"))` → `markdown_to_whatsapp()`
    → `split_text(max_len)` → `send-message` por chunk.
13. Persistir outbound + `COMPLETED` + `message_count += 2`.
14. Erros → inbound `FAILED` + mensagem genérica; `finally:` → `cleanup_agent_mcp(agent)` +
    `engine.dispose()` (mesmo cuidado com o busy-loop do anyio).

### 3.4 `formatting.py`

WhatsApp usa formatação própria (não HTML): `*negrito*`, `_itálico_`, `~tachado~`,
` ```mono``` `. Converter o Markdown do LLM:

- `**bold**`/`***bold***` → `*bold*`; headers `#` → `*linha*`; listas mantidas;
  links `[label](url)` → `label (url)` (WhatsApp auto-linkifica URL — mesma decisão do Telegram);
  tabelas → texto plano (mesma estratégia de strip do Telegram); escapar nada (não há HTML).
- `split_text(text, max_len)` com `DEFAULT_MAX_LEN = 4000` (limite prático seguro;
  o hard cap do WhatsApp é 65.536, mas mensagens longas degradam UX).

### 3.5 `channel_spec.py`

```python
CHANNEL_SPEC = ChannelSpec(
    interface="whatsapp",
    summary="Canal WhatsApp via WPPConnect Server (API interna) — webhook handler ...",
    config_storage="interface_profile.interface_config",
    interface_config_schema={
        "type": "object",
        "required": ["session_name"],
        "properties": {
            "session_name": {"type": "string", "description": "Nome da sessão no WPPConnect (ex.: org_gsage)."},
            "phone_number": {"type": "string", "description": "Somente leitura — preenchido após pareamento."},
        },
    },
    env_vars=[...],                       # ver §6
    cli_module="src.ops_cli.channels.whatsapp",
    worker_modules=["src.whatsapp_worker.main"],
    webhook_paths=["POST http://whatsapp-worker:8002/webhook/{profile_id} (interno)"],
    prerequisites=[
        "Container gsage-wppconnect saudável na rede interna.",
        "WPPCONNECT_SECRET_KEY definido no .env (igual no wppconnect e no worker).",
        "Um número de WhatsApp dedicado + celular para escanear o QR no primeiro pareamento.",
    ],
    source_files=[...],
)
```

---

## 4. Backend API — `src/backend_api/app/api/v1/admin_whatsapp.py`

Rotas admin (prefixo `/v1/orgs/{org_id}/admin`, guard `require_org_admin`, registradas em
`router.py` ao lado de `admin_interfaces`):

| Rota | Ação | Implementação |
|---|---|---|
| `GET /whatsapp/{profile_id}/status` | Status p/ UI | Lê `whatsapp:status:{profile_id}` do Redis (fallback: proxy `status-session` via `wpp_client`). Retorna `{ status: "connected"\|"qrcode"\|"initializing"\|"disconnected", phone_number, updated_at }`. Valida que o profile pertence ao `org_id` e tem `interface='whatsapp'`. |
| `GET /whatsapp/{profile_id}/qrcode` | QR p/ pareamento | Lê `whatsapp:qrcode:{profile_id}` → `{ qrcode_base64, generated_at }`; `404` se não houver QR pendente. |
| `POST /whatsapp/{profile_id}/connect` | Iniciar/parear | Chama `start-session` no wppconnect (via `wpp_client` compartilhado em `src/shared/`). Idempotente. |
| `POST /whatsapp/{profile_id}/disconnect` | Logout (desparear) | `logout-session` + limpa cache Redis. Confirmação na UI (ação destrutiva: exige novo QR). |
| `POST /whatsapp/{profile_id}/restart` | Reinício | `close-session` + `start-session`. |

Schemas Pydantic novos em `schemas/admin.py`: `WhatsappStatusOut`, `WhatsappQrcodeOut`.

> O `wpp_client.py` deve morar em `src/shared/channels/wppconnect.py` (ou ser importado do
> worker) para ser usado por: worker, backend (ações), `channel_sender` e `ops_cli` — uma
> única implementação do contrato HTTP.

---

## 5. Interface Web (web_client)

### 5.1 `api/admin.ts`

Novos tipos e funções: `WhatsappStatus`, `getWhatsappStatus(orgId, profileId)`,
`getWhatsappQrcode(...)`, `connectWhatsapp(...)`, `disconnectWhatsapp(...)`,
`restartWhatsapp(...)`.

### 5.2 `pages/admin/InterfacesPage.tsx` + novo componente `WhatsAppConnectionDialog.tsx`

- Na tabela de profiles, linhas com `interface === 'whatsapp'` ganham um **badge de status
  ao vivo** (React Query, `refetchInterval: 10_000`):
  - 🟢 `connected` → "Conectado (+55 11 9…)";
  - 🟡 `qrcode` / `initializing` → "Aguardando pareamento";
  - ⚪ `disconnected` → "Desconectado".
- Botão/ícone "Conexão" (ex. `QrCode` do lucide) abre o **`WhatsAppConnectionDialog`**:

```
┌─ Conexão WhatsApp — org_gsage ────────────────────────────┐
│  Status: 🟡 Aguardando pareamento                          │
│                                                            │
│            ┌──────────────────────┐                        │
│            │      [QR CODE]       │   Escaneie com o       │
│            │   <img base64 png>   │   WhatsApp do número   │
│            │                      │   dedicado:            │
│            └──────────────────────┘   Aparelhos conectados │
│   QR renova automaticamente a cada ~20s                    │   
│                                                            │
│  [Conectar/Gerar QR]  [Reiniciar]  [Desconectar]  [Fechar] │
└────────────────────────────────────────────────────────────┘
```

Comportamento:
- Ao abrir, consulta status. Se `disconnected`, mostra botão **Conectar/Gerar QR**
  (chama `connect`, entra em polling).
- Enquanto `status === 'qrcode'`: busca `/qrcode` com `refetchInterval: 5_000` e re-renderiza
  o `<img src="data:image/png;base64,...">` (o wppconnect renova o QR sozinho; o cache Redis
  acompanha via webhook).
- Quando virar `connected`: esconde QR, mostra número pareado + toast de sucesso.
- **Desconectar** pede confirmação (Dialog) — força novo pareamento.
- Estados de erro (worker fora do ar / wppconnect unhealthy) → banner com mensagem do backend.

### 5.3 Cadastro do usuário — `pages/admin/UsersPage.tsx`

Adicionar campo **WhatsApp (telefone E.164)** ao lado do `telegram_id` existente
(`UsersPage.tsx:355-384`), com placeholder `5511999999999` e hint de normalização.

### 5.4 i18n

Novas chaves em `web_client/public/locales/{en,pt-BR,es,fr,de,it,ja,ar}/translation.json`:
`admin.whatsapp.{status,connected,pairing,disconnected,scanQr,qrHint,connect,disconnect,restart,disconnectConfirm,...}` e `admin.users.whatsappId`.

---

## 6. Configuração / Settings

`src/shared/config/settings.py` (ao lado do bloco `telegram_*`, `settings.py:307-313`):

| Variável | Default | Descrição |
|---|---|---|
| `WPPCONNECT_BASE_URL` | `http://wppconnect:21465` | URL interna da API do WPPConnect Server. |
| `WPPCONNECT_SECRET_KEY` | — (obrigatória) | Secret key do wppconnect p/ `generate-token`. Sensível. |
| `WHATSAPP_WORKER_WEBHOOK_BASE` | `http://whatsapp-worker:8002` | Base do webhook registrado no `start-session`. |
| `WHATSAPP_RELOAD_INTERVAL` | `300` | Intervalo (s) de reconciliação profiles ↔ sessões. `0` desliga. |
| `WHATSAPP_RATE_LIMIT_ORG_DAILY` | `200` | Mensagens/org/dia (UTC). |
| `WHATSAPP_RATE_LIMIT_USER_HOURLY` | `30` | Mensagens/usuário/hora móvel. |
| `WHATSAPP_MAX_MESSAGE_LENGTH` | `4000` | Tamanho do chunk outbound. |

Atualizar `.env.example` / `installer/compose` conforme padrão dos demais canais.

---

## 7. Banco de Dados

**Única migração nova** (Alembic):

```python
# add_whatsapp_id_to_users
op.add_column("gsage_users", sa.Column(
    "whatsapp_id", sa.String(30), nullable=True,
    comment="WhatsApp phone (E.164 digits-only) for sender resolution"))
op.create_index("ix_gsage_users_whatsapp_id", "gsage_users", ["whatsapp_id"])
```

+ Model `GSageUser.whatsapp_id` (ao lado de `telegram_id`, `user.py:104`)
+ Schemas admin (`AdminUserOut`/`AdminUserUpdate`, `admin.py:182,217`) com
  validador de normalização (remover `+`, espaços, hífens; só dígitos, 8–15 chars).

Resolução do remetente (`resolver.py`): comparar `whatsapp_id` com o `from` normalizado.
**Atenção BR:** números antigos podem estar cadastrados sem o 9º dígito enquanto o WhatsApp
reporta com (ou vice-versa). Estratégia: match exato primeiro; fallback comparando
`DDI+DDD+últimos 8 dígitos`. Documentar no docs/channels/whatsapp.md.

Nenhuma alteração em `gsage_interface_profiles`, `gsage_channel_conversations`,
`gsage_channel_messages` (já são channel-agnósticas).

---

## 8. `channel_sender.py` — entrega assíncrona (HITL / continuações)

Em `src/backend_api/app/services/channel_sender.py`:

- `deliver_response()`: novo branch `elif source == "whatsapp": await _deliver_whatsapp(...)`.
- `_deliver_whatsapp(session, text, db)` (espelho de `_deliver_telegram`,
  `channel_sender.py:85`):
  1. Busca `GSageChannelConversation` com `channel == "whatsapp"` e `session_id`.
  2. Busca o profile `whatsapp` ativo da org → `session_name`.
  3. `markdown_to_whatsapp()` + `split_text()` → `wpp_client.send_message(session_name, phone=chat_id, ...)` por chunk.

**Refatoração opcional (recomendada, pequena):** mover
`telegram_worker/conversation_manager.py` para `src/shared/channels/conversation_manager.py`
(ele já é 100% genérico — recebe `channel` por parâmetro) e importar de ambos os workers,
mantendo um re-export no caminho antigo para compatibilidade.

---

## 9. `ops_cli` — `src/ops_cli/channels/whatsapp.py`

Espelho de `channels/telegram.py`, registrado em `ops_cli/channels/__init__.py`:

```bash
# Criar/atualizar profile org-wide (cria sessão "org_<slug>")
python -m ops_cli channels whatsapp upsert --org-slug gsage --description "SOC WhatsApp"

# Status + QR no terminal (renderizar com lib `qrcode` ASCII, ou salvar PNG)
python -m ops_cli channels whatsapp status --org-slug gsage
python -m ops_cli channels whatsapp qrcode --org-slug gsage [--out qr.png]

# Ações
python -m ops_cli channels whatsapp connect    --org-slug gsage
python -m ops_cli channels whatsapp disconnect --org-slug gsage
```

`upsert` segue a mesma normalização do Telegram (denylist vazio = canal liberado,
`telegram.py:115-122`).

---

## 10. Documentação

- `docs/channels/whatsapp.md` — gerado por `scripts/generate_channels_docs.py` a partir do
  `channel_spec.py` (mesmo fluxo de telegram/teams/email). Incluir guia do operador:
  1. Subir `wppconnect` + `whatsapp-worker`; 2. criar profile (UI ou ops_cli);
  3. abrir diálogo de conexão na UI Admin → Interfaces; 4. escanear QR; 5. cadastrar
  `whatsapp_id` nos usuários; 6. troubleshooting (QR expirado, desconexão pelo celular,
  banimento/limites do WhatsApp, perda do volume `wppconnect_tokens`).
- Atualizar `docs/channels/README.md` com a linha do novo canal.

---

## 11. Fases de Implementação

| Fase | Entrega | Arquivos principais | Critério de aceite |
|---|---|---|---|
| **0 — Infra** | Containers `wppconnect` + `whatsapp-worker` (esqueleto), settings, .env | `docker-compose.yml`, `settings.py`, `.env.example` | `docker compose up wppconnect` saudável; worker sobe e loga "no active profiles". |
| **1 — Identidade** | Migração `whatsapp_id`, model, schemas, campo na UsersPage | `migrations/`, `user.py`, `schemas/admin.py`, `UsersPage.tsx`, locales | Admin salva/edita telefone normalizado. |
| **2 — Lifecycle + QR** | `wpp_client`, `session_manager`, webhook de status/qrcode, cache Redis | `src/shared/channels/wppconnect.py`, `whatsapp_worker/{main,session_manager,webhook}.py` | Criar profile → sessão inicia → QR aparece no Redis → ao escanear, status vira `CONNECTED` e persiste após restart do worker. |
| **3 — Pipeline de mensagens** | handler 3-fases, resolver, rate limiter, formatting | `whatsapp_worker/{handler,resolver,rate_limiter,formatting}.py` | Mensagem de usuário cadastrado recebe resposta do agente; desconhecido recebe aviso; limites aplicados; mensagens persistidas. |
| **4 — Admin API** | Endpoints status/qrcode/connect/disconnect/restart | `api/v1/admin_whatsapp.py`, `router.py`, `schemas/admin.py` | `curl` autenticado retorna status/QR; ações refletem no wppconnect. |
| **5 — UI Web** | Badge de status + `WhatsAppConnectionDialog` + i18n | `InterfacesPage.tsx`, `WhatsAppConnectionDialog.tsx`, `api/admin.ts`, locales | Fluxo completo de pareamento feito 100% pela UI, com QR atualizando sozinho e status virando "Conectado". |
| **6 — HITL/continuação** | `_deliver_whatsapp` no channel_sender | `channel_sender.py` | Aprovação HITL entrega a resposta no chat do WhatsApp. |
| **7 — Operação** | ops_cli, channel_spec, docs | `ops_cli/channels/whatsapp.py`, `channel_spec.py`, `docs/channels/whatsapp.md` | Docs geradas; CLI funcional dentro do container. |
| **8 — Testes** | Unit + integração | `tests/unit/whatsapp/*`, `tests/integration/*` | Ver §12. |

Dependências: 2→0/1; 3→2; 4→2; 5→4; 6→3; 7/8 ao final de cada fase.

---

## 12. Testes

**Unit (mock do wppconnect via `respx`/`httpx.MockTransport`):**
- `formatting`: Markdown → WhatsApp (negrito, listas, links, tabelas, split em 4000).
- `resolver`: match exato, normalização E.164, fallback 9º dígito BR, isolamento por org.
- `rate_limiter`: limites org/dia e user/hora (espelhar testes do Telegram, se existirem).
- `session_manager`: reconciliação (profile novo → start; desativado → close; status/QR no Redis).
- `webhook`: roteamento de eventos; rejeição de `profile_id` desconhecido; ignore de
  grupos/`fromMe`/mídia.

**Integração:**
- `admin_whatsapp`: auth (somente org admin), 404 cross-org, proxy de ações.
- Pipeline fim-a-fim com wppconnect mockado: webhook `onmessage` → mensagens
  INBOUND/OUTBOUND persistidas + `send-message` chamado.

---

## 13. Riscos e Pontos de Atenção

1. **Natureza não-oficial do WPPConnect** (WhatsApp Web automation): risco de quebra em
   updates do WhatsApp e de banimento de número. Mitigação: número dedicado, rate limits
   conservadores, `restart` exposto na UI, e a camada `wpp_client` isolando o contrato
   (trocar por API oficial Cloud futuramente sem tocar no handler).
2. **QR expira (~60 s)**: o wppconnect emite novos QRs via webhook; o cache Redis com TTL
   curto + polling de 5 s na UI garante QR sempre válido. Nunca servir QR "velho" (checar
   `generated_at`).
3. **Persistência da sessão**: o pareamento sobrevive a restarts apenas se o volume
   `wppconnect_tokens` for preservado. Documentar em backup/restore.
4. **Memória**: cada sessão wppconnect = 1 Chromium headless (~300–500 MB). Com multi-org,
   revisar o limite do container (1 G atende ~1–2 sessões).
5. **Webhook sem auth nativa**: aceitável só porque a rede é interna e sem porta no host;
   se algum dia o wppconnect sair do compose, adicionar HMAC/secret no path do webhook.
6. **Mensagens duplicadas**: webhooks podem reentregar eventos; deduplicar por
   `channel_message_id` (chave `SETNX` curta no Redis ou unique check antes do INSERT).
7. **Concorrência**: como o processamento é `asyncio.create_task` por mensagem, mensagens
   em rajada do mesmo usuário podem processar em paralelo — serializar por
   `(profile_id, chat_id)` com um lock asyncio/Redis para preservar a ordem da conversa.

---

## 14. Fora de Escopo (v1) — backlog

- Mídia inbound/outbound (imagens, áudio, documentos) — modelo `GSageChannelMessage` já
  comporta extensão futura.
- Grupos (`@g.us`), botões/listas interativas, reactions, read receipts.
- Múltiplos números por organização (múltiplos profiles whatsapp por org — o índice único
  parcial em `gsage_interface_profiles` hoje permite 1 org-wide por interface).
- Streaming de resposta (Telegram também não tem).

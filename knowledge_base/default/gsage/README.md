# gSage SOC AI

* Project's site: [https://gsage.org/](https://gsage.org/)
* Overview video: [https://youtu.be/J4g0blz7PnE](https://youtu.be/J4g0blz7PnE)


gSage SOC AI is an on-premise SOC assistant that combines AI agents, structured tooling, and human review workflows to help security teams monitor, investigate, triage, and respond faster -- *exceptional operational efficiency without losing security or control*.

It is designed for organizations that want AI-assisted security operations without sending core workflows or operational data to a third-party SaaS by default. The platform runs in your environment, integrates with your internal services via APIs, and keeps observability, auditability, and control as first-class concerns.

It includes a complete orchestration framework with logging, auditing, permissions, and controlled tool access. Prompts can be scheduled, and users can interact with the platform through either the web UI, Telegram, or email.

The platform is designed to support custom tools and integrations for antivirus platforms, EDR, SIEM, ticketing, WAF, proxies, HR systems, Active Directory, and other operational systems.

gSage SOC AI can execute workflows like the examples below.

## Example workflows

### Example 1 - Investigating a malicious hash

*gSage, search the environment for file hash `abc123`, gather antivirus update status from the affected machines, identify the associated users, open a ticket for each impacted host, add the hash to the blacklist, and email me the final status.*

In that scenario, gSage SOC AI should execute automatically the following steps:
1. search for hash `abc123` in an integrated SIEM or EDR (ex. Bitdefender, CrowdStrike, Trellix) through an API integration (tools).
2. query antivirus APIs for update status and host details on each affected machine.
3. look up the responsible users in Active Directory and HR systems through APIs or direct database access.
4. open a ticket in the integrated ticketing platform for each affected machine, including the collected context.
5. add hash `abc123` to a centralized blacklist through an API.
6. send an email summary with host status, affected users, and opened tickets.

### Example 2 - Responding to a phishing alert

*gSage, a user reported a suspicious email. Analyze the message content, inspect links and attachments, and tell me whether it is phishing. If it is phishing, create an incident ticket and send an email summary for human analyst review.*

In that scenario, gSage SOC AI should execute automatically the following steps:
1. analyze the email content with an LLM to identify phishing indicators
2. inspect links with an integrated URL analysis tool
3. inspect attachments with an integrated sandbox or malware analysis tool
4. if the email is classified as phishing, block the sender with an email security integration and create an incident ticket through the ticketing API
5. return a summary including the classification, the indicators found, and the actions taken
6. email the human analyst with the final summary and incident ticket details

### Example 3 - Scheduled prompt for suspicious applications

*gSage, schedule the prompt below to run every day at 9:00 AM and email me the result:*

```
gSage, search the environment for the applications listed below. For each machine that has one of these applications installed, tell me the machine name and the currently logged-in user. The applications are: "AnyDesk", "TeamViewer", "OpenVPN".
```

In that scenario, gSage SOC AI can:
1. schedule the prompt to run daily at 9:00 AM using its internal scheduler
2. execute the scheduled search against integrated inventory or asset management systems
3. collect the machine name and logged-in user for each device where a listed application is found
4. compile the results and send an email summary to the analyst

See more examples in [PROMPT_EXAMPLES.md](PROMPT_EXAMPLES.md).

## Tools and integrations

The platform is designed to make new integrations straightforward to add. See [docs/dev/TOOLS.md](docs/dev/TOOLS.md) for the list of implemented tools and guidance for building new ones.

In the repository [guardiankey/gsage-soc-ai-tools](https://github.com/guardiankey/gsage-ai-soc), you can find a collection of community-contributed tools and integrations that can be used with this platform.

## Why this project exists

Security teams lose time switching between dashboards, repeating the same checks, and manually gathering context before they can make a decision. gSage SOC AI is built to reduce that friction while improving both operational efficiency and security.

With it, a team can:

- ask security questions from a web UI, Telegram, or by email.
- orchestrate AI agents with controlled tool access.
- keep a full audit trail of what was executed and why.
- run asynchronous analysis jobs without blocking the user experience.
- keep data, logs, and infrastructure on-premise.

## What it does

At a high level, the platform combines:

- FastAPI for API and agent orchestration.
- React for a lightweight web interface.
- Celery and RedBeat for background and scheduled work.
- PostgreSQL and Weaviate for operational data and semantic memory.
- Redis for broker, cache, locks, and pub/sub.
- Elasticsearch for audit logs, metrics, and structured app logs.
- an MCP server for controlled tool execution, with permissions and org/user/department scoping.
- an external LLM provider selected through `LLM_PROVIDER`, with support for Ollama, OpenAI, Gemini, and DeepSeek.

The result is a modular SOC workflow platform that can support investigation, enrichment, memory, email-driven automation, and internal security operations with clearer operational boundaries.

## Core strengths

- **On-premise first**: designed to run in your own infrastructure.
- **AI with guardrails**: tool execution is isolated and permission-aware.
- **Multi-tenant by design**: organizations, users, and permissions are part of the core model.
- **Audit-friendly**: logs, traces, and execution context are treated as product features, not afterthoughts.
- **Practical deployment**: Docker Compose is enough for the intended scale.

## Innovative features

- Email interface: send prompts and receive responses through email, with support for attachments and rich formatting.
- Flexible tool integration: easily add new tools and integrations with a clear execution model.
- Tool permissions and scoping: control which tools are available to which users, organizations and departments.
- Scheduled prompts: run prompts on a schedule.
- Multi-tenancy (organization and department): support multiple organizations with isolated data and permissions.
- Knowledge base in organization, department, user or global scope.
- Rules engine to approve or reject tool execution based on custom logic.
- Dynamic datastores in user or department scope, which allows managing structured data for variety of use cases.
- Integrated with a blocklist/allowlist management service (curator) that can be used by tools and rules.
- CLI client for terminal-based interaction.
- Administrative console tool.
- Mermaid diagrams generation tool for many objects that are rendered in the web UI.

## Who this is for

- internal SOC and security engineering teams
- MSSP-like environments that need strong isolation and traceability
- teams experimenting with AI-assisted operations but unwilling to give up deployment control
- organizations that want web and email entry points for the same analysis workflow

## Architecture in one minute

The platform is split into focused services:

- `backend`: API, orchestration, health checks, core application logic.
- `web-ui`: user-facing web interface.
- `mcp-server`: isolated tool execution layer.
- `celery workers` and `celery_beat`: asynchronous execution by queue.
- `email-worker`: email ingestion and response flow.
- `telegram-worker`: Telegram integration for prompt submission and response delivery.
- `postgres`, `redis`, `elasticsearch`, `weaviate`, `minio`: persistence, queues, cache, logs, and metrics.
- `curator`: service to manage blacklists, allowlists, and other operational data that can be used by tools and rules.
- external LLM provider: model inference through Ollama, OpenAI, Gemini, DeepSeek, or an OpenAI-compatible endpoint.

If you want the implementation-oriented view, start with [docs/dev/TOOLS.md](docs/dev/TOOLS.md).

## Install

gSage ships as a single installer tarball that provisions a Linux host
(Debian/Ubuntu or RHEL/Rocky, x86_64 or arm64) with Docker, pulls the
official images from the public registry, and runs an interactive wizard.

### Production (one-host install)

```bash
curl -fsSL http://raw.githubusercontent.com/guardiankey/gsage-ai-soc/refs/heads/main/dist/get-gsage.sh | sudo bash
```

The installer:

- checks prerequisites (RAM, disk, ports) and installs Docker + Python if missing,
- asks for admin credentials, host port, and the LLM provider of your choice,
- writes a `0600` `.env` at `/opt/gsage/shared/.env` with freshly generated secrets,
- brings the stack up with `docker compose`, waits for `backend_api` to be healthy,
- installs `gsage-cli`, `gsage-admin`, and `gsage-get-admin-key` into `/usr/local/bin/`.

Only **one port** is published publicly: the web UI (default `8080`). The
backend API is reached through the frontend reverse proxy at `/api`.

Re-running the installer on an existing host upgrades in place while
preserving `.env` and all volumes.

### Post-install: channels

- After install, you can access the web UI at `http://localhost:8080` with the admin credentials you set during installation.
- You may want to set up a Telegram bot or an email channel for prompt submission and response delivery. Use the following scripts to configure those channels.
- In web UI, menu Admin, you can also manage channels, organizations, users, tools, and other settings. You have to configure the Telegram ID and email addresses for each user that should have access to those channels.

```bash
sudo /opt/gsage/current/configure-email-channel.sh      # IMAP/SMTP mailbox.
sudo /opt/gsage/current/configure-telegram-channel.sh   # @BotFather token.
```

### Post-install: tools


- You have to configure tools with your credentials for them to work. For example, if you want to use the VirusTotal tool, you need to set `VT_API_KEY` in `.env` with your VirusTotal API key. The same applies to other tools that require credentials.
- If you need a tool, check if there is an existing one in [gsage-soc-ai-tools](https://github.com/guardiankey/gsage-soc-ai-tools). If not, you can build your own tool and add it to the platform. See [docs/dev/TOOLS.md](docs/dev/TOOLS.md) for guidance on building new tools. We encourage contributions of tools to the community repository so others can benefit from them.
- Configure your tools by setting variables at `/opt/gsage/shared/.env` or through the web UI admin panel. After changing `.env`, you need to recreate the affected containers for the changes to take effect. In save dir there is a `.env.example` file that you can use as a reference for the variables to set.

```bash
docker compose -f /opt/gsage/current/compose/docker-compose.yml up -d --force-recreate backend_api mcp-server celery-worker-tools 
```

### Development checkout

For contributors running the repo directly (not the packaged installer):

```bash
cp .env.example .env          # then edit the relevant variables
docker compose build
docker compose up -d
docker compose exec backend alembic upgrade head
docker compose exec backend python scripts/init-elasticsearch.py
docker compose logs backend | grep 'Admin API Key'
```

## Typical usage

After the stack is up:

1. open the web UI on `http://localhost:8080`
2. confirm the backend health endpoint is returning healthy status
3. configure the desired LLM provider and credentials in `.env`
4. create the initial data needed by your environment
5. start sending analysis requests through the UI or the email flow

For implementation details and internal conventions, use the auxiliary docs instead of treating this README as the source of truth.

## CLI Client

In addition to the web UI and email interface, a command-line client is available for terminal-based interaction.

### Setup

```bash
# Install CLI dependencies
pip install -r requirements-cli.txt

# Set your API key
export GSAGE_API_KEY='your-api-key-here'
export GSAGE_API_HOST='http://localhost:8080'  # optional, defaults to localhost:8080

# Run the CLI
python -m cli_client.main

# Or use the launcher script
./run-cli.sh
```

### Features

- Natural conversation interface — just type your questions
- Rich markdown rendering with syntax highlighting
- Conversation management (create, switch, list messages)
- Debug mode for troubleshooting
- Tango color scheme for readability

See [cli_client/README.md](cli_client/README.md) for detailed documentation.

## Troubleshooting

### Containers start but services fail internally

Check container status first:

```bash
docker compose ps
```

Then inspect logs for the failing service:

```bash
docker logs gsage-backend --tail 100
docker logs gsage-flask-ui --tail 100
docker logs gsage-email-worker --tail 100
docker logs gsage-celery-beat --tail 100
```

### Backend cannot connect to PostgreSQL, Redis, or Elasticsearch

In Docker Compose, do not use `localhost` for internal dependencies. Use service names:

- PostgreSQL: `postgres`
- Redis: `redis`
- Elasticsearch: `elasticsearch`
- MCP server: `mcp-server`

If you changed `.env`, recreate the containers so they reload environment variables:

```bash
docker compose up -d --force-recreate
```

### Email worker reports missing tables

The database schema was not migrated yet. Run:

```bash
docker compose exec backend alembic upgrade head
```

### Flask UI fails during startup with missing Python modules

Rebuild the affected image after dependency changes:

```bash
docker compose build flask-ui
docker compose up -d --force-recreate flask-ui
```

### Celery Beat or workers cannot reach Redis

Confirm `REDIS_HOST=redis` in `.env` and recreate the containers:

```bash
docker compose up -d --force-recreate
```

### LLM provider is unreachable

gSage AI expects the selected LLM provider to be external to this stack. Verify:

- `LLM_PROVIDER` matches the backend you intend to use
- the provider-specific endpoint is correct: `OLLAMA_BASE_URL`, `OPENAI_BASE_URL`, or `DEEPSEEK_BASE_URL`
- the required API key is present for the selected provider
- the Docker containers can resolve and reach the configured endpoint

If you use self-hosted Ollama on a separate GPU server, also confirm the hostname mapping strategy in Docker Compose.

## Documentation

This README stays intentionally light. More technical material lives here:

- [docs/dev/README.md](docs/dev/README.md): developer-oriented architecture overview
- [docs/dev/TOOLS.md](docs/dev/TOOLS.md): tools and execution model

## Issues and support

If you find a bug, open an issue in the repository's GitHub Issues tab.

When reporting a problem, include:

- what you expected to happen
- what actually happened
- the service affected
- relevant logs
- reproduction steps
- environment details such as Docker version, OS, and which LLM provider you configured

If the issue involves secrets, credentials, or sensitive customer data, redact them before posting.

## Contributing

Contributions are welcome, but they should be pragmatic and easy to review.
We encourage to share tools and integrations that others can use, but we ask to avoid large refactors or architectural changes without prior discussion.

Recommended flow:

1. open an issue first for significant changes
2. keep pull requests focused on one problem
3. include tests when behavior changes
4. update supporting docs when setup or usage changes
5. avoid mixing refactors with functional changes unless they are tightly coupled

Before contributing, review the developer docs under [docs/dev/README.md](docs/dev/README.md) and [docs/dev/TOOLS.md](docs/dev/TOOLS.md).

By submitting contributions, you agree to the repository's contribution and licensing terms described in [LICENSE.md](LICENSE.md).

## License

This project is distributed under the gSage AI Business Source License 1.0. 

Read the full terms in [LICENSE.md](LICENSE.md).

## Final note

gSage AI is meant to be useful before it tries to be clever. The project is opinionated about auditability, service boundaries, and operational clarity because those are the traits that matter when security workflows leave the whiteboard and hit production.

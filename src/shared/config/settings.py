"""gSage AI — Shared configuration."""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ── Application ──────────────────────────────────────────
    app_env: str = "development"
    debug: bool = False
    encryption_key: str = "CHANGE-ME"  # base64-encoded 32 bytes

    # ── PostgreSQL ───────────────────────────────────────────
    postgres_db: str = "gsage"
    postgres_user: str = "gsage"
    postgres_password: str = ""
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # SQLAlchemy async pool sizing. The default ``pool_size=5`` /
    # ``max_overflow=10`` is too tight for the MCP server when an LLM
    # agent fans out many tool calls in parallel — every blocked
    # request that waits for a connection past the MCP 30 s read
    # timeout risks being cancelled mid-flight and leaving an
    # "idle in transaction" zombie connection behind.
    database_pool_size: int = 10
    database_max_overflow: int = 20
    # Recycle pooled connections after this many seconds. Defends
    # against connections silently killed by an upstream load
    # balancer / firewall.
    database_pool_recycle_seconds: int = 1800
    # Server-side safety nets enforced by PostgreSQL itself.
    # ``statement_timeout`` aborts any single statement that runs
    # longer than the limit. ``idle_in_transaction_session_timeout``
    # kills sessions left in an open transaction with no further
    # activity (the failure mode that triggered this setting).
    # Values are in milliseconds; set to 0 to disable.
    database_statement_timeout_ms: int = 20000
    database_idle_in_tx_timeout_ms: int = 30000

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Redis ────────────────────────────────────────────────
    redis_password: str = ""
    redis_host: str = "redis"
    redis_port: int = 6379

    @property
    def redis_url(self) -> str:
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"

    @property
    def celery_broker_url(self) -> str:
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/1"

    @property
    def celery_result_backend(self) -> str:
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/2"

    # ── Elasticsearch ────────────────────────────────────────
    elasticsearch_url: str = "http://elasticsearch:9200"
    elasticsearch_index_prefix: str = "gsage-"
    # Prefix for agent run trace indices: {prefix}agno-traces-{YYYY-MM-DD}
    elasticsearch_trace_index_prefix: str = "gsage-"
    elasticsearch_trace_retention_days: int = 90
    # Redis buffer flush settings (used by Celery Beat + elasticsearch_ingest task)
    elasticsearch_buffer_flush_interval: int = 60    # seconds between flush cycles
    elasticsearch_buffer_max_batch: int = 5_000      # max docs popped per cycle

    # ── Rate Limiting ───────────────────────────────────────
    # Global defaults — can be overridden per org / per API key
    rate_limit_enabled: bool = True
    rate_limit_default_rpm: int = 600  # requests per minute (per org)
    rate_limit_user_rpm: int = 300     # requests per minute (per user within org)

    # ── Public application URL ──────────────────────────────
    # Absolute base URL where the gSage web application is reachable from
    # end-user devices.  Used to build absolute links delivered to users
    # via channels that render plain text without a known origin
    # (Telegram, e-mail) — e.g. knowledge-base download citations.
    # Must include scheme and host (no trailing slash), e.g.
    #   http://localhost:3000   or   https://gsage.example.com
    public_base_url: str = "http://localhost:3000"

    # ── LLM Provider ─────────────────────────────────────────
    # Controls which backend is used when no org-level override is set.
    # Valid values: "ollama" | "openai" | "deepseek" | "anthropic" | "gemini"
    llm_provider: str = "ollama"

    # ── Ollama (managed via gsage-ollama container) ────────────────────
    ollama_base_url: str = "http://ollama:11434"
    ollama_maker_model: str = "llama3.1:8b"

    # ── OpenAI ───────────────────────────────────────────────
    # Set openai_base_url to use any OpenAI-compatible endpoint.
    openai_api_key: str = ""
    openai_base_url: str = ""  # empty → use official OpenAI endpoint
    openai_maker_model: str = "gpt-4o-mini"

    # ── DeepSeek ─────────────────────────────────────────────
    # DeepSeek uses an OpenAI-compatible Chat API.
    # Embeddings are NOT available — falls back to keyword search.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_maker_model: str = "deepseek-chat"
    
    # ── Google Gemini ─────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_maker_model: str = "gemini-1.5-flash"

    # ── Anthropic ─────────────────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_maker_model: str = "claude-3-5-haiku-latest"

    # ── Embeddings (Ollama — used by Weaviate text2vec-ollama module) ─────
    # The gsage-ollama entrypoint creates a custom model (nomic-embed-ctx8k) from
    # nomic-embed-text with expanded num_ctx to avoid context-length errors.
    # Dimension: nomic-embed-text / nomic-embed-ctx8k → 768
    ollama_embedding_model: str = "nomic-embed-ctx8k"
    # Context window (tokens) sent in `options.num_ctx` for every /api/embed
    # call AND baked into the Ollama Modelfile at container startup (same var
    # used by docker/ollama/entrypoint.sh). Must be ≤ the embedding model's
    # native max context. Default 8192 matches nomic-embed-ctx8k.
    ollama_embed_num_ctx: int = 8192
    # Maximum characters per chunk during document ingestion. Must stay well
    # below ollama_embedding_num_ctx converted to characters (rule of thumb:
    # 1 token ≈ 3-4 chars for English/Portuguese, less for dense/base64/tables).
    # 2500 chars is a safe ceiling for an 8k-ctx embedder.
    ingest_chunk_size: int = 2500

    # ── Weaviate ─────────────────────────────────────────────────────────
    # Vector database for knowledge base semantic search.
    # Anonymous access enabled by default (no API key needed for self-hosted).
    weaviate_host: str = "weaviate"
    weaviate_port: int = 8080
    weaviate_grpc_port: int = 50051
    weaviate_api_key: str = ""  # empty = anonymous access
    # gRPC/REST init timeout (seconds). Raise if startup health checks time out
    # under load (default weaviate-client init timeout is only 2s).
    weaviate_init_timeout: int = 30
    # Skip the client-side startup health checks entirely. Useful when the
    # cluster is known-good but intermittently slow to answer gRPC pings.
    weaviate_skip_init_checks: bool = False
    # ── Agent system prompt ──────────────────────────────────
    # Overrides the built-in default system prompt for ALL agents.
    # When empty (default), the hardcoded _DEFAULT_SYSTEM_PROMPT is used.
    # The org-level system_prompt (set via UI/API) is always appended on top.
    agent_default_system_prompt: str = ""
    # ── Agent context control ─────────────────────────────────
    # Number of past conversation turns (user + assistant + tool calls) included
    # in the LLM context for every request.  Reducing this directly reduces
    # context window usage; increase it for better long-conversation coherence.
    agent_num_history_runs: int = 5
    # Maximum characters of a single tool output text stored in the agent history.
    # Responses larger than this limit are truncated with a notice, preventing
    # large tool outputs (e.g. bulk Zabbix data) from flooding the context window.
    # 0 = no limit (not recommended for models with small context windows).
    agent_tool_output_max_chars: int = 40000

    # ── Knowledge base auto-injection (per-turn preamble) ───────────────────
    # When enabled, every chat turn prepends a short ``<kb_hints>`` block to
    # the user message containing the most relevant saved notes for the
    # current user/org.  This nudges the LLM towards using saved memories
    # without forcing it to call ``search_knowledge_base`` first.
    # Set to false to disable the per-turn lookup entirely (saves one
    # Weaviate round-trip per turn at the cost of less consistent recall).
    kb_auto_inject_enabled: bool = True
    # Minimum Weaviate similarity score (cosine, 0..1) required for a note
    # to be auto-injected.  Lower values surface more notes (noisier);
    # higher values keep the preamble tight.  ``None`` (score unavailable)
    # results are kept when set to 0.0.
    kb_auto_inject_min_score: float = 0.65
    # Top-N user-private notes to include in the preamble.
    kb_auto_inject_user_top_n: int = 1
    # Top-N org-wide / dept-wide notes to include in the preamble.
    kb_auto_inject_shared_top_n: int = 2
    # Maximum characters of each note preview shown in the preamble.
    kb_auto_inject_preview_chars: int = 200

    # ── MCP Server ───────────────────────────────────────────
    mcp_server_url: str = "http://mcp-server:8001"
    # Read timeout in seconds for MCP tool calls (default: 120s for slow tools like wikijs_editor)
    mcp_tool_timeout_seconds: int = 60

    # ── Mermaid validator tool ───────────────────────────────
    # Path to the mermaid-cli binary inside the mcp_server container.
    # The Dockerfile installs @mermaid-js/mermaid-cli globally so `mmdc`
    # is on PATH — override only for custom installations.
    mermaid_cli_bin: str = "mmdc"
    # Hard timeout for a single mmdc subprocess invocation. The first call
    # after container start is the slowest (Chromium warm-up); typical
    # subsequent calls take 2–6s.
    mermaid_validate_timeout_seconds: int = 45

    # ── CRUD Tools ───────────────────────────────────────────
    # CRUD tools expose direct DB read/write to the AI agent.
    # Disabled by default — enable only in trusted environments.
    crud_tools_enabled: bool = False
    crud_tools_allow_write: bool = False

    # ── DataStore ────────────────────────────────────────────
    # Dynamic data stores: org-scoped named stores with JSON Schema validation.
    datastore_enabled: bool = False
    datastore_max_stores_per_org: int = 20
    datastore_max_records_per_store: int = 1000
    datastore_max_record_size_bytes: int = 10240  # 10 KB

    # ── Custom Extensions ────────────────────────────────────
    # Python package containing custom BaseTool subclasses.
    # The package is auto-discovered by the registry at startup.
    # Set to "" to disable custom tool loading.
    custom_tools_module: str = "custom_code.tools"

    # ── Tool filtering ───────────────────────────────────────
    # Comma-separated patterns used by the MCP registry to decide which
    # tools get loaded at startup. Empty = no filter (all tools load).
    # DISABLED wins over ENABLED (deny > allow).
    # Pattern syntax (each entry):
    #   <glob>                -> matches tool.name (default)
    #   name:<glob>           -> matches tool.name
    #   category:<glob>       -> matches tool.category
    #   module:<glob>         -> matches the python module path
    #   re:<regex>            -> regex against tool.name
    # Globs follow fnmatch rules (*, ?, [abc]). See
    # docs-local/architecture/30-TOOL-DISCOVERY.md for examples.
    tools_enabled: str = ""
    tools_disabled: str = ""

    # ── Binary Databases (GeoIP, IP2Location, …) ─────────────
    # Root directory for binary database files mounted via Docker volume.
    # Run dbs/<category>/update.sh to populate.
    gsage_dbs_path: str = "/app/dbs"

    # ── Self-registration ────────────────────────────────────
    # When False (default), POST /api/v1/auth/register returns 403.
    # Set to True only in development / demo environments.
    allow_self_register: bool = False

    # ── Bootstrap admin ──────────────────────────────────────
    # Used once at startup to create the initial organization and admin user.
    # Has no effect if an admin user already exists.
    admin_email: str = ""
    admin_password: str = ""
    admin_org_name: str = "gSage SOC"

    # ── JWT ──────────────────────────────────────────────────
    jwt_secret_key: str = "CHANGE-ME"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7

    # ── SMTP (outgoing email) ─────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    # When False, the SMTP client accepts self-signed / untrusted TLS
    # certificates.  Set to False only for internal relays in trusted networks.
    smtp_validate_certs: bool = True
    smtp_from_email: str = "noreply@gsage.local"
    smtp_from_name: str = "gSage AI"
    # Default body format for outgoing emails: "html" | "text"
    # "html" auto-converts Markdown bodies; "text" sends plain-text only.
    smtp_default_format: str = "html"

    # ── Telegram Bot Worker ───────────────────────────────────
    # Rate limits for the Telegram channel (all values per UTC window).
    telegram_rate_limit_org_daily: int = 200
    telegram_rate_limit_user_hourly: int = 30
    # Maximum length of a single outgoing Telegram message (Telegram cap = 4096).
    telegram_max_message_length: int = 4096
    # Interval (seconds) to re-query the DB for active InterfaceProfiles
    # and perform hot-reload of bots. 0 = disable hot-reload.
    telegram_reload_interval: int = 300

    # ── Microsoft Teams Channel ───────────────────────────────────────────
    # Rate limits for the Teams channel (all values per UTC window).
    teams_rate_limit_org_daily: int = 200
    teams_rate_limit_user_hourly: int = 30
    # Microsoft Teams hard limit on a single text activity is 28 KB.
    # We chunk above this to be safe with markdown overhead.
    teams_max_message_length: int = 25_000
    # Microsoft Bot Framework public OpenID configuration URL — used to
    # validate inbound JWTs. Override only for sovereign/gov clouds.
    teams_bot_openid_metadata_url: str = (
        "https://login.botframework.com/v1/.well-known/openidconfiguration"
    )
    # Cache TTL (seconds) for AAD-Object-ID → email lookups via Microsoft
    # Graph (only used for first-contact resolution).
    teams_graph_email_cache_ttl: int = 86_400

    # ── Email Worker ──────────────────────────────────────────────────────
    # Rate limits for the Email channel (all values per UTC window).
    # Org-level: max inbound emails processed per organization per calendar day.
    email_rate_limit_org_daily: int = 100
    # User-level: max new email threads opened per user per rolling hour.
    email_rate_limit_user_hourly: int = 10
    # When True, emails are permanently deleted from the IMAP server after being
    # successfully read and dispatched. When False (default), they are only
    # marked as \Seen so they are not re-fetched on the next IDLE cycle.
    email_delete_after_process: bool = False
    # IMAP folder to move emails from unknown senders (no matching user in the
    # org). The folder is auto-created on first use.
    email_unknown_sender_folder: str = "Unknown-Senders"

    # ── MinIO (object storage — tool-generated files) ———————————─
    # Files are streamed to clients via the backend API proxy.
    # MinIO does NOT need to be externally reachable.
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "gsage"
    minio_secret_key: str = "changeme-strong-password"
    minio_secure: bool = False        # True when MinIO is behind TLS
    minio_bucket: str = "gsage-files"
    minio_template_bucket: str = "gsage-templates"
    minio_kb_originals_bucket: str = "gsage-kb-originals"
    # When running the admin console directly on the host (outside Docker), set
    # this to the host-accessible MinIO endpoint (e.g. localhost:9000).
    # Leave empty to use minio_endpoint (internal service hostname).
    admin_minio_endpoint: str = ""

    # ── Tool-Generated Files ────────────────────────
    # Default TTL for tool-generated files (hours). 0 = never expires.
    file_default_ttl_hours: int = 72
    # Hard limit on file size accepted by _store_file() (bytes).
    # Must be aligned with nginx ``client_max_body_size`` in web_client/nginx*.conf.
    file_max_size_bytes: int = 1_073_741_824  # 1 GB

    # ── GuardianKey (Adaptive Authentication) ────────────────────────────────
    # Post-credential risk check via GuardianKey API v2. Fail-open: if the API
    # is unreachable, login proceeds normally.
    # Credentials: https://gdn.guardiankey.io → Settings → Authgroup → Deploy tab
    gk_enabled: bool = False
    gk_api_url: str = "https://api.guardiankey.io"
    gk_org_id: str = ""
    gk_authgroup_id: str = ""
    gk_key: str = ""           # Base64 key from GuardianKey panel
    gk_iv: str = ""            # Base64 IV from GuardianKey panel
    gk_agent_id: str = "gSage-ai"
    gk_service_name: str = "gSageAI"
    gk_reverse_dns: bool = False   # Perform local reverse-DNS lookup (may add latency)
    gk_timeout_seconds: int = 4

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # Ignore extra fields from .env (e.g., database_url which is a @property)
    }

    @model_validator(mode="after")
    def _reset_empty_model_ids(self) -> "Settings":
        """Treat empty-string model IDs (e.g. OLLAMA_MAKER_MODEL=) as unset and apply defaults."""
        if not self.ollama_maker_model:
            self.ollama_maker_model = "llama3.1:8b"
        if not self.openai_maker_model:
            self.openai_maker_model = "gpt-4o-mini"
        if not self.deepseek_maker_model:
            self.deepseek_maker_model = "deepseek-chat"
        if not self.ollama_embedding_model:
            self.ollama_embedding_model = "nomic-embed-ctx8k"
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()

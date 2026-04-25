# gSage AI — CLI Client

Interactive command-line client for gSage AI.

## Features

- 🔐 API key authentication
- 💬 Natural conversation interface
- 🎨 Rich markdown rendering (via `rich`)
- 📊 Conversation management
- 🐛 Debug mode for troubleshooting
- 🌈 Tango color scheme

## Installation

```bash
# Install dependencies
pip install -r requirements-cli.txt
```

## Configuration

Set environment variables before running the client:

```bash
# Required
export GSAGE_API_KEY='your-api-key-here'

# Optional
export GSAGE_API_HOST='http://localhost:8000'  # default
export GSAGE_CONVERSATION_ID='uuid'            # resume existing conversation
export GSAGE_DEBUG='true'                       # enable debug mode
export GSAGE_OUTPUT_FORMAT='markdown'           # or 'plain'
```

### Getting an API Key

When you first start the backend with `ADMIN_EMAIL` configured, an admin user and API key are automatically created. The key is printed in the logs:

```bash
docker compose logs backend | grep "Admin API Key"
```

You'll see output like:

```
═══════════════════════════════════════════════════════════════
  Admin API Key (SAVE THIS NOW — shown only once):
  abc123def456...
  Use with: export GSAGE_API_KEY='abc123def456...'
═══════════════════════════════════════════════════════════════
```

**Important:** This key is shown only once during bootstrap. Save it immediately.

Alternatively, you can create additional API keys through the web UI or database.

## Usage

```bash
# Run the CLI client
python -m cli_client.main
```

## Commands

Once in the CLI:

- **`help`** — Show help information
- **`conversation new [title]`** — Create a new conversation
- **`conversation <id>`** — Switch to a specific conversation
- **`messages [limit]`** — Show recent messages (default: 10)
- **`debug`** — Toggle debug mode
- **`clear`** — Clear the screen
- **`exit`** / **`quit`** — Exit the CLI

**Default behavior:** Any text that is not a command will be sent as a message.

## Examples

```
> What is a DNS lookup?
```
ASSISTANT:
A DNS (Domain Name System) lookup is a process where a DNS resolver translates a human-readable domain name (like www.example.com) into an IP address (like 192.0.2.1) that computers use to identify each other on the network...

```
> How can I detect phishing emails?
```
ASSISTANT:
Here are key indicators to detect phishing emails:

1. **Sender verification**: Check if the sender's email address matches the official domain
2. **Suspicious links**: Hover over links to see their actual destination...

```
> conversation new Security Investigation
✓ Created conversation: Security Investigation

> Search the environment for hash abc123
```
ASSISTANT:
Searching for file hash `abc123` in the environment...

```
> messages 5
```
Messages (showing 3 of 3):

YOU:
What is a DNS lookup?

ASSISTANT:
A DNS (Domain Name System) lookup is a process...

YOU:
Search the environment for hash abc123

ASSISTANT:
Searching for file hash `abc123` in the environment...

```
> debug
Debug mode enabled

> exit
Thank you for using gSage AI. Stay secure! 🛡️
```


## Architecture

- **`config.py`** — Configuration from environment variables
- **`client.py`** — HTTP client for gSage AI REST API
- **`commands.py`** — Command handlers
- **`repl.py`** — Interactive REPL loop
- **`main.py`** — Entry point

## API Routes Used

The client communicates with these FastAPI endpoints:

- `POST /api/conversations` — Create conversation
- `GET /api/conversations/{id}` — Get conversation details
- `POST /api/conversations/{id}/messages` — Send message (agent executes)
- `GET /api/conversations/{id}/messages` — List messages

Authentication: `X-API-Key` header (see `src/backend/app/core/deps.py`)

## Troubleshooting

### Invalid API key

Make sure your API key is correctly set and exists in the database:

```sql
-- Check if API key exists (run inside postgres container)
SELECT id, name, is_active, expires_at, scoped_permissions
FROM gsage_api_keys 
WHERE is_active = true
ORDER BY created_at DESC;
```

Or verify the key hash matches:

```bash
# Generate hash from your key
echo -n 'your-api-key-here' | sha256sum

# Compare with database
docker compose exec postgres psql -U gsage -d gsage -c \
  "SELECT name, key_hash, is_active FROM gsage_api_keys WHERE is_active = true;"
```

### Connection refused

Ensure the backend is running:

```bash
cd /path/to/gsage-ai
docker-compose up backend
```

### Enable debug mode

```bash
export GSAGE_DEBUG=true
python -m cli_client.main
```

Or use the `debug` command inside the CLI.

## Quick Test

After setting up the backend and retrieving your API key:

```bash
# 1. Get the admin API key from logs
docker compose logs backend | grep "Admin API Key" | tail -1

# 2. Export the key
export GSAGE_API_KEY='your-key-from-logs'
export GSAGE_API_HOST='http://localhost:8000'

# 3. Test the CLI
python -m cli_client.main

# Inside the CLI:
> help
> What is DNS?
> conversation new Test Conversation
> messages
> exit
```

If everything works, you should see the agent respond to your question with formatted markdown output.


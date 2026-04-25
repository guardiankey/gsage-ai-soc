#!/bin/bash

# Commands to help debug async issues in the backend_api container. Run these commands on the host machine (not inside the container) to inspect the Python process and its sockets.
# Use for reference when investigating high CPU usage, hanging requests, or "input length exceeds context length" errors during document ingestion.

exit 0

#docker stats --no-stream
#ps auxw | grep python | grep xxx

PID=2186901
sudo ./.venv/bin/py-spy dump --pid $PID
sudo ./.venv/bin/py-spy top --pid $PID
sudo ./.venv/bin/py-spy dump --pid $PID --locals
sudo ./.venv/bin/py-spy record -o /tmp/telegram_flame.svg -d 10 --pid $PID
sudo ./.venv/bin/py-spy record -o /tmp/telegram_flame.txt -d 10 -f speedscope --pid $PID
sudo ./.venv/bin/py-spy dump --pid $PID --native
sudo ./.venv/bin/py-spy top --pid $PID --rate 200

PID=$(pgrep -f 'src.backend_api.*|gunicorn.*backend')
sudo ./.venv/bin/py-spy dump --pid $PID --locals > /tmp/dump.txt 2>&1
docker exec gsage-backend_api ss -tnp | grep -E 'CLOSE-WAIT|172.27.0' > /tmp/sockets.txt

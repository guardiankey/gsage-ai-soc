#!/bin/bash

echo "This script will delete all existing migration files and create a new baseline migration based on the current DB schema."
echo "Make sure you have a backup if you need to preserve any existing data or migration history."

read -p "Do you want to proceed? (yes/no) " confirmation
if [[ "$confirmation" != "yes" ]]; then
    echo "Aborting."
    exit 0
fi

rm -f src/migrations/versions/*.py
rm -f src/migrations/versions/*.pyc

alembic revision --autogenerate -m "initial_schema"
alembic upgrade head

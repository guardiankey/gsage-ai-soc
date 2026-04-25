"""gSage AI — Operator CLI (channel helpers + on-host admin operations).

Invoked from host-side sh wrappers via:

    docker compose exec -T backend_api python -m ops_cli ...

The package reuses ``src.shared.*`` models / session maker / encryption so
that writes go through the exact same data-layer contract as the REST API.
"""

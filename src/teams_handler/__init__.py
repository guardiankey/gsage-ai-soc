"""gSage AI — Microsoft Teams channel handler.

Reusable, library-style modules consumed by the FastAPI webhook router
``src/backend_api/app/api/v1/channels_teams.py`` and the outbound
delivery service ``channel_sender._deliver_teams``.

Mirrors ``src/telegram_worker/`` but is a library — there is no worker
process for Teams: the Microsoft Bot Framework requires a public HTTPS
webhook, which is hosted by ``backend_api``.
"""

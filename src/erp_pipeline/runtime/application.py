"""The deployable application.

    uvicorn erp_pipeline.runtime.application:app
    python -m erp_pipeline.api

Both routes end here. ``app`` is built on first attribute access rather than at
import time, so importing this module stays cheap and a tool that merely
inspects it does not open a database connection.
"""

from __future__ import annotations

import logging
from typing import Any

from erp_pipeline.runtime.settings import ConfigurationError, RuntimeSettings

LOGGER = logging.getLogger("erp_pipeline.runtime.application")


def create_production_app(
    settings: RuntimeSettings | None = None, engine: Any = None
) -> Any:
    """Build the FastAPI app wired to durable services.

    Startup order matters: configuration is validated first, then the schemas
    are ensured, then the services are assembled. Assembling first would open
    connections that validation was about to reject.
    """
    from erp_pipeline.api.main import create_app
    from erp_pipeline.orchestration import JobExecutor, OrchestrationService
    from erp_pipeline.orchestration.job_store import PostgresJobStore
    from erp_pipeline.runtime.database import build_pipeline_engine
    from erp_pipeline.runtime.services import build_production_services

    resolved = settings or RuntimeSettings.from_environment()
    resolved.require_valid()

    active_engine = engine or build_pipeline_engine(resolved.database)

    if resolved.bootstrap_on_startup:
        from erp_pipeline.runtime.bootstrap import bootstrap_all
        from erp_pipeline.runtime.persistence import bootstrap_runtime_persistence

        result = bootstrap_all(active_engine)
        bootstrap_runtime_persistence(active_engine)

        if result.created:
            LOGGER.info("created schemas: %s", ", ".join(result.created))

        if not result.ok:
            raise ConfigurationError(
                "schema bootstrap did not complete:\n" + result.render()
            )

    services = build_production_services(resolved, active_engine)

    orchestration = OrchestrationService(
        services=services,
        # Durable jobs - never InMemoryJobStore in production.
        job_store=PostgresJobStore(active_engine),
        executor=JobExecutor(max_workers=resolved.executor_workers),
    )

    app = create_app(settings=resolved.api, orchestration=orchestration)
    app.state.runtime_settings = resolved
    app.state.engine = active_engine

    LOGGER.info(
        "application assembled: jobs=PostgresJobStore records=Postgres "
        "tier_state=Postgres sources=Postgres uploads=Postgres "
        "secrets=EnvironmentSecretProvider"
    )

    return app


class _LazyApp:
    """An ASGI callable that builds the real app on first request.

    Uvicorn imports the module and then calls the app. Building eagerly at
    import would mean ``--help`` or a linting pass opened PostgreSQL, and it
    would break Phase 13's guarantee that importing the API is cheap.
    """

    def __init__(self) -> None:
        self._app: Any = None

    def _resolve(self) -> Any:
        if self._app is None:
            self._app = create_production_app()

        return self._app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
        return await self._resolve()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


#: The documented ASGI target.
app = _LazyApp()


def run() -> int:
    """Start the server. Backs ``python -m erp_pipeline.api``."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    settings = RuntimeSettings.from_environment()

    try:
        settings.require_valid()
    except ConfigurationError as error:
        # Printed rather than raised: an operator wants the list of problems,
        # not a traceback. No value from the environment is echoed.
        print(str(error))

        return 2

    built = create_production_app(settings)

    print(
        f"serving on http://{settings.api.host}:{settings.api.port}  "
        f"(docs: /docs, auth: "
        f"{'enabled' if settings.api.auth_enabled else 'disabled'})"
    )

    uvicorn.run(built, host=settings.api.host, port=settings.api.port)

    return 0


__all__ = ["app", "create_production_app", "run"]

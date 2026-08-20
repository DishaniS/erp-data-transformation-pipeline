"""Lightweight dependency health checks.

Each pipeline stage calls the checks it actually needs before doing real work,
so a missing service produces one clear sentence instead of a stack trace from
somewhere deep inside a driver.

Nothing here ever prints or returns a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bpi2020.common.config import EmbeddingSettings, PostgresSettings


class DependencyUnavailableError(RuntimeError):
    """Raised when a required external dependency cannot be used."""


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'OK  ' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def check_postgres(
    settings: PostgresSettings,
    required_tables: tuple[str, ...] = (),
    raise_on_failure: bool = True,
) -> CheckResult:
    """Verify a PostgreSQL database is reachable and has the expected tables."""
    name = f"PostgreSQL {settings.label} ({settings.safe_target})"

    try:
        engine = settings.create_engine(connect_args={"connect_timeout": 10})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

            if required_tables:
                rows = connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                        """
                    )
                ).fetchall()
                present = {row[0] for row in rows}
                missing = [table for table in required_tables if table not in present]

                if missing:
                    detail = (
                        f"connected, but missing tables: {', '.join(missing)}. "
                        "Run src/bpi2020/storage/create_ai_native_db_schema.py."
                    )
                    result = CheckResult(name, False, detail)
                    if raise_on_failure:
                        raise DependencyUnavailableError(f"{name}: {detail}")
                    return result

    except DependencyUnavailableError:
        raise
    except Exception as exc:
        detail = f"unreachable ({type(exc).__name__}: {_first_line(exc)})"
        result = CheckResult(name, False, detail)
        if raise_on_failure:
            raise DependencyUnavailableError(
                f"{name}: {detail}. Check that PostgreSQL is running and that the "
                "ERP_SOURCE_DB_* / PIPELINE_DB_* settings in .env are correct."
            ) from exc
        return result

    return CheckResult(name, True, "reachable")


def check_qdrant(
    qdrant_settings: Any,
    collection_name: str,
    expected_vector_size: int | None = None,
    raise_on_failure: bool = True,
) -> CheckResult:
    """Verify Qdrant is reachable and report the collection's real status.

    This never creates a collection and never reports availability it did not
    observe. Collection creation stays in the embedding stage, which is the
    only component that knows the vector size.
    """
    name = f"Qdrant ({qdrant_settings.target})"

    try:
        client = qdrant_settings.create_client()
        collections = [item.name for item in client.get_collections().collections]

        if collection_name not in collections:
            detail = (
                f"reachable; collection '{collection_name}' does not exist yet "
                "(the embedding stage will create it)"
            )
            return CheckResult(name, True, detail)

        info = client.get_collection(collection_name)
        vectors_config = info.config.params.vectors
        vector_size = getattr(vectors_config, "size", None)
        detail = (
            f"reachable; collection '{collection_name}' has "
            f"{info.points_count} points, vector size {vector_size}"
        )

        if expected_vector_size is not None and vector_size != expected_vector_size:
            detail = (
                f"collection '{collection_name}' has vector size {vector_size}, "
                f"but the configured embedding model produces {expected_vector_size}"
            )
            result = CheckResult(name, False, detail)
            if raise_on_failure:
                raise DependencyUnavailableError(f"{name}: {detail}")
            return result

        return CheckResult(name, True, detail)

    except DependencyUnavailableError:
        raise
    except Exception as exc:
        detail = f"unreachable ({type(exc).__name__}: {_first_line(exc)})"
        result = CheckResult(name, False, detail)
        if raise_on_failure:
            raise DependencyUnavailableError(
                f"{name}: {detail}. Check VECTOR_DB_URL and VECTOR_DB_API_KEY in .env, "
                "or start a local Qdrant server."
            ) from exc
        return result


def check_embedding_model(
    settings: EmbeddingSettings | None = None,
    load_model: bool = False,
    raise_on_failure: bool = True,
) -> CheckResult:
    """Verify sentence-transformers is installed and the model id is configured.

    ``load_model=False`` only checks configuration and imports, because loading
    the model downloads weights and is slow. The embedding stage loads it for
    real straight afterwards.
    """
    settings = settings or EmbeddingSettings.from_env()
    name = f"Embedding model ({settings.model_id})"

    try:
        import sentence_transformers  # noqa: F401

        if settings.batch_size <= 0:
            raise ValueError("EMBEDDING_BATCH_SIZE must be greater than zero")

        if load_model:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(settings.model_id)
            size = model.get_sentence_embedding_dimension()
            return CheckResult(name, True, f"loaded, vector size {size}")

        return CheckResult(
            name,
            True,
            f"sentence-transformers available, batch size {settings.batch_size}",
        )

    except Exception as exc:
        detail = f"unavailable ({type(exc).__name__}: {_first_line(exc)})"
        result = CheckResult(name, False, detail)
        if raise_on_failure:
            raise DependencyUnavailableError(
                f"{name}: {detail}. Install requirements.txt and check EMBEDDING_MODEL_ID."
            ) from exc
        return result


def check_tesseract(raise_on_failure: bool = True) -> CheckResult:
    """Verify the Tesseract OCR binary is callable. Only images need this."""
    from bpi2020.common.config import get_tesseract_cmd

    name = "Tesseract OCR"

    try:
        import pytesseract

        configured = get_tesseract_cmd()
        if configured:
            pytesseract.pytesseract.tesseract_cmd = configured

        version = pytesseract.get_tesseract_version()
        return CheckResult(name, True, f"version {version}")

    except Exception as exc:
        detail = f"unavailable ({type(exc).__name__}: {_first_line(exc)})"
        result = CheckResult(name, False, detail)
        if raise_on_failure:
            raise DependencyUnavailableError(
                f"{name}: {detail}. Install Tesseract and set TESSERACT_CMD in .env."
            ) from exc
        return result


def engine_for(settings: PostgresSettings, verify: bool = True, **kwargs) -> Engine:
    """Create an engine after confirming the database is reachable."""
    if verify:
        check_postgres(settings)
    return settings.create_engine(**kwargs)


def _first_line(exc: Exception) -> str:
    message = str(exc).strip().splitlines()
    return message[0][:200] if message else exc.__class__.__name__

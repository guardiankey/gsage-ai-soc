"""gSage AI — DataStore service layer.

All business logic for GSageDataStore and GSageDataStoreRecord.
Functions receive an AsyncSession and operate within the caller's transaction.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from jsonschema import Draft7Validator, SchemaError, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DataStoreError(Exception):
    """Base for DataStore service errors (maps to HTTP 400/404/409/422)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class DataStoreNotFound(DataStoreError):
    def __init__(self, detail: str = "DataStore not found") -> None:
        super().__init__(detail, status_code=404)


class DataStoreRecordNotFound(DataStoreError):
    def __init__(self, detail: str = "DataStore record not found") -> None:
        super().__init__(detail, status_code=404)


class DataStoreLimitExceeded(DataStoreError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=409)


class DataStoreValidationError(DataStoreError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=422)


class DataStoreAccessDenied(DataStoreError):
    def __init__(self, detail: str = "Access denied") -> None:
        super().__init__(detail, status_code=403)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_json_schema_definition(schema: dict) -> None:
    """Raise DataStoreValidationError if *schema* is not a valid JSON Schema."""
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise DataStoreValidationError(f"Invalid JSON Schema: {exc.message}") from exc


def _validate_record_data(data: dict, schema: dict) -> None:
    """Raise DataStoreValidationError if *data* fails *schema* validation.

    Skips validation when *schema* is empty ({}), meaning no schema is enforced.
    """
    if not schema:
        return
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        messages = "; ".join(e.message for e in errors[:5])
        raise DataStoreValidationError(f"Record validation failed: {messages}")


def _check_visibility(store: Any, user_id: uuid.UUID) -> None:
    """Raise DataStoreAccessDenied if *store* is private and user is not its owner."""
    if store.visibility == "private" and store.created_by != user_id:
        raise DataStoreAccessDenied("This store is private and belongs to another user.")


def _check_record_limit(store: Any) -> None:
    """Raise DataStoreLimitExceeded if the store is already at max capacity."""
    if store.record_count >= store.max_records:
        raise DataStoreLimitExceeded(
            f"Store '{store.name}' has reached its record limit ({store.max_records})."
        )


def _check_record_size(data: dict, max_bytes: int) -> None:
    """Raise DataStoreLimitExceeded if *data* serialised size exceeds *max_bytes*."""
    size = len(json.dumps(data, separators=(",", ":")).encode())
    if size > max_bytes:
        raise DataStoreLimitExceeded(
            f"Record size {size} bytes exceeds the limit of {max_bytes} bytes."
        )


# ---------------------------------------------------------------------------
# Store operations
# ---------------------------------------------------------------------------


async def list_stores(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dept_id: uuid.UUID,
    include_private: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Any], int]:
    """Return paginated stores visible to *user_id* within *dept_id*.

    Visibility rules:
    - ``shared`` stores are always visible.
    - ``private`` stores are visible only to their creator, unless
      *include_private* is True (admin override).
    """
    from sqlalchemy import and_, or_

    from src.shared.models.datastore import GSageDataStore

    visibility_filter = (
        GSageDataStore.org_id == org_id,
        GSageDataStore.dept_id == dept_id,
        GSageDataStore.is_active.is_(True),
    )
    if not include_private:
        visibility_filter = (
            *visibility_filter,
            or_(
                GSageDataStore.visibility == "shared",
                GSageDataStore.created_by == user_id,
            ),
        )

    count_stmt = select(func.count()).select_from(
        select(GSageDataStore).where(and_(*visibility_filter)).subquery()
    )
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    stmt = (
        select(GSageDataStore)
        .where(and_(*visibility_filter))
        .order_by(GSageDataStore.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    stores = list(result.scalars().all())
    return stores, total


async def get_store(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    store_id: uuid.UUID,
    dept_id: uuid.UUID | None = None,
) -> Any:
    """Return a single store or raise DataStoreNotFound / DataStoreAccessDenied."""
    from src.shared.models.datastore import GSageDataStore

    filters = [
        GSageDataStore.id == store_id,
        GSageDataStore.org_id == org_id,
        GSageDataStore.is_active.is_(True),
    ]
    if dept_id is not None:
        filters.append(GSageDataStore.dept_id == dept_id)

    result = await session.execute(select(GSageDataStore).where(*filters))
    store = result.scalar_one_or_none()
    if store is None:
        raise DataStoreNotFound()
    _check_visibility(store, user_id)
    return store


async def get_store_by_name(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    dept_id: uuid.UUID | None = None,
) -> Any:
    """Return a single active store by name or raise DataStoreNotFound."""
    from src.shared.models.datastore import GSageDataStore

    filters = [
        GSageDataStore.org_id == org_id,
        GSageDataStore.name == name,
        GSageDataStore.is_active.is_(True),
    ]
    if dept_id is not None:
        filters.append(GSageDataStore.dept_id == dept_id)

    result = await session.execute(select(GSageDataStore).where(*filters))
    store = result.scalar_one_or_none()
    if store is None:
        raise DataStoreNotFound(f"Store '{name}' not found.")
    _check_visibility(store, user_id)
    return store


async def create_store(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    dept_id: uuid.UUID,
    name: str,
    description: Optional[str] = None,
    schema: Optional[dict] = None,
    visibility: str = "shared",
    max_records: int = 500,
) -> Any:
    """Create a new DataStore for *dept_id* within *org_id*.

    Validates the JSON Schema definition and checks the per-org store limit.
    """
    from src.shared.config.settings import get_settings
    from src.shared.models.datastore import GSageDataStore

    settings = get_settings()
    effective_schema = schema or {}

    if effective_schema:
        _validate_json_schema_definition(effective_schema)

    # Check store count limit (per org)
    count_result = await session.execute(
        select(func.count()).where(
            GSageDataStore.org_id == org_id,
            GSageDataStore.is_active.is_(True),
        )
    )
    current_count = count_result.scalar_one()
    if current_count >= settings.datastore_max_stores_per_org:
        raise DataStoreLimitExceeded(
            f"Organization has reached the maximum of "
            f"{settings.datastore_max_stores_per_org} active stores."
        )

    # Check name uniqueness within the department
    existing = await session.execute(
        select(GSageDataStore).where(
            GSageDataStore.dept_id == dept_id,
            GSageDataStore.name == name,
            GSageDataStore.is_active.is_(True),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise DataStoreError(f"A store named '{name}' already exists in this department.", status_code=409)

    effective_max = min(max_records, settings.datastore_max_records_per_store)

    store = GSageDataStore(
        org_id=org_id,
        dept_id=dept_id,
        created_by=user_id,
        name=name,
        description=description,
        schema=effective_schema,
        visibility=visibility,
        max_records=effective_max,
        record_count=0,
        is_active=True,
    )
    session.add(store)
    await session.commit()
    await session.refresh(store)
    return store


async def update_store(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    store_id: uuid.UUID,
    dept_id: uuid.UUID | None = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    schema: Optional[dict] = None,
    visibility: Optional[str] = None,
    max_records: Optional[int] = None,
    is_active: Optional[bool] = None,
) -> Any:
    """Partial update of a DataStore. Only provided fields are changed."""
    from src.shared.config.settings import get_settings
    from src.shared.models.datastore import GSageDataStore

    settings = get_settings()

    result = await session.execute(
        select(GSageDataStore).where(
            GSageDataStore.id == store_id,
            GSageDataStore.org_id == org_id,
            *([GSageDataStore.dept_id == dept_id] if dept_id is not None else []),
        )
    )
    store = result.scalar_one_or_none()
    if store is None:
        raise DataStoreNotFound()
    _check_visibility(store, user_id)

    if name is not None:
        store.name = name
    if description is not None:
        store.description = description
    if schema is not None:
        if schema:
            _validate_json_schema_definition(schema)
        store.schema = schema
    if visibility is not None:
        store.visibility = visibility
    if max_records is not None:
        store.max_records = min(max_records, settings.datastore_max_records_per_store)
    if is_active is not None:
        store.is_active = is_active

    await session.commit()
    await session.refresh(store)
    return store


async def delete_store(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    store_id: uuid.UUID,
    dept_id: uuid.UUID | None = None,
) -> None:
    """Hard-delete a store and all its records (via CASCADE)."""
    from src.shared.models.datastore import GSageDataStore

    filters = [
        GSageDataStore.id == store_id,
        GSageDataStore.org_id == org_id,
    ]
    if dept_id is not None:
        filters.append(GSageDataStore.dept_id == dept_id)

    result = await session.execute(select(GSageDataStore).where(*filters))
    store = result.scalar_one_or_none()
    if store is None:
        raise DataStoreNotFound()
    _check_visibility(store, user_id)

    await session.delete(store)
    await session.commit()


# ---------------------------------------------------------------------------
# Record operations
# ---------------------------------------------------------------------------


async def list_records(
    session: AsyncSession,
    store_id: uuid.UUID,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Any], int]:
    """Return paginated records for *store_id*."""
    from src.shared.models.datastore import GSageDataStoreRecord

    count_stmt = select(func.count()).where(
        GSageDataStoreRecord.datastore_id == store_id
    )
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    stmt = (
        select(GSageDataStoreRecord)
        .where(GSageDataStoreRecord.datastore_id == store_id)
        .order_by(GSageDataStoreRecord.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    records = list(result.scalars().all())
    return records, total


async def get_record(
    session: AsyncSession,
    store_id: uuid.UUID,
    record_id: uuid.UUID,
) -> Any:
    """Return a single record or raise DataStoreRecordNotFound."""
    from src.shared.models.datastore import GSageDataStoreRecord

    result = await session.execute(
        select(GSageDataStoreRecord).where(
            GSageDataStoreRecord.id == record_id,
            GSageDataStoreRecord.datastore_id == store_id,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise DataStoreRecordNotFound()
    return record


async def insert_record(
    session: AsyncSession,
    store: Any,
    data: dict,
) -> Any:
    """Insert a single validated record into *store*."""
    from src.shared.config.settings import get_settings
    from src.shared.models.datastore import GSageDataStoreRecord

    settings = get_settings()
    _check_record_limit(store)
    _check_record_size(data, settings.datastore_max_record_size_bytes)
    _validate_record_data(data, store.schema)

    record = GSageDataStoreRecord(datastore_id=store.id, data=data)
    session.add(record)
    store.record_count = (store.record_count or 0) + 1
    await session.commit()
    await session.refresh(record)
    return record


async def bulk_insert_records(
    session: AsyncSession,
    store: Any,
    records: list[dict],
) -> list[Any]:
    """Insert multiple records atomically. Validates all before inserting any."""
    from src.shared.config.settings import get_settings
    from src.shared.models.datastore import GSageDataStoreRecord

    settings = get_settings()

    if not records:
        return []

    # Pre-flight checks
    current_count = store.record_count or 0
    if current_count + len(records) > store.max_records:
        raise DataStoreLimitExceeded(
            f"Bulk insert of {len(records)} records would exceed the store limit "
            f"of {store.max_records} (currently {current_count})."
        )

    for i, data in enumerate(records):
        _check_record_size(data, settings.datastore_max_record_size_bytes)
        try:
            _validate_record_data(data, store.schema)
        except DataStoreValidationError as exc:
            raise DataStoreValidationError(f"Record[{i}]: {exc}") from exc

    # All good — persist
    new_records: list[GSageDataStoreRecord] = []
    for data in records:
        rec = GSageDataStoreRecord(datastore_id=store.id, data=data)
        session.add(rec)
        new_records.append(rec)

    store.record_count = current_count + len(records)
    await session.commit()
    for rec in new_records:
        await session.refresh(rec)
    return new_records


async def update_record(
    session: AsyncSession,
    store: Any,
    record_id: uuid.UUID,
    data: dict,
) -> Any:
    """Replace the payload of an existing record after schema validation."""
    from src.shared.config.settings import get_settings

    settings = get_settings()
    _check_record_size(data, settings.datastore_max_record_size_bytes)
    _validate_record_data(data, store.schema)

    record = await get_record(session, store.id, record_id)
    record.data = data
    await session.commit()
    await session.refresh(record)
    return record


async def delete_record(
    session: AsyncSession,
    store_id: uuid.UUID,
    record_id: uuid.UUID,
) -> None:
    """Hard-delete a single record and decrement the store counter."""
    from src.shared.models.datastore import GSageDataStore, GSageDataStoreRecord

    record = await get_record(session, store_id, record_id)
    await session.delete(record)

    # Decrement counter
    store_result = await session.execute(
        select(GSageDataStore).where(GSageDataStore.id == store_id)
    )
    store = store_result.scalar_one_or_none()
    if store is not None and store.record_count > 0:
        store.record_count -= 1

    await session.commit()


async def query_records(
    session: AsyncSession,
    store_id: uuid.UUID,
    filters: Optional[dict] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Any], int]:
    """Query records using JSONB containment (@>).

    *filters* is a dict that the ``data`` column must contain.
    Omit or pass ``None``/``{}`` to return all records (same as list_records).
    """
    from src.shared.models.datastore import GSageDataStoreRecord

    base_where = GSageDataStoreRecord.datastore_id == store_id

    if filters:
        base_where = base_where & GSageDataStoreRecord.data.contains(filters)  # type: ignore[operator]

    count_stmt = select(func.count()).where(base_where)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    stmt = (
        select(GSageDataStoreRecord)
        .where(base_where)
        .order_by(GSageDataStoreRecord.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    records = list(result.scalars().all())
    return records, total

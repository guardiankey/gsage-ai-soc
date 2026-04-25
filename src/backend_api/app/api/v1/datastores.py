"""gSage AI — DataStore REST endpoints.

Routes
------
Store endpoints (prefix: /v1/orgs/{org_id}/datastores):
    GET    /                              List stores (paginated, visibility-filtered)
    POST   /                              Create store
    GET    /{store_id}                    Get store detail
    PATCH  /{store_id}                    Update store
    DELETE /{store_id}                    Delete store + all its records

Record endpoints (prefix: /v1/orgs/{org_id}/datastores/{store_id}/records):
    GET    /                              List records (paginated)
    POST   /                              Insert one or more records
    POST   /query                         Query records with JSONB filters
    GET    /{record_id}                   Get single record
    PATCH  /{record_id}                   Update record
    DELETE /{record_id}                   Delete record
"""

from __future__ import annotations

import uuid
from typing import Annotated, NoReturn, Union

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_department_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.datastore import (
    DataStoreCreate,
    DataStoreOut,
    DataStoreRecordBulkCreate,
    DataStoreRecordCreate,
    DataStoreRecordOut,
    DataStoreRecordQueryParams,
    DataStoreRecordUpdate,
    DataStoreUpdate,
)
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams
from src.shared.database import get_db
from src.shared.services import datastore_service
from src.shared.services.datastore_service import (
    DataStoreAccessDenied,
    DataStoreError,
    DataStoreLimitExceeded,
    DataStoreNotFound,
    DataStoreRecordNotFound,
    DataStoreValidationError,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raise_http(exc: DataStoreError) -> NoReturn:
    """Convert a service-layer DataStoreError to an appropriate HTTPException."""
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


async def _get_store_or_raise(
    store_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    dept_id: uuid.UUID | None = None,
):
    try:
        return await datastore_service.get_store(db, org_id, user_id, store_id, dept_id=dept_id)
    except DataStoreError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# Store routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[DataStoreOut],
    summary="List data stores for the org",
)
async def list_stores(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
) -> PaginatedResponse[DataStoreOut]:
    ctx.require_permission("datastores:read")

    dept = ctx.dept_id
    assert dept is not None, "dept_id required — guaranteed by get_department_context"

    # Admins/owners see private stores too
    include_private = ctx.has_permission("datastores:write")

    stores, total = await datastore_service.list_stores(
        session=db,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        dept_id=dept,
        include_private=include_private,
        page=pagination.page,
        page_size=pagination.limit,
    )
    return PaginatedResponse.build(
        [DataStoreOut.model_validate(s) for s in stores],
        total=total,
        pagination=pagination,
    )


@router.post(
    "",
    response_model=DataStoreOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new data store",
)
async def create_store(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    body: DataStoreCreate,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DataStoreOut:
    ctx.require_permission("datastores:write")

    dept = ctx.dept_id
    assert dept is not None, "dept_id required — guaranteed by get_department_context"

    try:
        store = await datastore_service.create_store(
            session=db,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            dept_id=dept,
            name=body.name,
            description=body.description,
            schema=body.json_schema,
            visibility=body.visibility,
            max_records=body.max_records or 500,
        )
    except DataStoreError as exc:
        _raise_http(exc)

    return DataStoreOut.model_validate(store)


@router.get(
    "/{store_id}",
    response_model=DataStoreOut,
    summary="Get a single data store",
)
async def get_store(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DataStoreOut:
    ctx.require_permission("datastores:read")

    try:
        store = await datastore_service.get_store(db, ctx.org_id, ctx.user_id, store_id, dept_id=ctx.dept_id)
    except DataStoreError as exc:
        _raise_http(exc)

    return DataStoreOut.model_validate(store)


@router.patch(
    "/{store_id}",
    response_model=DataStoreOut,
    summary="Update a data store",
)
async def update_store(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    body: DataStoreUpdate,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DataStoreOut:
    ctx.require_permission("datastores:write")

    try:
        store = await datastore_service.update_store(
            session=db,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            store_id=store_id,
            dept_id=ctx.dept_id,
            name=body.name,
            description=body.description,
            schema=body.json_schema,
            visibility=body.visibility,
            max_records=body.max_records,
            is_active=body.is_active,
        )
    except DataStoreError as exc:
        _raise_http(exc)

    return DataStoreOut.model_validate(store)


@router.delete(
    "/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a data store and all its records",
)
async def delete_store(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ctx.require_permission("datastores:write")

    try:
        await datastore_service.delete_store(db, ctx.org_id, ctx.user_id, store_id, dept_id=ctx.dept_id)
    except DataStoreError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# Record routes
# ---------------------------------------------------------------------------


@router.get(
    "/{store_id}/records",
    response_model=PaginatedResponse[DataStoreRecordOut],
    summary="List records in a data store",
)
async def list_records(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
) -> PaginatedResponse[DataStoreRecordOut]:
    ctx.require_permission("datastores:read")

    await _get_store_or_raise(store_id, ctx.org_id, ctx.user_id, db, dept_id=ctx.dept_id)

    records, total = await datastore_service.list_records(
        session=db,
        store_id=store_id,
        page=pagination.page,
        page_size=pagination.limit,
    )
    return PaginatedResponse.build(
        [DataStoreRecordOut.model_validate(r) for r in records],
        total=total,
        pagination=pagination,
    )


@router.post(
    "/{store_id}/records",
    response_model=Union[DataStoreRecordOut, list[DataStoreRecordOut]],
    status_code=status.HTTP_201_CREATED,
    summary="Insert one or multiple records into a store",
)
async def insert_records(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    body: Union[DataStoreRecordCreate, DataStoreRecordBulkCreate],
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Union[DataStoreRecordOut, list[DataStoreRecordOut]]:
    ctx.require_permission("datastores:write")

    try:
        store = await datastore_service.get_store(db, ctx.org_id, ctx.user_id, store_id, dept_id=ctx.dept_id)
    except DataStoreError as exc:
        _raise_http(exc)

    try:
        if isinstance(body, DataStoreRecordBulkCreate):
            inserted = await datastore_service.bulk_insert_records(db, store, body.records)
            return [DataStoreRecordOut.model_validate(r) for r in inserted]
        else:
            record = await datastore_service.insert_record(db, store, body.data)
            return DataStoreRecordOut.model_validate(record)
    except DataStoreError as exc:
        _raise_http(exc)


@router.post(
    "/{store_id}/records/query",
    response_model=PaginatedResponse[DataStoreRecordOut],
    summary="Query records using JSONB containment filters",
)
async def query_records(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    body: DataStoreRecordQueryParams,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PaginatedResponse[DataStoreRecordOut]:
    ctx.require_permission("datastores:read")

    await _get_store_or_raise(store_id, ctx.org_id, ctx.user_id, db, dept_id=ctx.dept_id)

    records, total = await datastore_service.query_records(
        session=db,
        store_id=store_id,
        filters=body.filters,
        page=body.page,
        page_size=body.page_size,
    )

    # Build a minimal PaginatedResponse without PaginationParams dependency
    from src.backend_api.app.schemas.pagination import PaginatedResponse as PR

    return PR(
        items=[DataStoreRecordOut.model_validate(r) for r in records],
        total=total,
        page=body.page,
        limit=body.page_size,
        has_more=(body.page * body.page_size < total),
    )


@router.get(
    "/{store_id}/records/{record_id}",
    response_model=DataStoreRecordOut,
    summary="Get a single record",
)
async def get_record(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    record_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DataStoreRecordOut:
    ctx.require_permission("datastores:read")

    await _get_store_or_raise(store_id, ctx.org_id, ctx.user_id, db, dept_id=ctx.dept_id)

    try:
        record = await datastore_service.get_record(db, store_id, record_id)
    except DataStoreError as exc:
        _raise_http(exc)

    return DataStoreRecordOut.model_validate(record)


@router.patch(
    "/{store_id}/records/{record_id}",
    response_model=DataStoreRecordOut,
    summary="Update a record",
)
async def update_record(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    record_id: uuid.UUID,
    body: DataStoreRecordUpdate,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DataStoreRecordOut:
    ctx.require_permission("datastores:write")

    try:
        store = await datastore_service.get_store(db, ctx.org_id, ctx.user_id, store_id, dept_id=ctx.dept_id)
        record = await datastore_service.update_record(db, store, record_id, body.data)
    except DataStoreError as exc:
        _raise_http(exc)

    return DataStoreRecordOut.model_validate(record)


@router.delete(
    "/{store_id}/records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a single record",
)
async def delete_record(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    store_id: uuid.UUID,
    record_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ctx.require_permission("datastores:write")

    await _get_store_or_raise(store_id, ctx.org_id, ctx.user_id, db, dept_id=ctx.dept_id)

    try:
        await datastore_service.delete_record(db, store_id, record_id)
    except DataStoreError as exc:
        _raise_http(exc)

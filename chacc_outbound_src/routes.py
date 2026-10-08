from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, List, Dict, Any, Generic, TypeVar
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from .context_factory import get_current_user, get_db, get_outbound_service
from .models import Outbound, OutboundModuleMapping
from .service import OutboundService
from .exceptions import AdapterNotFoundError


router = APIRouter()
security = HTTPBearer()

T = TypeVar("T")


class SendOutboundRequest(BaseModel):
    module_name: str = Field(..., description="Module name sending the notification")
    recipient_id: str = Field(..., description="User/entity identifier")
    recipient_contact: str = Field(..., description="Email address or phone number")
    subject: Optional[str] = Field(default=None, description="Message subject (required for email, ignored for SMS)")
    body: str = Field(..., description="Message body content")
    channel: str = Field(default="email", description="Channel to use")
    content_type: str = Field(default="text/plain", description="Content type: text/plain for SMS/text, html for HTML email")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Module-specific tracking data")

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        if v not in ("text/plain", "html"):
            raise ValueError('content_type must be "text/plain" or "html"')
        return v


class OutboundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uuid: str
    module_name: str
    recipient_id: str
    channel: str
    subject: Optional[str]
    body: str
    recipient_contact: str
    outbound_metadata: Optional[dict]
    status: str
    sent_at: Optional[str]
    attempts: int
    last_error: Optional[str]


class ModuleMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    module_name: str
    default_adapter_name: Optional[str]
    default_channel: Optional[str]
    max_retry_attempts: Optional[int]
    retry_backoff_seconds: Optional[int]
    rate_limit_per_minute: Optional[int]
    is_active: Optional[bool]
    description: Optional[str]


class CreateModuleMappingRequest(BaseModel):
    module_name: str = Field(..., description="Module name this mapping applies to")
    default_adapter_name: Optional[str] = Field(default=None, description="Default adapter for this module")
    default_channel: Optional[str] = Field(default=None, description="Default channel for this module")
    max_retry_attempts: Optional[int] = Field(default=None, ge=1, description="Max retry attempts")
    retry_backoff_seconds: Optional[int] = Field(default=None, ge=0, description="Initial backoff seconds between retries")
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1, description="Max sends per minute")
    is_active: Optional[bool] = Field(default=None, description="Whether this module is active")
    description: Optional[str] = Field(default=None, description="Human-readable description")


class Pager(BaseModel):
    page: int
    size: int
    pages: int


class BaseResponseModel(BaseModel, Generic[T]):
    success: bool = True
    message: str = ""
    data: List[T]
    total: Optional[int] = None
    pager: Optional[Pager] = None

    class Config:
        from_attributes = True


class PaginationParams(BaseModel):
    paging: bool = Field(True, description="Whether to use pagination")
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    size: int = Field(10, ge=1, le=1000, description="Page size")


def _serialize_outbound(n) -> OutboundResponse:
    return OutboundResponse(
        uuid=str(n.uuid),
        module_name=n.module_name,
        recipient_id=n.recipient_id,
        channel=n.channel,
        subject=n.subject,
        body=n.body,
        recipient_contact=n.recipient_contact,
        outbound_metadata=n.messaging_metadata,
        status=n.status.value,
        sent_at=n.sent_at.isoformat() if n.sent_at else None,
        attempts=n.attempts,
        last_error=n.last_error,
    )


@router.post("/send", response_model=OutboundResponse)
async def send_outbound(
    payload: SendOutboundRequest,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    try:
        return await service.send(
            db=db,
            recipient_id=payload.recipient_id,
            recipient_contact=payload.recipient_contact,
            body=payload.body,
            module_name=payload.module_name,
            subject=payload.subject,
            channel=payload.channel,
            adapter_name=payload.adapter_name,
            metadata=payload.metadata,
            content_type=payload.content_type,
        )
    except AdapterNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages", response_model=BaseResponseModel[OutboundResponse])
async def list_outbounds(
    params: PaginationParams = Depends(),
    module_name: Optional[str] = Query(None, description="Filter by module name"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    status: Optional[str] = Query(None, description="Filter by status"),
    search: str = Query("", description="Search by uuid, module_name, recipient_contact, subject, or body"),
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    stmt = select(Outbound)
    if module_name:
        stmt = stmt.where(Outbound.module_name == module_name)
    if channel:
        stmt = stmt.where(Outbound.channel == channel)
    if status:
        stmt = stmt.where(Outbound.status == status)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(
                Outbound.uuid.ilike(like),
                Outbound.module_name.ilike(like),
                Outbound.recipient_contact.ilike(like),
                Outbound.subject.ilike(like),
                Outbound.body.ilike(like),
            )
        )
    stmt = stmt.order_by(Outbound.created_at.desc())

    if not params.paging:
        result = db.execute(stmt)
        items = result.scalars().all()
        return BaseResponseModel(
            success=True,
            message="Data fetched successfully",
            data=[_serialize_outbound(n) for n in items],
            total=None,
            pager=None,
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    paginated = db.execute(stmt.offset((params.page - 1) * params.size).limit(params.size))
    items = paginated.scalars().all()
    pages = (total + params.size - 1) // params.size if params.size else 1

    return BaseResponseModel(
        success=True,
        message="Data fetched successfully",
        data=[_serialize_outbound(n) for n in items],
        total=total,
        pager=Pager(page=params.page, size=params.size, pages=pages),
    )


@router.get("/messages/{outbound_uuid}", response_model=OutboundResponse)
async def get_outbound(
    outbound_uuid: str,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    outbound = await service.get_message(db, outbound_uuid)
    if not outbound:
        raise HTTPException(status_code=404, detail="Message not found")
    return _serialize_outbound(outbound)


@router.get("/messages/{outbound_uuid}/status")
async def get_outbound_message_status(
    outbound_uuid: str,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    status = service.get_status(db, outbound_uuid)
    if status is None:
        raise HTTPException(status_code=404, detail="Outbound Message not found")
    return {"uuid": outbound_uuid, "status": status}


@router.get("/module-mappings", response_model=BaseResponseModel[ModuleMappingResponse])
async def list_module_mappings(
    params: PaginationParams = Depends(),
    module_name: Optional[str] = Query(None, description="Filter by module name"),
    current_user: dict = Depends(get_current_user),
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
):
    stmt = select(OutboundModuleMapping)
    if module_name:
        stmt = stmt.where(OutboundModuleMapping.module_name == module_name)
    stmt = stmt.order_by(OutboundModuleMapping.module_name)

    if not params.paging:
        result = db.execute(stmt)
        items = result.scalars().all()
        return BaseResponseModel(
            success=True,
            message="Data fetched successfully",
            data=items,
            total=None,
            pager=None,
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    paginated = db.execute(stmt.offset((params.page - 1) * params.size).limit(params.size))
    items = paginated.scalars().all()
    pages = (total + params.size - 1) // params.size if params.size else 1

    return BaseResponseModel(
        success=True,
        message="Data fetched successfully",
        data=items,
        total=total,
        pager=Pager(page=params.page, size=params.size, pages=pages),
    )


@router.get("/adapters")
async def list_adapters(
    service: OutboundService = Depends(get_outbound_service),
    current_user: dict = Depends(get_current_user),
):
    adapters = service.adapter_registry_service.list_adapters()
    return {"success": True, "data": adapters}


@router.get("/module-mappings/{module_name}", response_model=ModuleMappingResponse)
async def get_module_mapping(
    module_name: str,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    mapping = service.get_module_mapping(db, module_name)
    if not mapping:
        raise HTTPException(status_code=404, detail="Module mapping not found")
    return mapping


@router.post("/module-mappings", response_model=ModuleMappingResponse)
async def create_module_mapping(
    payload: CreateModuleMappingRequest,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    mapping = service._create_or_update_module_mapping(
        db=db,
        module_name=payload.module_name,
        default_adapter_name=payload.default_adapter_name,
        default_channel=payload.default_channel,
        max_retry_attempts=payload.max_retry_attempts,
        retry_backoff_seconds=payload.retry_backoff_seconds,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        is_active=payload.is_active,
        description=payload.description,
    )
    db.commit()
    db.refresh(mapping)
    return mapping


@router.put("/module-mappings/{module_name}", response_model=ModuleMappingResponse)
async def update_module_mapping(
    module_name: str,
    payload: CreateModuleMappingRequest,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    existing = service.get_module_mapping(db, module_name)
    if not existing:
        raise HTTPException(status_code=404, detail="Module mapping not found")

    mapping = service._create_or_update_module_mapping(
        db=db,
        module_name=module_name,
        default_adapter_name=payload.default_adapter_name,
        default_channel=payload.default_channel,
        max_retry_attempts=payload.max_retry_attempts,
        retry_backoff_seconds=payload.retry_backoff_seconds,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        is_active=payload.is_active,
        description=payload.description,
    )
    db.commit()
    db.refresh(mapping)
    return mapping


@router.delete("/module-mappings/{module_name}")
async def delete_module_mapping(
    module_name: str,
    service: OutboundService = Depends(get_outbound_service),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    mapping = service.get_module_mapping(db, module_name)
    if not mapping:
        raise HTTPException(status_code=404, detail="Module mapping not found")
    db.delete(mapping)
    db.commit()
    return {"success": True, "message": f"Module mapping '{module_name}' deleted"}

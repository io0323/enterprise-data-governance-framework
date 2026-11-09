"""§16 イベント配信 API: Webhook の配信先とデッドレター."""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from edgf import authz
from edgf.auth import Principal
from edgf.db import get_session
from edgf.deps import get_principal
from edgf.models import WebhookDelivery, WebhookEndpoint
from edgf.pagination import Page, page_params, paginate
from edgf.schemas import DeliveryOut, WebhookIn, WebhookOut, WebhookUpdate
from edgf.services import audit, webhooks
from edgf.services.webhooks import WebhookConfigError

router = APIRouter(tags=["webhooks"])


def _validate(url: str | None, secret_ref: str | None) -> None:
    try:
        if url is not None:
            webhooks.check_destination(url)
        if secret_ref is not None:
            webhooks.check_secret(secret_ref)
    except WebhookConfigError as e:
        raise HTTPException(422, str(e)) from e


def _get(db: Session, endpoint_id: str) -> WebhookEndpoint:
    endpoint = db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(404, f"webhook not found: {endpoint_id}")
    return endpoint


@router.post("/webhooks", response_model=WebhookOut, status_code=201)
def create_webhook(body: WebhookIn, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "webhook.manage", entity_type="webhook", entity_id="new")
    _validate(body.url, body.secret_ref)
    endpoint = WebhookEndpoint(url=body.url, secret_ref=body.secret_ref, event_types=body.event_types, description=body.description,
                               created_by=principal.subject)
    db.add(endpoint)
    db.flush()
    audit.record(db, principal.audit_actor, "webhook.create", "webhook", endpoint.id,
                 {"secret_ref": endpoint.secret_ref, "event_types": endpoint.event_types, "url_host": _host(endpoint.url)})
    return endpoint


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


@router.get("/webhooks", response_model=list[WebhookOut])
def list_webhooks(request: Request, response: Response, page: Page = Depends(page_params()), db: Session = Depends(get_session),
                  principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "webhook.manage", entity_type="webhook", entity_id="*")
    return paginate(db, select(WebhookEndpoint).order_by(WebhookEndpoint.created_at, WebhookEndpoint.id), page, request, response)


@router.get("/webhooks/{endpoint_id}", response_model=WebhookOut)
def get_webhook(endpoint_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "webhook.manage", entity_type="webhook", entity_id=endpoint_id)
    return _get(db, endpoint_id)


@router.patch("/webhooks/{endpoint_id}", response_model=WebhookOut)
def update_webhook(endpoint_id: str, body: WebhookUpdate, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """無効にすると新しいイベントを割り当てず、未配信のものも送らない (有効に戻すと再開する)。"""
    authz.require(db, principal, "webhook.manage", entity_type="webhook", entity_id=endpoint_id)
    endpoint = _get(db, endpoint_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    _validate(changes.get("url"), changes.get("secret_ref"))
    for key, value in changes.items():
        setattr(endpoint, key, value)
    db.flush()
    audit.record(db, principal.audit_actor, "webhook.update", "webhook", endpoint.id, {"fields": sorted(changes), "active": endpoint.active})
    return endpoint


@router.get("/outbox/dead-letters", response_model=list[DeliveryOut])
def list_dead_letters(request: Request, response: Response, page: Page = Depends(page_params()), db: Session = Depends(get_session),
                      principal: Principal = Depends(get_principal)):
    authz.require(db, principal, "outbox.redeliver", entity_type="webhook_delivery", entity_id="*")
    stmt = select(WebhookDelivery).where(WebhookDelivery.status == webhooks.DEAD).order_by(WebhookDelivery.dead_at, WebhookDelivery.id)
    return paginate(db, stmt, page, request, response)


@router.post("/outbox/dead-letters/{delivery_id}/redeliver", response_model=DeliveryOut)
def redeliver(delivery_id: str, db: Session = Depends(get_session), principal: Principal = Depends(get_principal)):
    """再送の予約だけを行い、実際の送信は配信ジョブが行う (同じイベント ID で届く)。"""
    authz.require(db, principal, "outbox.redeliver", entity_type="webhook_delivery", entity_id=delivery_id)
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(404, f"delivery not found: {delivery_id}")
    try:
        webhooks.redeliver(db, delivery)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    audit.record(db, principal.audit_actor, "outbox.redeliver", "webhook_delivery", delivery.id, {"event_id": delivery.event_id})
    return delivery

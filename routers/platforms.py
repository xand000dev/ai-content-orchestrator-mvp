import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Platform, new_id

router = APIRouter(prefix="/platforms", tags=["platforms"])

# Ключі що НЕ є секретними — повертаємо відкрито для зручності редагування
_PUBLIC_KEYS = {"channel_id", "chat_id", "channel_username", "username", "page_id"}


class PlatformCreate(BaseModel):
    name: str
    type: str
    credentials: dict = {}
    settings: dict = {}
    auto_publish: bool = False


class PlatformUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    credentials: Optional[dict] = None
    settings: Optional[dict] = None
    auto_publish: Optional[bool] = None
    active: Optional[bool] = None


def _serialize(p: Platform) -> dict:
    creds = json.loads(p.credentials or "{}")
    # channel_id та подібні — відкрито; токени/ключі — маскуємо
    visible = {k: (v if k in _PUBLIC_KEYS else "***") for k, v in creds.items()}
    return {
        "id": p.id,
        "name": p.name,
        "type": p.type,
        "credentials": visible,           # channel_id видно, bot_token = ***
        "credentials_keys": list(creds.keys()),
        "settings": json.loads(p.settings or "{}"),
        "auto_publish": p.auto_publish,
        "active": p.active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("")
def list_platforms(db: Session = Depends(get_db)):
    return [_serialize(p) for p in db.query(Platform).order_by(Platform.created_at).all()]


@router.post("", status_code=201)
def create_platform(body: PlatformCreate, db: Session = Depends(get_db)):
    p = Platform(
        id=new_id(), name=body.name, type=body.type,
        credentials=json.dumps(body.credentials),
        settings=json.dumps(body.settings),
        auto_publish=body.auto_publish,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.get("/{platform_id}")
def get_platform(platform_id: str, db: Session = Depends(get_db)):
    p = db.query(Platform).filter(Platform.id == platform_id).first()
    if not p:
        raise HTTPException(404, "Platform not found")
    return _serialize(p)


@router.put("/{platform_id}")
def update_platform(platform_id: str, body: PlatformUpdate, db: Session = Depends(get_db)):
    p = db.query(Platform).filter(Platform.id == platform_id).first()
    if not p:
        raise HTTPException(404, "Platform not found")
    if body.name is not None:        p.name = body.name
    if body.type is not None:        p.type = body.type
    if body.settings is not None:    p.settings = json.dumps(body.settings)
    if body.auto_publish is not None: p.auto_publish = body.auto_publish
    if body.active is not None:      p.active = body.active
    if body.credentials is not None:
        # Merge: якщо поле = "***" → залишаємо старе значення (не затираємо токен)
        old_creds = json.loads(p.credentials or "{}")
        merged = {k: (old_creds.get(k, v) if v == "***" else v)
                  for k, v in body.credentials.items()}
        # Додаємо нові ключі яких не було
        for k, v in body.credentials.items():
            if v != "***":
                merged[k] = v
        p.credentials = json.dumps(merged)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.delete("/{platform_id}", status_code=204)
def delete_platform(platform_id: str, db: Session = Depends(get_db)):
    p = db.query(Platform).filter(Platform.id == platform_id).first()
    if not p:
        raise HTTPException(404, "Platform not found")
    db.delete(p)
    db.commit()

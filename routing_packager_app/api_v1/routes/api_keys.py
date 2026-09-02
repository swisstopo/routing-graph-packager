from datetime import datetime, timedelta
import secrets
from typing import List

from fastapi import Depends, HTTPException, APIRouter, Query
from fastapi.security import HTTPBasicCredentials
from sqlmodel import Session, select
from starlette.responses import Response
from starlette.status import (
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
)

from ..models import APIKeys, APIKeysUpdate, APIKeysRead, APIKeysCreate, APIPermission, User
from ...utils.db_utils import delete_or_abort, add_or_abort
from ...db import get_db
from ..auth import BasicAuth, hmac_hash

router = APIRouter()


@router.get("/", response_model=List[APIKeysRead])
def get_keys(
    permission: APIPermission | None = Query(None),
    is_active: bool | None = Query(None),
    is_valid: bool | None = Query(None),
    comment: str | None = Query(None),
    db: Session = Depends(get_db),
    auth: HTTPBasicCredentials = Depends(BasicAuth),
):
    """GET all API Keys. Optionally filter by permission, active or valid status."""
    req_user = User.get_user(db, auth)
    if not req_user:
        raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authorized to read keys.")

    keys = select(APIKeys)

    if permission is not None:
        keys = keys.where(APIKeys.permission == permission)
    if is_active is not None:
        keys = keys.where(APIKeys.is_active == is_active)
    if comment is not None:
        keys = keys.where(APIKeys.comment.startswith(comment))
    if is_valid is not None:
        now = datetime.now()
        if is_valid:
            keys = keys.where(APIKeys.valid_until > now)
        else:
            keys = keys.where(APIKeys.valid_until <= now)

    return db.exec(keys).all()


@router.post("/", response_model=APIKeysRead)
def post_key(
    key: APIKeysCreate, db: Session = Depends(get_db), auth: HTTPBasicCredentials = Depends(BasicAuth)
):
    """POST a new key. Needs admin privileges"""
    if not User.get_user(db, auth):
        raise HTTPException(HTTP_401_UNAUTHORIZED, "Wrong username or password.")

    generated_key = secrets.token_urlsafe(16)
    hashed_key = hmac_hash(generated_key)
    key_db = APIKeys.model_validate(
        key,
        update={
            "key": hashed_key,
            "is_active": True,
            "valid_until": datetime.now() + timedelta(days=key.validity_days),
        },
    )

    add_or_abort(db, key_db)

    # store the hashed key but return the generated_key to the user
    response_key = key_db.model_dump(mode="json")
    response_key.update({"key": generated_key})
    print(f"key now: {response_key['key']}")
    return response_key


@router.get("/{key_id}", response_model=APIKeysRead)
def get_key(
    key_id,
    db: Session = Depends(get_db),
    auth: HTTPBasicCredentials = Depends(BasicAuth),
):
    """Get API Key with specified ID"""
    user = User.get_user(db, auth)
    if not user:
        raise HTTPException(HTTP_401_UNAUTHORIZED, "Wrong username or password.")

    key = db.get(APIKeys, key_id)

    if not key:
        raise HTTPException(HTTP_404_NOT_FOUND, f"Couldn't find key with id {key_id}")

    return key


@router.patch("/{key_id}", response_model=APIKeysRead)
def patch_key(
    key_id,
    key_update: APIKeysUpdate,
    db: Session = Depends(get_db),
    auth: HTTPBasicCredentials = Depends(BasicAuth),
):
    """Get API Key with specified ID"""
    user = User.get_user(db, auth)
    if not user:
        raise HTTPException(HTTP_401_UNAUTHORIZED, "Wrong username or password.")

    key = db.get(APIKeys, key_id)

    if not key:
        raise HTTPException(HTTP_404_NOT_FOUND, f"Couldn't find key with id {key_id}")

    key_data = key_update.model_dump(exclude_unset=True)
    for k, val in key_data.items():
        if k == "validity_days":
            key.valid_until = datetime.now() + timedelta(days=val)
        else:
            setattr(key, k, val)

    db.add(key)
    db.commit()
    db.refresh(key)

    return key


@router.delete("/{key_id}")
def delete_key(key_id, db: Session = Depends(get_db), auth: HTTPBasicCredentials = Depends(BasicAuth)):
    # first authenticate
    req_user = User.get_user(db, auth)
    if not req_user:
        raise HTTPException(HTTP_401_UNAUTHORIZED, "Not authorized to delete a key.")

    key: APIKeys | None = db.get(APIKeys, key_id)
    if not key:
        raise HTTPException(HTTP_404_NOT_FOUND, f"Couldn't find key id {key_id}")

    delete_or_abort(db, key)

    return Response("", HTTP_204_NO_CONTENT)

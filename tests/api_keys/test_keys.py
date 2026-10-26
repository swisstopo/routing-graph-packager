from datetime import datetime, timedelta, timezone
import pytest
from base64 import b64encode
from sqlalchemy_utils.functions.database import itertools
from sqlmodel import Session, select

from routing_packager_app.api_v1.auth import hmac_hash
from routing_packager_app.api_v1.models import APIKeys, APIKeysRead
from routing_packager_app.config import SETTINGS
from ..utils_ import create_new_key

permissions = ("read", "write")
validity_values = (1, 12, 90, 365, 500, 1000)


@pytest.mark.parametrize("permission, validity_days", itertools.product(permissions, validity_values))
def test_post_key(
    get_client, basic_auth_header, get_session: Session, permission: str, validity_days: int
):
    res = create_new_key(
        get_client,
        auth_header=basic_auth_header,
        data={"permission": permission, "validity_days": validity_days},
    )

    assert len(res.json()["key"]) == 22
    statement = select(APIKeys).where(APIKeys.id == res.json()["id"])
    key_db: APIKeys | None = get_session.exec(statement).first()

    assert key_db is not None
    assert key_db.is_active

    tolerance = timedelta(seconds=10)
    assert abs(key_db.valid_until - (datetime.now(timezone.utc) + timedelta(days=validity_days))) <= tolerance

    assert key_db.permission == permission
    assert len(key_db.key) == 64


def test_post_key_unauthenticated(get_client):
    data = {"permission": "read", "validity_days": 90}
    response = get_client.post("/api/v1/keys/", headers={}, json=data)
    assert response.status_code == 401


def test_post_key_wrong_password(get_client):
    data = {"permission": "read", "validity_days": 90}

    admin_email = SETTINGS.ADMIN_EMAIL
    admin_pass = "wrong_pass"

    auth_encoded = b64encode(bytes(":".join((admin_email, admin_pass)).encode("utf-8"))).decode()
    auth_header = {"Authorization": f"Basic {auth_encoded}"}
    response = get_client.post("/api/v1/keys/", headers=auth_header, json=data)
    assert response.status_code == 401


def test_read_and_filter_keys(get_client, basic_auth_header, get_session: Session):
    for i in range(10):
        data = {
            "permission": "write" if i < 3 else "read",
            "validity_days": 1,
        }

        if i == 8:
            data.update({"comment": "test"})
        res = create_new_key(
            get_client,
            auth_header=basic_auth_header,
            data=data,
        )

    # get all
    res = get_client.get("/api/v1/keys/", headers=basic_auth_header)
    assert res.status_code == 200
    assert len(res.json()) == 10

    # get read
    res = get_client.get("/api/v1/keys/?permission=read", headers=basic_auth_header)
    assert res.status_code == 200
    assert len(res.json()) == 7

    # get write
    res = get_client.get("/api/v1/keys/?permission=write", headers=basic_auth_header)
    assert res.status_code == 200
    assert len(res.json()) == 3

    # get comment
    res = get_client.get("/api/v1/keys/?comment=tes", headers=basic_auth_header)
    assert res.status_code == 200
    assert len(res.json()) == 1

    # get write and comment (should be empty)
    res = get_client.get("/api/v1/keys/?comment=tes&permission=write", headers=basic_auth_header)
    assert res.status_code == 200
    assert len(res.json()) == 0


def test_patch_key(get_client, basic_auth_header, get_session: Session):
    data = {
        "permission": "write",
        "validity_days": 1,
    }

    res = create_new_key(
        get_client,
        auth_header=basic_auth_header,
        data=data,
    )

    response_key = APIKeysRead.model_validate(res.json())
    assert response_key.permission == "write"
    tolerance = timedelta(seconds=10)
    assert abs(response_key.valid_until - (datetime.now(timezone.utc) + timedelta(days=1))) <= tolerance
    assert response_key.comment == ""

    # update it
    # first add a comment
    data = {
        "comment": "test",
    }
    res = get_client.patch(f"/api/v1/keys/{response_key.id}", headers=basic_auth_header, json=data)
    assert res.status_code == 200
    new_response_key = APIKeysRead.model_validate(res.json())
    assert new_response_key.id == response_key.id
    assert new_response_key.permission == "write"
    tolerance = timedelta(seconds=20)
    assert abs(new_response_key.valid_until - (datetime.now(timezone.utc)+ timedelta(days=1))) <= tolerance
    assert new_response_key.comment == "test"

    # then a new permission and a different validity time frame
    data = {"permission": "read", "validity_days": 15}
    res = get_client.patch(f"/api/v1/keys/{response_key.id}", headers=basic_auth_header, json=data)
    assert res.status_code == 200
    new_response_key = APIKeysRead.model_validate(res.json())
    assert new_response_key.id == response_key.id
    assert new_response_key.permission == "read"
    tolerance = timedelta(seconds=20)
    assert abs(new_response_key.valid_until - (datetime.now(timezone.utc)+ timedelta(days=15))) <= tolerance
    assert new_response_key.comment == "test"
    assert new_response_key.is_active

    # finally revoke it
    data = {"is_active": False}
    res = get_client.patch(f"/api/v1/keys/{response_key.id}", headers=basic_auth_header, json=data)
    assert res.status_code == 200
    new_response_key = APIKeysRead.model_validate(res.json())
    assert new_response_key.id == response_key.id
    assert not new_response_key.is_active


@pytest.mark.parametrize("_", range(10))
def test_match_key(get_client, basic_auth_header, get_session: Session, _):
    """
    Check some generated API Keys against their hashes
    """
    data = {
        "permission": "write",
        "validity_days": 1,
    }
    res = create_new_key(
        get_client,
        auth_header=basic_auth_header,
        data=data,
    )

    statement = select(APIKeys).where(APIKeys.id == res.json()["id"])
    key_db: APIKeys | None = get_session.exec(statement).first()
    assert key_db is not None
    assert hmac_hash(res.json()["key"]) == key_db.key

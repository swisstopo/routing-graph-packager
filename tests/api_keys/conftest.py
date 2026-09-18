
import pytest
from sqlmodel import Session, select

from routing_packager_app.api_v1.models import APIKeys


@pytest.fixture(scope="function", autouse=True)
def delete_keys(get_session: Session):
    yield
    keys = get_session.exec(select(APIKeys)).all()
    for key in keys:
        get_session.delete(key)
        get_session.commit()

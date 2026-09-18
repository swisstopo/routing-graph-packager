import pytest
from sqlmodel import Session, select

from routing_packager_app.api_v1.models import User


@pytest.fixture(scope="function", autouse=True)
def delete_users(get_session: Session):
    yield
    users = get_session.exec(select(User).filter(User.id != 1)).all()
    for user in users:
        get_session.delete(user)
        get_session.commit()

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from fastapi.security import HTTPBasicCredentials
from geoalchemy2 import Geography
from pydantic import EmailStr
from sqlalchemy import Column
from sqlalchemy_utils import PasswordType
from sqlmodel import AutoString, DateTime, Field, Relationship, Session, SQLModel, and_, select, or_

from routing_packager_app.api_v1.auth import hmac_hash

from ..config import SETTINGS
from ..constants import Providers, Statuses
from ..utils.geom_utils import wkbe_to_str


class APIPermission(str, Enum):
    READ = "read"
    READWRITE = "write"
    INTERNAL = "internal"


class APIKeysBase(SQLModel):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    is_active: bool = Field(nullable=False, default=False)
    comment: str = Field(nullable=False, default="")


class APIKeysUpdate(SQLModel):
    is_active: bool | None = None
    comment: str | None = None
    validity_days: int | None = None
    permission: APIPermission | None = None


class APIKeysCreate(APIKeysBase):
    permission: APIPermission
    validity_days: int


class APIKeysRead(APIKeysBase):
    key: str
    permission: APIPermission
    valid_until: datetime


class APIKeys(APIKeysBase, table=True):
    __tablename__ = "api_keys"  # type: ignore

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(nullable=False)
    permission: APIPermission = Field(default=APIPermission.READ)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    valid_until: datetime = Field(nullable=True, default=datetime(1970, 1, 1, tzinfo=timezone.utc))
    is_active: bool = Field(nullable=False, default=False)

    @staticmethod
    def check_key(db: Session, key: str, min_permission: APIPermission) -> bool:
        if not key:
            return False
        hashed_key = hmac_hash(key)
        permission_clause = min_permission == APIPermission.READ  

        if min_permission == APIPermission.READWRITE: 
            permission_clause = or_(APIKeys.permission == "write", APIKeys.permission == "internal")
        if min_permission == APIPermission.INTERNAL: 
            permission_clause = APIKeys.permission == "internal"

        key_candidate: APIKeys | None = db.exec(
            select(APIKeys).where(
                and_(
                    APIKeys.is_active,
                    APIKeys.key == hashed_key,
                    permission_clause,
                    APIKeys.valid_until > datetime.now(),
                )
            )
        ).first()

        if not key_candidate:
            return False

        return True

    def __repr__(self):  # pragma: no cover
        s = f"<APIKey id={self.id} key={self.key} permission={self.permission}"
        f"created_at={self.created_at} valid_until={self.valid_until} is_active={self.is_active}"
        f"comment={self.comment}>"
        return s


class JobBase(SQLModel):
    name: str = Field(nullable=False, default="test")
    provider: Providers = Field(nullable=False, default=Providers.OSM)
    # WKBElement isn't a valid pydantic type
    bbox: str = Field(
        sa_column=Column(Geography("POLYGON", srid=4326, spatial_index=True, nullable=False)),
        default="5.9559,45.818,10.4921,47.8084",
    )
    description: str = Field(nullable=True, default="")
    update: bool = Field(nullable=False, default=False)


class JobRead(JobBase):
    id: int = 1
    user_id: int | None = 1
    arq_id: str = ""
    status: Statuses = Statuses.QUEUED
    bbox: str = ""
    zip_path: Optional[str]
    last_started: Optional[datetime]
    last_finished: Optional[datetime]


class JobCreate(JobBase):
    pass


class Job(JobBase, table=True):
    __tablename__ = "jobs"  # type: ignore

    id: int | None = Field(default=None, primary_key=True)
    arq_id: str | None = Field(nullable=True)
    user_id: int | None = Field(default=None, foreign_key="users.id")
    status: Statuses = Field(nullable=False)
    zip_path: str = Field(nullable=True)
    last_started: datetime | None = Field(nullable=True)  # did it ever run?
    last_finished: datetime | None = Field(
        sa_column=Column(DateTime(), nullable=True)
    )  # did it ever finish?

    user: "User" = Relationship(back_populates="jobs")

    def __repr__(self):  # pragma: no cover
        s = f"<Job id={self.id} name={self.name} status={self.status} provider={self.provider}>"
        return s

    def convert_bbox(self):
        """Converts a WKBElement to a bbox string"""
        self.bbox = wkbe_to_str(self.bbox)  # type: ignore


class UserBase(SQLModel):
    email: EmailStr = Field(index=True, unique=True, nullable=False, sa_type=AutoString)


class UserRead(UserBase):
    id: int


class UserCreate(UserBase):
    password: str


class User(UserBase, table=True):
    """The users table."""

    __tablename__ = "users"  # type: ignore

    id: int | None = Field(default=None, primary_key=True)
    password: str = Field(sa_column=Column(PasswordType(schemes=("pbkdf2_sha512",)), nullable=False))
    jobs: List[Job] = Relationship(back_populates="user")

    def __repr__(self):  # pragma: no cover
        return "<User {}>".format(self.email)

    @staticmethod
    def get_user(db: Session, auth_data: HTTPBasicCredentials) -> Optional["User"]:
        if not auth_data or not auth_data.username or not auth_data.password:
            return None
        user = db.exec(select(User).filter(User.email == auth_data.username)).first()  # type: ignore
        if not user:
            return None
        if not user.password == auth_data.password:
            return None

        return user

    @staticmethod
    def add_admin_user(session: Session):
        """Add admin user before first request."""
        admin_email = SETTINGS.ADMIN_EMAIL
        admin_pass = SETTINGS.ADMIN_PASS

        if not session.exec(select(User).filter_by(email=admin_email)).first():
            admin_user = User(email=admin_email, password=admin_pass)
            session.add(admin_user)
            session.commit()


class LogType(str, Enum):
    WORKER = "worker"
    APP = "app"
    BUILDER = "builder"

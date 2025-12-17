from fastapi import APIRouter

from .routes import jobs, logs, users, api_keys

api_v1_router = APIRouter()
api_v1_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
api_v1_router.include_router(users.router, prefix="/users", tags=["users"])
api_v1_router.include_router(logs.router, prefix="/logs", tags=["logs"])
api_v1_router.include_router(api_keys.router, prefix="/keys", tags=["api_keys"])
api_v1_router.include_router(health.router, prefix="/health", tags=["health"])

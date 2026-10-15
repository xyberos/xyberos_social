"""Aggregate router: the thin HTTP surface that assembles every route module."""

from fastapi import APIRouter

from app.api.routes import (
    account,
    chat,
    communities,
    content,
    health,
    identity,
    moderation,
    notifications,
    pages,
    people,
)

api_router = APIRouter()

for route_module in (
    health,
    pages,
    identity,
    people,
    chat,
    communities,
    content,
    moderation,
    notifications,
    account,
):
    api_router.include_router(route_module.router)

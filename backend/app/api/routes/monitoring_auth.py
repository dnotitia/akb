from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import StrictBool

from app.api.routes.admin_auth import ProductAdminActor, get_product_admin_mutation
from app.config import settings
from app.exceptions import AuthenticationError
from app.services.monitoring_auth import (
    MonitoringSession,
    end_monitoring_session,
    login_monitoring_local,
    require_monitoring_session,
    set_monitoring_access,
)
from app.util.text import NFCModel


MONITORING_PAGE = "/admin/mcp-monitoring"
MONITORING_API = "/admin/mcp-monitoring"
_COOKIE_PATH = "/api/v1" + MONITORING_API

router = APIRouter(prefix=MONITORING_API)


def _cookie() -> tuple[str, bool]:
    secure = urlsplit(settings.public_base_url).scheme == "https"
    return ("__Secure-akb_monitor_session" if secure else "akb_dev_monitor_session", secure)


def set_monitoring_cookie(response: Response, issued: MonitoringSession) -> None:
    name, secure = _cookie()
    response.set_cookie(
        name, issued.token, expires=issued.expires_at, path=_COOKIE_PATH,
        secure=secure, httponly=True, samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


async def get_monitoring_reader(request: Request) -> None:
    authorization = request.headers.get("authorization")
    if authorization is None:
        token = request.cookies.get(_cookie()[0], "")
    else:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise AuthenticationError()
    await require_monitoring_session(token)


class LocalMonitoringLogin(NFCModel):
    username: str
    password: str


@router.post("/auth/local/login", status_code=204, summary="Start a local monitoring session")
async def local_monitoring_login(credentials: LocalMonitoringLogin):
    if settings.require_auth_mode() != "local":
        raise HTTPException(404, "Local monitoring sign-in is disabled")
    issued = await login_monitoring_local(credentials.username, credentials.password)
    response = Response(status_code=204)
    set_monitoring_cookie(response, issued)
    return response


@router.post("/auth/logout", status_code=204, summary="End a browser monitoring session")
async def monitoring_logout(request: Request):
    if request.headers.get("X-AKB-Monitoring-Logout") != "1":
        raise AuthenticationError()
    name, secure = _cookie()
    await end_monitoring_session(request.cookies.get(name, ""))
    response = Response(status_code=204)
    response.delete_cookie(name, path=_COOKIE_PATH, secure=secure, httponly=True, samesite="lax")
    return response


class MonitoringAccessRequest(NFCModel):
    enabled: StrictBool


@router.put("/access/{user_id}", status_code=204, summary="Grant or revoke a dedicated monitoring account")
async def set_monitoring_access_route(
    user_id: UUID, body: MonitoringAccessRequest, admin: ProductAdminActor = Depends(get_product_admin_mutation),
):
    await set_monitoring_access(user_id, body.enabled, actor_id=str(admin.user_id))
    return Response(status_code=204)

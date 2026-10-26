"""账号 / 会话状态 API — persistent Playwright profile."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from app.collectors.goofish_profile import (
    acquire_profile_lock,
    ensure_user_data_dir,
    probe_session,
    session_status_summary,
    user_data_dir,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# In-process login job state (MVP — single operator)
_login_job: dict[str, Any] = {
    "status": "idle",  # idle | running | success | failed | unavailable
    "message": "",
}


class SessionStatusOut(BaseModel):
    state: str
    logged_in: Optional[bool] = None
    nickname: Optional[str] = None
    message: str
    user_data_dir: str
    profile_has_content: bool = False
    profile_mtime: Optional[str] = None
    last_collect_success_at: Optional[str] = None
    collector_backend: str = "stub"
    headless: bool = True
    fixture_path: Optional[str] = None
    probe: Optional[dict[str, Any]] = None
    login_script: str = "python -m app.scripts.login_goofish"
    login_job: dict[str, Any] = Field(default_factory=dict)


class OpenLoginOut(BaseModel):
    ok: bool
    mode: str
    message: str
    user_data_dir: str
    login_script: str = "python -m app.scripts.login_goofish"
    job: dict[str, Any] = Field(default_factory=dict)


def _display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def _run_headed_login(timeout_s: float = 180.0) -> None:
    global _login_job
    _login_job = {"status": "running", "message": "有头浏览器已启动，请在窗口中登录…"}
    try:
        from app.scripts.login_goofish import run_login
        from app.collectors.goofish_profile import GOOFISH_HOME

        # Serialize with collector against the same persistent profile.
        with acquire_profile_lock(timeout_s=120.0):
            code = await run_login(timeout_s=timeout_s, start_url=GOOFISH_HOME)
        if code == 0:
            _login_job = {"status": "success", "message": "登录成功，会话已写入配置目录"}
        else:
            _login_job = {
                "status": "failed",
                "message": "登录超时或失败；请在本机有显示环境运行登录脚本",
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("[accounts] open-login failed")
        _login_job = {"status": "failed", "message": str(exc)[:300]}


@router.get("/goofish/status", response_model=SessionStatusOut)
async def goofish_session_status(probe: bool = False) -> SessionStatusOut:
    """Session status: optional short Playwright probe + profile mtime / last success."""
    ensure_user_data_dir()
    probe_result = None
    if probe and not os.getenv("GOOFISH_FIXTURE_PATH", "").strip():
        try:
            probe_result = await asyncio.wait_for(probe_session(), timeout=45)
        except Exception as exc:  # noqa: BLE001
            probe_result = {
                "logged_in": None,
                "method": "probe_error",
                "error": str(exc)[:200],
                "message": f"probe failed: {exc}"[:200],
            }
    summary = session_status_summary(probe=probe_result)
    return SessionStatusOut(
        **summary,
        login_script="python -m app.scripts.login_goofish",
        login_job=dict(_login_job),
    )


@router.post("/goofish/open-login", response_model=OpenLoginOut)
async def goofish_open_login(background_tasks: BackgroundTasks) -> OpenLoginOut:
    """
    Start headed login into persistent profile when DISPLAY is available.
    Otherwise return pause-friendly instructions to run the local script.
    """
    udd = str(ensure_user_data_dir())
    script = "python -m app.scripts.login_goofish"

    if os.getenv("GOOFISH_FIXTURE_PATH", "").strip():
        return OpenLoginOut(
            ok=False,
            mode="fixture",
            message="当前为 fixture 模式；请 unset GOOFISH_FIXTURE_PATH 后再登录。",
            user_data_dir=udd,
            login_script=script,
            job=dict(_login_job),
        )

    if not _display_available():
        return OpenLoginOut(
            ok=False,
            mode="instructions",
            message=(
                "当前环境无图形显示（无 DISPLAY）。请在有桌面的机器上执行："
                f" `{script}` ，登录成功后把 `{udd}` 同步到本服务，"
                "或在本机用 VNC/有头一次登录。"
            ),
            user_data_dir=udd,
            login_script=script,
            job=dict(_login_job),
        )

    if _login_job.get("status") == "running":
        return OpenLoginOut(
            ok=True,
            mode="headed_running",
            message="登录浏览器已在运行，请查看弹出窗口。",
            user_data_dir=udd,
            login_script=script,
            job=dict(_login_job),
        )

    background_tasks.add_task(_run_headed_login)
    return OpenLoginOut(
        ok=True,
        mode="headed_started",
        message="已在后台启动有头浏览器，请在窗口中完成登录。",
        user_data_dir=udd,
        login_script=script,
        job={"status": "running", "message": "starting"},
    )

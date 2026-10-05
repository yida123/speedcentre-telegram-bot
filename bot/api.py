"""SpeedCentre+ 用户 API 的异步客户端。"""
from typing import Any

import httpx


class APIError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data

    def __str__(self) -> str:
        msg = self.args[0]
        if self.code == 50001 and isinstance(self.data, dict):
            return (
                f"积分不足：本次需要 {self.data.get('required_credit')}，"
                f"剩余 {self.data.get('remaining_credit')}"
            )
        return msg


_STATUS_HINTS = {
    401: "API Key 无效",
    403: "无权限（API 未启用、套餐不支持或超出配额）",
    404: "未找到",
    422: "任务未完成或数据缺失",
    429: "请求过于频繁，请稍后再试",
}


class SCPClient:
    def __init__(self, api_key: str, base_url: str = "https://api.speedcentre.plus", timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Api-Key": api_key, "User-Agent": "speed-bot/1.0"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise APIError(f"请求 API 失败：{e}") from e
        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            pass
        if isinstance(body, dict) and "code" in body:
            if body.get("code") != 0 or resp.status_code >= 400:
                raise APIError(
                    body.get("message") or _STATUS_HINTS.get(resp.status_code, "未知错误"),
                    code=body.get("code"),
                    status=resp.status_code,
                    data=body.get("data"),
                )
            return body.get("data")
        if resp.status_code >= 400:
            raise APIError(
                f"HTTP {resp.status_code}：{_STATUS_HINTS.get(resp.status_code, resp.text[:200])}",
                status=resp.status_code,
            )
        return body

    async def list_backends(self) -> list[dict]:
        return await self._request("GET", "/api/v1/backends") or []

    async def list_scripts(self) -> list[dict]:
        return await self._request("GET", "/api/v1/scripts") or []

    async def create_share(self, task_id: str, title: str, hide_private_info: bool = True) -> dict:
        return await self._request("POST", f"/api/v1/tasks/{task_id}/share",
                                   json={"title": title[:128], "hide_private_info": hide_private_info})

    async def list_tasks(self, page: int = 1, page_size: int = 10, status: str | None = None) -> dict:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        return await self._request("GET", "/api/v1/tasks", params=params) or {}

    async def submit_task(
        self,
        name: str,
        nodes: list[dict],
        matrices: list[dict],
        configs: dict | None = None,
        slave_id: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": name[:128], "nodes": nodes, "matrices": matrices}
        if configs:
            payload["configs"] = configs
        if slave_id:
            payload["slave_id"] = slave_id
        return await self._request("POST", "/api/v1/tasks", json=payload)

    async def get_task(self, task_id: str) -> dict:
        return await self._request("GET", f"/api/v1/tasks/{task_id}")

    async def get_progress(self, task_id: str) -> dict:
        return await self._request("GET", f"/api/v1/tasks/{task_id}/progress")

    async def get_result(self, task_id: str) -> dict:
        return await self._request("GET", f"/api/v1/tasks/{task_id}/result")

    async def cancel_task(self, task_id: str) -> None:
        await self._request("DELETE", f"/api/v1/tasks/{task_id}")

    async def export_image(self, task_id: str, view: str = "normalview", sort: str | None = None) -> bytes:
        params = {"sort": sort} if sort and view == "normalview" else None
        try:
            resp = await self._client.get(
                f"/api/v1/tasks/{task_id}/export/{view}", params=params, timeout=120
            )
        except httpx.HTTPError as e:
            raise APIError(f"导出图片失败：{e}") from e
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            msg = _STATUS_HINTS.get(resp.status_code, f"HTTP {resp.status_code}")
            try:
                msg = resp.json().get("message") or msg
            except ValueError:
                pass
            raise APIError(f"导出图片失败：{msg}", status=resp.status_code)
        return resp.content

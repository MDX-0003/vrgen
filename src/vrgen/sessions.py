"""项目/剧集/生成会话树（只读，供 web 的会话选择器使用）。

mglc 的平台层级是 企业 → 项目 → 剧集 → 生成会话。这里只做三件事：
逐层调用 mglc 的只读命令、把结果整理成契约里的嵌套 JSON、保持空值可读。
不写台账、不提交任务、不消耗额度。契约见 docs/case-model.md §4。
"""

from __future__ import annotations

from datetime import UTC, datetime

from .errors import UsageError
from .platform.client import Envelope, Mglc


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _items(envelope: Envelope) -> list[dict]:
    data = envelope.data or {}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or []
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def _int(item: dict, *keys: str) -> int:
    for key in keys:
        value = item.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _name(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _sessions(client: Mglc, project_id: int, episode: int) -> list[dict]:
    out: list[dict] = []
    for item in _items(client.session_list(project_id, episode)):
        out.append({"id": _int(item, "sessionId", "id"),
                    "name": _name(item, "sessionName", "name")})
    return out


def tree(client: Mglc, *, project_id: int | None = None,
         episode: int | None = None) -> dict:
    """构造项目/剧集/会话树。

    ``episode`` 必须与 ``project_id`` 一起给出；只给剧集无法确定它属于哪个项目。
    指定项目或剧集时仍然逐层查询，只是过滤结果，保证名称与层级来自平台本身。
    """
    if episode is not None and project_id is None:
        raise UsageError("--episode 必须和 --project-id 一起给")

    projects: list[dict] = []
    for project in _items(client.project_list()):
        pid = _int(project, "projectId", "id")
        if project_id is not None and pid != project_id:
            continue
        episodes: list[dict] = []
        for ep in _items(client.episode_list(pid)):
            no = _int(ep, "episodeNo", "episode")
            if episode is not None and no != episode:
                continue
            episodes.append({
                "no": no,
                "id": _int(ep, "episodeId", "id"),
                "name": _name(ep, "episodeName", "name"),
                "sessions": _sessions(client, pid, no),
            })
        episodes.sort(key=lambda e: e["no"])
        projects.append({"id": pid, "name": _name(project, "name"),
                         "episodes": episodes})
    return {"fetched_at": _now_utc(), "projects": projects}

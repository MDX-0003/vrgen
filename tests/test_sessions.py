"""项目/剧集/生成会话树（只读）的单元测试。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from vrgen import cli as vrgen_cli
from vrgen import sessions
from vrgen.errors import UsageError
from vrgen.platform.client import Envelope

runner = CliRunner()


def _env(data) -> Envelope:
    return Envelope(("mglc",), 0, data, "OK", "")


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def project_list(self):
        self.calls.append(("project",))
        return _env({"items": [
            {"projectId": 11, "name": "项目甲"},
            {"projectId": 12, "name": "项目乙"},
        ]})

    def episode_list(self, project_id):
        self.calls.append(("episode", project_id))
        if project_id == 11:
            return _env([
                {"episodeNo": 2, "episodeId": 112, "episodeName": "第二集"},
                {"episodeNo": 1, "episodeId": 111, "episodeName": "第一集"},
            ])
        return _env({"items": []})

    def session_list(self, project_id, episode):
        self.calls.append(("session", project_id, episode))
        if (project_id, episode) == (11, 1):
            return _env({"items": [{"sessionId": 1111, "sessionName": "会话一"}]})
        return _env({"items": []})


def test_tree_expands_projects_episodes_and_sessions():
    client = _FakeClient()
    payload = sessions.tree(client)
    assert payload["projects"][0]["name"] == "项目甲"
    assert [e["no"] for e in payload["projects"][0]["episodes"]] == [1, 2]
    assert payload["projects"][0]["episodes"][0]["sessions"] == [
        {"id": 1111, "name": "会话一"}]
    assert payload["projects"][1]["episodes"] == []
    assert ("session", 11, 1) in client.calls
    assert ("session", 12, 1) not in client.calls


def test_tree_filters_to_one_episode():
    client = _FakeClient()
    payload = sessions.tree(client, project_id=11, episode=1)
    assert len(payload["projects"]) == 1
    assert [e["no"] for e in payload["projects"][0]["episodes"]] == [1]
    assert ("session", 11, 2) not in client.calls


def test_tree_requires_project_for_episode():
    try:
        sessions.tree(_FakeClient(), episode=1)
    except UsageError as exc:
        assert "project-id" in str(exc)
    else:
        raise AssertionError("只给 --episode 必须报参数错误")


def test_cli_sessions_list_emits_json(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(vrgen_cli, "Mglc",
                        lambda **kwargs: fake)
    r = runner.invoke(vrgen_cli.app, ["sessions", "list"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["projects"][0]["episodes"][0]["sessions"][0]["id"] == 1111
    assert payload["fetched_at"].endswith("Z")

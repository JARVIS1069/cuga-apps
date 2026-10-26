"""Offline tests for mcp-workplace: Slack and Box are faked with httpx.MockTransport.

Run from cuga-apps/:   python -m pytest mcp_servers/workplace/test_server.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_servers.workplace import server  # noqa: E402

CHANNELS = [{"id": "C0CBE6CS0TE", "name": "automation-strategy-test"}, {"id": "C0OTHER", "name": "c"}]
USERS = {"vp@example.com": "U0VP", "report@example.com": "U0REP"}
HISTORY: list = []  # newest first, like Slack
REPLIES: list = []  # a thread: the parent first, then replies


def _fake(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        path = req.url.path
        if path == "/api/conversations.list":
            return httpx.Response(200, json={"ok": True, "channels": CHANNELS})
        if path == "/api/users.lookupByEmail":
            uid = USERS.get(req.url.params["email"])
            return httpx.Response(200, json={"ok": True, "user": {"id": uid}} if uid else {"ok": False, "error": "users_not_found"})
        if path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": "U0BOT", "bot_id": "B0BOT"})
        if path == "/api/conversations.history":
            return httpx.Response(200, json={"ok": True, "messages": HISTORY})
        if path == "/api/conversations.replies":
            return httpx.Response(200, json={"ok": True, "messages": REPLIES})
        if path == "/api/conversations.open":
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D0DM"}})
        if path == "/api/chat.postMessage":
            return httpx.Response(200, json={"ok": True, "ts": "1.2"})
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "box-tok", "expires_in": 3600})
        if path == "/2.0/files/77":
            assert req.headers["authorization"] == "Bearer box-tok"
            return httpx.Response(200, json={
                "id": "77", "name": "highlights.txt", "size": 25,
                "created_at": "2026-10-22T10:00:00-07:00", "modified_at": "2026-10-22T10:00:00-07:00",
                "created_by": {"name": "Anupama Murthi", "login": "a@example.com"},
                "modified_by": {"name": "Anupama Murthi", "login": "a@example.com"},
                "parent": {"name": "SIL Highlights (test)"},
            })
        if path == "/2.0/files/404":
            return httpx.Response(404, json={})
        return httpx.Response(500, json={"unexpected": path})
    return handler


@pytest.fixture
def calls(monkeypatch):
    seen: list = []
    transport = httpx.MockTransport(_fake(seen))
    monkeypatch.setattr(server, "_client", lambda: httpx.Client(transport=transport))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("BOX_CLIENT_ID", "id")
    monkeypatch.setenv("BOX_CLIENT_SECRET", "secret")
    monkeypatch.setenv("BOX_ENTERPRISE_ID", "123")
    monkeypatch.delenv("BOX_USER_ID", raising=False)
    monkeypatch.delenv("WORKPLACE_SLACK_CHANNELS", raising=False)
    monkeypatch.delenv("WORKPLACE_SLACK_DM_USERS", raising=False)
    server._box_cache.update(token="", expires_at=0.0)
    server._recent_dms.clear()
    server._recent_file_replies.clear()
    return seen


def _posted(calls):
    return [json.loads(r.content) for r in calls if r.url.path == "/api/chat.postMessage"]


def test_slack_post_resolves_channel_name_and_mentions(calls):
    out = server.slack_post("#automation-strategy-test", "Add your highlights",
                                       ["report@example.com", "ghost@example.com"])
    assert out["ok"] and out["data"]["channel"] == "C0CBE6CS0TE"
    assert out["data"]["mentioned"] == 1 and out["data"]["not_found"] == ["ghost@example.com"]
    (msg,) = _posted(calls)
    assert msg["channel"] == "C0CBE6CS0TE" and msg["text"].startswith("<@U0REP>\nAdd your highlights")


def test_slack_post_respects_the_channel_allowlist(calls, monkeypatch):
    monkeypatch.setenv("WORKPLACE_SLACK_CHANNELS", "some-other-channel")
    out = server.slack_post("automation-strategy-test", "hi")
    assert not out["ok"] and out["code"] == "forbidden"
    assert _posted(calls) == []


def test_slack_dm_opens_a_dm_by_email(calls):
    out = server.slack_dm("vp@example.com", "Report One added an entry")
    assert out["ok"] and out["data"] == {"user": "U0VP", "channel": "D0DM", "ts": "1.2"}
    (msg,) = _posted(calls)
    assert msg["channel"] == "D0DM"


def test_slack_dm_sends_an_identical_repeat_only_once(calls):
    first = server.slack_dm("vp@example.com", "Report One added an entry")
    again = server.slack_dm("vp@example.com", "Report One added an entry")
    assert first["ok"] and again["ok"] and again["data"]["duplicate"] is True
    assert len(_posted(calls)) == 1
    assert server.slack_dm("vp@example.com", "a different message")["ok"]
    assert len(_posted(calls)) == 2


def test_tools_return_structured_dicts(calls):
    # a JSON string reaches a CUGA agent as {"result": "<text>"}; a dict arrives as data
    assert isinstance(server.box_file_info("77"), dict)


def test_slack_dm_respects_the_recipient_allowlist(calls, monkeypatch):
    monkeypatch.setenv("WORKPLACE_SLACK_DM_USERS", "vp@example.com")
    assert server.slack_dm("vp@example.com", "ok")["ok"]
    out = server.slack_dm("report@example.com", "nope")
    assert not out["ok"] and out["code"] == "forbidden"


def test_slack_errors_come_back_as_tool_errors(calls):
    out = server.slack_dm("ghost@example.com", "hi")
    assert not out["ok"] and out["code"] == "slack_error" and "users_not_found" in out["error"]


def test_missing_slack_token_is_reported(calls, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    out = server.slack_post("#automation-strategy-test", "hi")
    assert not out["ok"] and out["code"] == "missing_key"


def test_box_file_info_returns_the_uploader(calls):
    out = server.box_file_info("77")
    assert out["ok"]
    d = out["data"]
    assert d["name"] == "highlights.txt" and d["folder"] == "SIL Highlights (test)"
    assert d["uploaded_by"] == {"name": "Anupama Murthi", "email": "a@example.com"}
    # the token is minted once and cached
    assert server.box_file_info("77")["ok"]
    assert sum(r.url.path == "/oauth2/token" for r in calls) == 1


def test_box_file_info_validates_and_reports_not_found(calls):
    assert server.box_file_info("../etc")["code"] == "bad_input"
    assert server.box_file_info("404")["code"] == "not_found"


def test_http_app_requires_the_bearer_token(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("WORKPLACE_MCP_TOKEN", "s3cret")
    app = server.build_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}},
        }
        r = c.post("/mcp", json=init, headers={
            "Authorization": "Bearer s3cret",
            "Accept": "application/json, text/event-stream",
        })
        assert r.status_code == 200 and "mcp-workplace" in r.text


def test_refuses_to_start_open_without_a_token(monkeypatch):
    monkeypatch.delenv("WORKPLACE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("WORKPLACE_MCP_ALLOW_NO_AUTH", raising=False)
    with pytest.raises(SystemExit):
        server.build_app()


def test_reply_in_thread_goes_under_the_latest_matching_bot_post(calls):
    HISTORY[:] = [
        {"ts": "9.0", "user": "U0REP", "text": "SIL highlights question from a human"},
        {"ts": "8.5", "bot_id": "B0BOT", "thread_ts": "7.0", "text": "a thread reply from the bot"},
        {"ts": "8.0", "bot_id": "B0BOT", "text": "Hi team, time for SIL highlights"},
        {"ts": "6.0", "bot_id": "B0BOT", "text": "Hi team, time for SIL highlights (older)"},
    ]
    out = server.slack_reply_in_thread("#automation-strategy-test", "Done: Anupama added x.txt", "SIL highlights")
    assert out["ok"] and out["data"]["thread_ts"] == "8.0"
    (msg,) = _posted(calls)
    assert msg["thread_ts"] == "8.0" and msg["text"] == "Done: Anupama added x.txt"


def test_reply_in_thread_falls_back_to_a_new_message(calls):
    HISTORY[:] = [{"ts": "9.0", "user": "U0REP", "text": "nothing from the bot"}]
    out = server.slack_reply_in_thread("automation-strategy-test", "Done", "SIL highlights")
    assert out["ok"] and out["data"]["thread_ts"] is None
    (msg,) = _posted(calls)
    assert "thread_ts" not in msg


def test_reply_in_thread_respects_the_channel_allowlist(calls, monkeypatch):
    monkeypatch.setenv("WORKPLACE_SLACK_CHANNELS", "elsewhere")
    assert server.slack_reply_in_thread("#automation-strategy-test", "Done")["code"] == "forbidden"


def test_reply_in_thread_mentions_the_person_or_falls_back_to_their_name(calls):
    HISTORY[:] = [{"ts": "8.0", "bot_id": "B0BOT", "text": "Hi team, time for SIL highlights"}]
    out = server.slack_reply_in_thread("#automation-strategy-test", "added their SIL highlights: x.txt",
                                       "SIL highlights", mention_email="report@example.com",
                                       fallback_name="Report One")
    assert out["data"]["mentioned"] is True
    out = server.slack_reply_in_thread("#automation-strategy-test", "added their SIL highlights: y.txt",
                                       "SIL highlights", mention_email="bot@boxdevedition.com",
                                       fallback_name="Default App")
    assert out["data"]["mentioned"] is False
    first, second = _posted(calls)
    assert first["text"] == "<@U0REP> added their SIL highlights: x.txt"
    assert second["text"] == "Default App added their SIL highlights: y.txt"


def test_reply_in_thread_does_not_repeat_the_name(calls):
    HISTORY[:] = [{"ts": "8.0", "bot_id": "B0BOT", "text": "Hi team, time for SIL highlights"}]
    server.slack_reply_in_thread("#c", "Default App added their SIL highlights: a.txt", "SIL highlights",
                                 mention_email="bot@boxdevedition.com", fallback_name="Default App")
    server.slack_reply_in_thread("#c", "@Report One added their SIL highlights: b.txt", "SIL highlights",
                                 mention_email="report@example.com", fallback_name="Report One")
    first, second = _posted(calls)
    assert first["text"] == "Default App added their SIL highlights: a.txt"
    assert second["text"] == "<@U0REP> added their SIL highlights: b.txt"


def test_reply_in_thread_takes_the_box_person_object(calls):
    HISTORY[:] = [{"ts": "8.0", "bot_id": "B0BOT", "text": "Hi team, time for SIL highlights"}]
    server.slack_reply_in_thread("#c", "updated notes.txt", "SIL highlights",
                                 person={"name": "Default App", "email": "bot@boxdevedition.com"})
    server.slack_reply_in_thread("#c", "updated notes.txt", "SIL highlights",
                                 person={"name": "Report One", "email": "report@example.com"})
    first, second = _posted(calls)
    assert first["text"] == "Default App updated notes.txt"
    assert second["text"] == "<@U0REP> updated notes.txt"


def test_reply_about_box_file_decides_new_or_edited_from_box(calls, monkeypatch):
    HISTORY[:] = [{"ts": "8.0", "bot_id": "B0BOT", "text": "Hi team, time for SIL highlights"}]
    new = {"name": "n.txt", "uploaded_at": "2026-10-22T10:00:00-07:00", "last_modified_at": "2026-10-22T10:00:01-07:00",
           "uploaded_by": {"name": "Report One", "email": "report@example.com"}, "last_modified_by": {"name": "X", "email": "x@x"}}
    edited = {**new, "name": "e.txt", "last_modified_at": "2026-10-22T11:00:00-07:00",
              "last_modified_by": {"name": "Ghost", "email": "ghost@example.com"}}
    data = {"1": new, "2": edited}
    monkeypatch.setattr(server, "box_file_info", lambda fid: {"ok": True, "data": data[fid]})
    a = server.slack_reply_about_box_file("#c", "1", "SIL highlights", action="updated")  # wrong hint ignored
    u = server.slack_reply_about_box_file("#c", "2", "SIL highlights", action="added")
    assert (a["data"]["action"], a["data"]["person"]) == ("added", "Report One")
    assert (u["data"]["action"], u["data"]["person"]) == ("updated", "Ghost")
    first, second = _posted(calls)
    assert first["text"] == "<@U0REP> added n.txt ✅"
    assert second["text"] == "Ghost updated e.txt ✏️" and second["thread_ts"] == "8.0"


def test_reply_about_box_file_posts_once_per_file(calls, monkeypatch):
    HISTORY[:] = [{"ts": "8.0", "bot_id": "B0BOT", "text": "SIL highlights"}]
    meta = {"name": "n.txt", "uploaded_at": "2026-10-22T10:00:00-07:00", "last_modified_at": "2026-10-22T10:00:00-07:00",
            "uploaded_by": {"name": "A", "email": "a@x"}, "last_modified_by": {"name": "A", "email": "a@x"}}
    monkeypatch.setattr(server, "box_file_info", lambda fid: {"ok": True, "data": meta})
    first = server.slack_reply_about_box_file("#c", "77")
    again = server.slack_reply_about_box_file("#c", "77")
    assert first["data"]["note"].startswith("Posted") and again["data"]["duplicate"] is True
    assert len(_posted(calls)) == 1
    meta["last_modified_at"] = "2026-10-22T12:00:00-07:00"  # a later edit is separate news
    server.slack_reply_about_box_file("#c", "77")
    assert len(_posted(calls)) == 2


_PARENT = {"ts": "5.0", "user": "U0BOT", "text": "SIL highlights please"}


def _thread(*texts):
    return [_PARENT] + [{"ts": f"5.{i + 1}", "thread_ts": "5.0", "user": "U0BOT", "text": t}
                        for i, t in enumerate(texts)]


def test_notify_waits_until_everyone_is_credited(calls, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HISTORY", [_PARENT])
    monkeypatch.setattr(sys.modules[__name__], "REPLIES", _thread("<@U0REP> added q4.txt \u2705"))
    out = server.slack_notify_when_all_replied("#automation-strategy-test", ["report@example.com", "vp@example.com"],
                                               "vp@example.com", "Everyone updated", "SIL highlights")
    assert out["ok"] and out["data"]["complete"] is False and out["data"]["waiting"] == ["vp@example.com"]
    assert _posted(calls) == []


def test_notify_sends_once_when_complete_and_closes_the_thread(calls, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HISTORY", [_PARENT])
    monkeypatch.setattr(sys.modules[__name__], "REPLIES", _thread("<@U0REP> added q4.txt \u2705"))
    out = server.slack_notify_when_all_replied("#automation-strategy-test", ["report@example.com"],
                                               "vp@example.com", "Everyone updated", "SIL highlights")
    assert out["ok"] and out["data"]["notified"] is True
    posts = _posted(calls)
    assert posts[0] == {"channel": "D0DM", "text": "Everyone updated"}
    assert posts[1]["thread_ts"] == "5.0" and server._DONE_MARK in posts[1]["text"]


def test_notify_does_not_resend_after_the_closing_line(calls, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HISTORY", [_PARENT])
    monkeypatch.setattr(sys.modules[__name__], "REPLIES", _thread(
        "<@U0REP> added q4.txt \u2705", f"\U0001f389 {server._DONE_MARK}. I let the lead know.",
        "<@U0REP> updated q4.txt \u270f\ufe0f"))
    out = server.slack_notify_when_all_replied("#automation-strategy-test", ["report@example.com"],
                                               "vp@example.com", "Everyone updated")
    assert out["ok"] and out["data"]["notified"] is False and _posted(calls) == []

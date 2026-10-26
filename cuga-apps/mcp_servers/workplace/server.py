"""mcp-workplace — Slack and Box actions for event-driven CUGA agents.

Tools:
  - slack_post(channel, text, mention_emails)   post in a channel, @mentioning people by email
  - slack_reply_in_thread(channel, text, match_text)  reply under the bot's latest matching post
  - slack_reply_about_box_file(channel, file_id, match_text)  the same, about a Box file: looks it
    up, decides new vs edited from Box, and puts the uploader or last editor in front
  - slack_dm(user, text)                        direct-message one person (email or Slack user id)
  - box_file_info(file_id)                      a Box file's name, uploader and timestamps

Unlike the other servers here, this one acts AS a bot in a real workspace and reads a real Box
account, so it is NOT open: every MCP request must carry `Authorization: Bearer <WORKPLACE_MCP_TOKEN>`.
It refuses to start without that token unless WORKPLACE_MCP_ALLOW_NO_AUTH=1 (local dev only).

Credentials (server-side only — the agent never sees them):
  SLACK_BOT_TOKEN                                   bot scopes: chat:write, channels:read, channels:history,
                                                    im:write, users:read, users:read.email
  BOX_CLIENT_ID + BOX_CLIENT_SECRET + BOX_ENTERPRISE_ID (or BOX_USER_ID)   client-credentials grant

Optional guard rails:
  WORKPLACE_SLACK_CHANNELS   comma-separated channel names/ids slack_post may write to
  WORKPLACE_SLACK_DM_USERS   comma-separated emails/ids slack_dm may write to
"""
from __future__ import annotations

import hmac
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVERS_ROOT = _HERE.parent
if str(_SERVERS_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_SERVERS_ROOT.parent))

import httpx  # noqa: E402

from mcp_servers._core.serve import make_server  # noqa: E402
from apps._ports import MCP_WORKPLACE_PORT  # noqa: E402

mcp = make_server("mcp-workplace")

SLACK_API = "https://slack.com/api"
BOX_API = "https://api.box.com/2.0"
BOX_TOKEN_URL = "https://api.box.com/oauth2/token"
_TIMEOUT = float(os.getenv("MCP_HTTP_TIMEOUT", "20"))


# Tools return DICTS, not the JSON strings the shared _core helpers produce. A string return reaches
# a CUGA agent as {"result": "<json text>"}, and agents reliably read it as a dict anyway — a live
# run DMed "unknown uploader added unknown file" before re-parsing and DMing again. A dict comes
# back as structured output, so `result["data"]["uploaded_by"]["name"]` just works.
def tool_result(data) -> dict:
    return {"ok": True, "data": data}


def tool_error(message: str, code: str = "error") -> dict:
    return {"ok": False, "error": message, "code": code}


def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT)


def _allowlist(var: str) -> set[str]:
    return {x.strip().lstrip("#").lower() for x in os.getenv(var, "").split(",") if x.strip()}


# ── Slack ───────────────────────────────────────────────────────────────

def _slack(method: str, *, params: dict | None = None, json: dict | None = None) -> dict:
    tok = os.getenv("SLACK_BOT_TOKEN", "")
    if not tok:
        raise _MissingKey("SLACK_BOT_TOKEN is not set on the MCP server")
    with _client() as c:
        if json is not None:
            r = c.post(f"{SLACK_API}/{method}", headers={"Authorization": f"Bearer {tok}"}, json=json)
        else:
            r = c.get(f"{SLACK_API}/{method}", headers={"Authorization": f"Bearer {tok}"}, params=params)
    data = r.json()
    if not data.get("ok"):
        raise _SlackError(f"{method}: {data.get('error', 'unknown_error')}")
    return data


class _MissingKey(Exception):
    pass


class _SlackError(Exception):
    pass


def _user_id(user: str) -> str:
    """An email → its Slack user id; a user id (U…/W…) passes through."""
    u = user.strip().lstrip("@")
    if "@" in u:
        return _slack("users.lookupByEmail", params={"email": u})["user"]["id"]
    return u


def _channel_id(channel: str) -> str:
    """A public channel name (with or without '#') → its id; an id (C…/G…) passes through."""
    name = channel.strip().lstrip("#")
    if name[:1] in ("C", "G") and name.upper() == name:
        return name
    cursor = ""
    while True:
        # Public channels only: listing private ones needs groups:read, which the bot does not
        # have. A private channel still works when it is passed by id.
        params = {"limit": 1000, "types": "public_channel", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        data = _slack("conversations.list", params=params)
        for ch in data.get("channels", []):
            if ch.get("name") == name:
                return ch["id"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            raise _SlackError(f"no channel named #{name} that the bot can see")


@mcp.tool()
def slack_post(channel: str, text: str, mention_emails: list[str] | None = None) -> dict:
    """Post a message in a Slack channel as the bot, optionally @mentioning people.

    Use this for announcements and reminders that a whole channel should see.

    Args:
        channel: Channel name such as "#automation-strategy-test" (the "#" is optional) or a channel id.
        text: The message. Plain text; Slack formatting such as *bold* works.
        mention_emails: Emails of people to @mention. They are looked up in Slack and the
            mentions are put at the start of the message, so they get notified.

    Returns:
        {"ok": true, "data": {"channel", "ts", "mentioned", "not_found"}} or {"ok": false, "error", "code"}.
    """
    if not text or not text.strip():
        return tool_error("text must not be empty", code="bad_input")
    allowed = _allowlist("WORKPLACE_SLACK_CHANNELS")
    try:
        cid = _channel_id(channel)
        if allowed and channel.strip().lstrip("#").lower() not in allowed and cid.lower() not in allowed:
            return tool_error(f"posting to {channel} is not allowed on this server", code="forbidden")
        mentions, missing = [], []
        for email in mention_emails or []:
            try:
                mentions.append(f"<@{_user_id(email)}>")
            except _SlackError:
                missing.append(email)
        body = (" ".join(mentions) + "\n" + text) if mentions else text
        data = _slack("chat.postMessage", json={"channel": cid, "text": body})
    except _MissingKey as e:
        return tool_error(str(e), code="missing_key")
    except _SlackError as e:
        return tool_error(str(e), code="slack_error")
    except httpx.HTTPError as e:
        return tool_error(f"Slack unreachable: {e}", code="upstream_error")
    return tool_result(
        {"channel": cid, "ts": data.get("ts"), "mentioned": len(mentions), "not_found": missing}
    )


@mcp.tool()
def slack_reply_in_thread(
    channel: str,
    text: str,
    match_text: str = "",
    mention_email: str = "",
    fallback_name: str = "",
    person: dict | None = None,
) -> dict:
    """Reply in the thread of the bot's most recent message in a channel.

    Use this to answer under an earlier announcement (for example, to say "Done" under the latest
    reminder) instead of starting a new message.

    Args:
        channel: Channel name such as "#automation-strategy-test" or a channel id.
        text: The reply.
        match_text: Optional words the parent message must contain (e.g. "SIL highlights"), so the
            reply goes under the right announcement. Empty → the bot's latest message.
        mention_email: Optional email of the person the reply is about. If it matches a Slack
            account, the reply starts with an @mention of them (they get notified).
        fallback_name: Name to start the reply with when mention_email has no Slack account.
        person: Shortcut for both: pass box_file_info's "uploaded_by" or "last_modified_by" object
            ({"name": ..., "email": ...}) as is.
            Don't repeat the person in `text`: the tool puts them in front, so pass e.g.
            text="added their SIL highlights: q4.txt" → "@Ana added their SIL highlights: q4.txt".

    Returns:
        {"ok": true, "data": {"channel", "ts", "thread_ts", "mentioned"}} or {"ok": false, "error", "code"}.
        If the bot has no matching message in the last 200, the reply is posted as a new message
        and "thread_ts" is null.
    """
    if not text or not text.strip():
        return tool_error("text must not be empty", code="bad_input")
    if isinstance(person, dict):
        mention_email = mention_email or str(person.get("email") or "")
        fallback_name = fallback_name or str(person.get("name") or "")
    allowed = _allowlist("WORKPLACE_SLACK_CHANNELS")
    try:
        cid = _channel_id(channel)
        if allowed and channel.strip().lstrip("#").lower() not in allowed and cid.lower() not in allowed:
            return tool_error(f"posting to {channel} is not allowed on this server", code="forbidden")
        who = _slack("auth.test", json={})
        mine = {who.get("user_id"), who.get("bot_id")} - {None}
        parent = None
        # history is newest first; a parent is a top-level message (not itself a thread reply)
        for m in _slack("conversations.history", params={"channel": cid, "limit": 200}).get("messages", []):
            if not ({m.get("user"), m.get("bot_id")} & mine):
                continue
            if m.get("thread_ts") not in (None, m.get("ts")):
                continue
            if match_text and match_text.lower() not in (m.get("text") or "").lower():
                continue
            parent = m["ts"]
            break
        prefix, mentioned = fallback_name.strip(), False
        if mention_email.strip():
            try:
                prefix, mentioned = f"<@{_user_id(mention_email)}>", True
            except _SlackError:
                pass  # no Slack account for that email (e.g. a bot's upload) → the plain name
        # The tool owns the "who" at the front. If the caller already wrote the name (or "@name")
        # there too, drop theirs so the reply never reads "Default App Default App added …".
        body_text = text.strip()
        for lead in {fallback_name.strip(), f"@{fallback_name.strip()}"} - {"", "@"}:
            if body_text.lower().startswith(lead.lower()):
                body_text = body_text[len(lead):].lstrip(" :,-")
        body = {"channel": cid, "text": f"{prefix} {body_text}" if prefix else body_text}
        if parent:
            body["thread_ts"] = parent
        data = _slack("chat.postMessage", json=body)
    except _MissingKey as e:
        return tool_error(str(e), code="missing_key")
    except _SlackError as e:
        return tool_error(str(e), code="slack_error")
    except httpx.HTTPError as e:
        return tool_error(f"Slack unreachable: {e}", code="upstream_error")
    return tool_result({"channel": cid, "ts": data.get("ts"), "thread_ts": parent, "mentioned": mentioned})


@mcp.tool()
def slack_reply_about_box_file(channel: str, file_id: str, match_text: str = "", action: str = "") -> dict:
    """Reply in the thread of the bot's latest post about a Box file, naming who did it.

    One call does it all: looks the file up in Box, decides from Box itself whether the file is new
    or was edited, picks that person (the uploader, or the last editor), @mentions them if their
    email has a Slack account (else uses their Box name) and replies under the bot's latest post:
    "@person added <file> ✅" or "@person updated <file> ✏️".

    Args:
        channel: Channel name such as "#automation-strategy-test" or a channel id.
        file_id: The numeric Box file id.
        match_text: Optional words the parent post must contain (e.g. "SIL highlights").
        action: Ignored; kept for older callers. Box's own timestamps decide.

    Returns:
        {"ok": true, "data": {"channel", "ts", "thread_ts", "mentioned", "person", "file", "action"}}
        or {"ok": false, "error", "code"}.
    """
    info = box_file_info(file_id)
    if not info.get("ok"):
        return info
    f = info["data"]
    act = "updated" if _edited(f) else "added"
    key = (channel.strip().lstrip("#").lower(), str(file_id).strip(), act, f.get("last_modified_at") or "")
    prev = _recent_file_replies.get(key)
    if prev and time.time() - prev[0] < _DM_DEDUP_SECS:
        return tool_result({**prev[1], "duplicate": True, "note": "Already posted. Nothing more to do."})
    who = f.get("uploaded_by") if act == "added" else f.get("last_modified_by")
    mark = "\u2705" if act == "added" else "\u270f\ufe0f"
    body = f"{act} {f.get('name') or file_id} {mark}"
    out = slack_reply_in_thread(channel, body, match_text, person=who or {})
    if out.get("ok"):
        out["data"].update(
            {
                "person": (who or {}).get("name"),
                "file": f.get("name"),
                "action": act,
                # mentioned=false is not a failure (no Slack account → the name is used instead);
                # say so, or a model reads it as one and posts the same reply again.
                "note": "Posted. Nothing more to do.",
            }
        )
        _recent_file_replies[key] = (time.time(), out["data"])
    return out


_DONE_MARK = "Everyone has responded"


@mcp.tool()
def slack_notify_when_all_replied(
    channel: str, people: list[str], notify: str, text: str, match_text: str = ""
) -> dict:
    """DM one person once every listed person has been credited under the bot's latest post.

    "Credited" means a bot reply in that post's thread starts with an @mention of the person (as
    slack_reply_about_box_file writes them). Call it after each such reply; it does nothing until
    the list is complete, then sends `text` to `notify` and closes the thread with one line. It
    sends at most once per post, so calling it again is safe.

    Args:
        channel: Channel name such as "#automation-strategy-test" or a channel id.
        people: Emails of everyone who must be credited.
        notify: Email of the person to DM when all are in.
        text: The DM.
        match_text: Optional words the parent post must contain (e.g. "SIL highlights").

    Returns:
        {"ok": true, "data": {"complete", "notified", "done": [emails], "waiting": [emails]}}
        or {"ok": false, "error", "code"}.
    """
    if not people or not notify.strip() or not text.strip():
        return tool_error("people, notify and text are required", code="bad_input")
    allowed = _allowlist("WORKPLACE_SLACK_CHANNELS")
    try:
        cid = _channel_id(channel)
        if allowed and channel.strip().lstrip("#").lower() not in allowed and cid.lower() not in allowed:
            return tool_error(f"posting to {channel} is not allowed on this server", code="forbidden")
        who = _slack("auth.test", json={})
        mine = {who.get("user_id"), who.get("bot_id")} - {None}
        parent = None
        for m in _slack("conversations.history", params={"channel": cid, "limit": 200}).get("messages", []):
            if not ({m.get("user"), m.get("bot_id")} & mine) or m.get("thread_ts") not in (None, m.get("ts")):
                continue
            if match_text and match_text.lower() not in (m.get("text") or "").lower():
                continue
            parent = m["ts"]
            break
        if not parent:
            return tool_error(f"no bot post in {channel} to check", code="not_found")
        replies = _slack("conversations.replies", params={"channel": cid, "ts": parent, "limit": 200})
        ours = [r for r in replies.get("messages", [])[1:] if {r.get("user"), r.get("bot_id")} & mine]
        credited = {t.split(">", 1)[0][2:] for t in (r.get("text") or "" for r in ours) if t.startswith("<@")}
        ids = {}
        for p in people:
            try:
                ids[p] = _user_id(p)
            except _SlackError:
                ids[p] = ""
        done = [p for p in people if ids[p] and ids[p] in credited]
        waiting = [p for p in people if p not in done]
        out = {"complete": not waiting, "notified": False, "done": done, "waiting": waiting}
        if waiting:
            return tool_result({**out, "note": "Not everyone yet. Nothing more to do."})
        if any(_DONE_MARK in (r.get("text") or "") for r in ours):
            return tool_result({**out, "note": "Already notified. Nothing more to do."})
        dm = slack_dm(notify, text)
        if not dm.get("ok"):
            return dm
        _slack(
            "chat.postMessage",
            json={"channel": cid, "thread_ts": parent, "text": f"\U0001f389 {_DONE_MARK}. I let the lead know."},
        )
    except _MissingKey as e:
        return tool_error(str(e), code="missing_key")
    except _SlackError as e:
        return tool_error(str(e), code="slack_error")
    except httpx.HTTPError as e:
        return tool_error(f"Slack unreachable: {e}", code="upstream_error")
    return tool_result({**out, "notified": True, "note": "Sent. Nothing more to do."})


def _edited(f: dict, grace_secs: float = 30.0) -> bool:
    """True when Box shows a change after the upload — a later version, not the first one. The
    model used to choose "added"/"updated" and sometimes chose wrong when its task was paraphrased;
    Box's own timestamps can't be paraphrased."""
    from datetime import datetime

    try:
        created = datetime.fromisoformat(f.get("uploaded_at") or "")
        modified = datetime.fromisoformat(f.get("last_modified_at") or "")
    except ValueError:
        return False
    return (modified - created).total_seconds() > grace_secs


# (channel, file id, action) → (posted_at, result). The same file announced twice in a row is a
# retry, not news.
_recent_file_replies: dict = {}


# (recipient, text) → (sent_at, result). An agent that retries a step re-sends the same DM; the
# person should still get it once.
_recent_dms: dict = {}
_DM_DEDUP_SECS = 120


@mcp.tool()
def slack_dm(user: str, text: str) -> dict:
    """Send a direct message to one person in Slack, as the bot.

    Use this to notify a specific person privately.

    Args:
        user: The person's email address (preferred) or their Slack user id (e.g. "U0CAV4QRWDV").
        text: The message. Send everything in ONE message; an identical repeat within two
            minutes is not sent again.

    Returns:
        {"ok": true, "data": {"user", "channel", "ts"}} (plus "duplicate": true when skipped)
        or {"ok": false, "error", "code"}.
    """
    if not text or not text.strip():
        return tool_error("text must not be empty", code="bad_input")
    allowed = _allowlist("WORKPLACE_SLACK_DM_USERS")
    key = (user.strip().lower(), text.strip())
    prev = _recent_dms.get(key)
    if prev and time.time() - prev[0] < _DM_DEDUP_SECS:
        return tool_result({**prev[1], "duplicate": True})
    try:
        uid = _user_id(user)
        if allowed and user.strip().lstrip("@").lower() not in allowed and uid.lower() not in allowed:
            return tool_error(f"messaging {user} is not allowed on this server", code="forbidden")
        im = _slack("conversations.open", json={"users": uid})["channel"]["id"]
        data = _slack("chat.postMessage", json={"channel": im, "text": text})
    except _MissingKey as e:
        return tool_error(str(e), code="missing_key")
    except _SlackError as e:
        return tool_error(str(e), code="slack_error")
    except httpx.HTTPError as e:
        return tool_error(f"Slack unreachable: {e}", code="upstream_error")
    out = {"user": uid, "channel": im, "ts": data.get("ts")}
    _recent_dms[key] = (time.time(), out)
    return tool_result(out)


# ── Box ─────────────────────────────────────────────────────────────────

_box_cache: dict = {"token": "", "expires_at": 0.0}


def _box_token() -> str:
    """A client-credentials access token, minted on demand and cached until shortly before expiry."""
    if _box_cache["token"] and time.time() < _box_cache["expires_at"]:
        return _box_cache["token"]
    cid, sec = os.getenv("BOX_CLIENT_ID", ""), os.getenv("BOX_CLIENT_SECRET", "")
    user, ent = os.getenv("BOX_USER_ID", ""), os.getenv("BOX_ENTERPRISE_ID", "")
    if not (cid and sec and (user or ent)):
        raise _MissingKey("BOX_CLIENT_ID, BOX_CLIENT_SECRET and BOX_ENTERPRISE_ID are not all set")
    form = {
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": sec,
        "box_subject_type": "user" if user else "enterprise",
        "box_subject_id": user or ent,
    }
    with _client() as c:
        r = c.post(BOX_TOKEN_URL, data=form)
    if r.status_code != 200:
        # never echo the body: some Box error paths repeat the client secret
        raise _BoxError(f"Box token request failed: HTTP {r.status_code}")
    j = r.json()
    _box_cache["token"] = j["access_token"]
    _box_cache["expires_at"] = time.time() + max(60.0, float(j.get("expires_in", 3600)) - 120)
    return _box_cache["token"]


class _BoxError(Exception):
    pass


def _who(entry: dict | None) -> dict:
    entry = entry or {}
    return {"name": entry.get("name") or "", "email": entry.get("login") or ""}


@mcp.tool()
def box_file_info(file_id: str) -> dict:
    """Look up a Box file: its name, who uploaded it, who last changed it, and when.

    Use this to find out who added a file to a Box folder.

    Args:
        file_id: The numeric Box file id (e.g. "2045812345678").

    Returns:
        {"ok": true, "data": {"id", "name", "size_bytes", "uploaded_by": {"name", "email"},
        "uploaded_at", "last_modified_by": {"name", "email"}, "last_modified_at", "folder"}}
        or {"ok": false, "error", "code"}.
    """
    fid = str(file_id).strip()
    if not fid.isdigit():
        return tool_error("file_id must be a numeric Box file id", code="bad_input")
    try:
        tok = _box_token()
        with _client() as c:
            r = c.get(
                f"{BOX_API}/files/{fid}",
                params={"fields": "id,name,size,created_at,modified_at,created_by,modified_by,parent"},
                headers={"Authorization": f"Bearer {tok}"},
            )
    except _MissingKey as e:
        return tool_error(str(e), code="missing_key")
    except _BoxError as e:
        return tool_error(str(e), code="box_error")
    except httpx.HTTPError as e:
        return tool_error(f"Box unreachable: {e}", code="upstream_error")
    if r.status_code == 404:
        return tool_error(f"no Box file {fid} that this account can see", code="not_found")
    if r.status_code != 200:
        return tool_error(f"Box returned HTTP {r.status_code}", code="box_error")
    f = r.json()
    return tool_result(
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "size_bytes": f.get("size"),
            "uploaded_by": _who(f.get("created_by")),
            "uploaded_at": f.get("created_at"),
            "last_modified_by": _who(f.get("modified_by")),
            "last_modified_at": f.get("modified_at"),
            "folder": (f.get("parent") or {}).get("name"),
        }
    )


# ── auth + serving ──────────────────────────────────────────────────────

class BearerAuth:
    """ASGI middleware: every request needs `Authorization: Bearer <token>`, except GET /health."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"ok": true}'})
                return
            return await self.app(scope, receive, send)
        got = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(got, self.expected):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
            return
        await self.app(scope, receive, send)


def build_app():
    """The streamable-HTTP MCP app, wrapped in bearer auth unless explicitly opened for local dev."""
    # Same host/DNS-rebinding handling as mcp_servers._core.serve.run — and it must happen BEFORE
    # the app is built, which is when the transport-security settings are read. The bearer token,
    # not a Host allow-list, is what guards this server.
    mcp.settings.host = os.getenv("MCP_BIND_HOST", "0.0.0.0")
    if mcp.settings.host not in ("127.0.0.1", "localhost", "::1"):
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    app = mcp.streamable_http_app()
    token = os.getenv("WORKPLACE_MCP_TOKEN", "")
    if token:
        return BearerAuth(app, token)
    if os.getenv("WORKPLACE_MCP_ALLOW_NO_AUTH") == "1":
        return app
    raise SystemExit(
        "WORKPLACE_MCP_TOKEN is not set. This server posts to Slack and reads Box, so it will not "
        "start open. Set the token, or WORKPLACE_MCP_ALLOW_NO_AUTH=1 for local development only."
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        build_app(),
        host=mcp.settings.host,
        port=int(os.getenv("MCP_PORT_OVERRIDE", str(MCP_WORKPLACE_PORT))),
        lifespan="on",
    )

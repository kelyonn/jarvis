"""Google Calendar and Drive over REST, using the same sign-in as Gmail."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from jarvis_core.adapters.google import GoogleSession

CAL_SCOPE = "https://www.googleapis.com/auth/calendar"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CAL_API = "https://www.googleapis.com/calendar/v3"
DRIVE_API = "https://www.googleapis.com/drive/v3"

_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
_KIND = {
    "application/vnd.google-apps.document": "doc", "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.presentation": "slides", "application/vnd.google-apps.folder": "folder",
    "application/pdf": "PDF",
}


class WorkspaceError(RuntimeError):
    """Not signed in, missing permission, or Google said no. The message is safe to say out loud."""


@dataclass
class Event:
    id: str
    title: str
    start: datetime | None
    end: datetime | None
    all_day: bool
    location: str
    calendar: str
    attendees: int
    link: str

    def line(self) -> str:
        when = "all day" if self.all_day else (self.start.astimezone().strftime("%a %d %b, %I:%M %p").replace(" 0", " ")
                                               if self.start else "")
        extra = f" at {self.location}" if self.location else ""
        crowd = f" ({self.attendees} people)" if self.attendees > 2 else ""
        return f"{when}: {self.title}{extra}{crowd}"


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    modified: str
    owner: str

    def line(self) -> str:
        kind = _KIND.get(self.mime, self.mime.rsplit("/", 1)[-1])
        return f"{self.name} ({kind}, edited {self.modified[:10]}{', by ' + self.owner if self.owner else ''})"


def _parse(d: dict[str, Any]) -> tuple[datetime | None, bool]:
    if "dateTime" in d:
        return datetime.fromisoformat(d["dateTime"].replace("Z", "+00:00")), False
    if "date" in d:
        return datetime.fromisoformat(d["date"]).astimezone(), True
    return None, False


class Workspace:
    def __init__(self, connector: str = "gmail", session: GoogleSession | None = None) -> None:
        self.session = session or GoogleSession(connector)

    # --- plumbing

    def has(self, scope: str) -> bool:
        return scope in self.session.scopes().split()

    def _need(self, scope: str, what: str) -> None:
        if not self.session.connected():
            raise WorkspaceError("You're not signed in to Google yet. Run jarvisd google reauth.")
        if not self.has(scope):
            raise WorkspaceError(f"I don't have permission to use Google {what} yet. Run jarvisd google reauth and tick the {what} box.")

    def _get(self, url: str, **params: Any) -> Any:
        r = self.session.request("GET", url, params=params)
        return self._ok(r)

    @staticmethod
    def _ok(r: Any) -> Any:
        if r.status_code == 403 and ("accessNotConfigured" in r.text or "has not been used" in r.text):
            raise WorkspaceError("That Google API isn't switched on for your Google Cloud project. Enable the Calendar and Drive APIs there.")
        if r.status_code in (401, 403):
            raise WorkspaceError("Google refused. Run jarvisd google reauth to grant the new permissions.")
        if r.status_code == 404:
            raise WorkspaceError("Google couldn't find that.")
        if r.status_code >= 400:
            raise WorkspaceError(f"Google had a problem ({r.status_code}).")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else r.text

    # --- calendar

    def events(self, start: datetime, end: datetime, query: str = "", limit: int = 30) -> list[Event]:
        self._need(CAL_SCOPE, "Calendar")
        cals = self._get(f"{CAL_API}/users/me/calendarList", minAccessRole="reader").get("items", [])
        out: list[Event] = []
        for cal in [c for c in cals if c.get("selected", True)][:8]:
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": limit}
            if query:
                params["q"] = query
            cid = cal["id"].replace("/", "%2F").replace("#", "%23")
            for it in self._get(f"{CAL_API}/calendars/{cid}/events", **params).get("items", []):
                if it.get("status") == "cancelled":
                    continue
                s, all_day = _parse(it.get("start", {}))
                e, _ = _parse(it.get("end", {}))
                me = next((a for a in it.get("attendees", []) if a.get("self")), None)
                if me and me.get("responseStatus") == "declined":
                    continue
                out.append(Event(it.get("id", ""), it.get("summary") or "(no title)", s, e, all_day, it.get("location", ""),
                                 cal.get("summaryOverride") or cal.get("summary", ""), len(it.get("attendees", [])),
                                 it.get("htmlLink", "")))
        out.sort(key=lambda ev: ev.start or datetime.max.astimezone())
        return out[:limit]

    def create_event(self, title: str, start: datetime, minutes: int = 60, location: str = "", notes: str = "") -> Event:
        self._need(CAL_SCOPE, "Calendar")
        body: dict[str, Any] = {"summary": title, "start": {"dateTime": start.isoformat()},
                                "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()}}
        if location:
            body["location"] = location
        if notes:
            body["description"] = notes
        it = self._ok(self.session.request("POST", f"{CAL_API}/calendars/primary/events", json=body))
        return Event(it.get("id", ""), title, start, start + timedelta(minutes=minutes), False, location, "primary", 0,
                     it.get("htmlLink", ""))

    # --- drive

    @staticmethod
    def _q(text: str) -> str:
        return text.replace("\\", "\\\\").replace("'", "\\'")

    def search_files(self, query: str, limit: int = 8) -> list[DriveFile]:
        self._need(DRIVE_SCOPE, "Drive")
        q = f"(name contains '{self._q(query)}' or fullText contains '{self._q(query)}') and trashed = false"
        data = self._get(f"{DRIVE_API}/files", q=q, pageSize=limit, orderBy="modifiedTime desc",
                         fields="files(id,name,mimeType,modifiedTime,owners(displayName))")
        return [DriveFile(f["id"], f["name"], f.get("mimeType", ""), f.get("modifiedTime", ""),
                          (f.get("owners") or [{}])[0].get("displayName", "")) for f in data.get("files", [])]

    def recent_files(self, limit: int = 8) -> list[DriveFile]:
        self._need(DRIVE_SCOPE, "Drive")
        data = self._get(f"{DRIVE_API}/files", q="trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                         pageSize=limit, orderBy="modifiedTime desc",
                         fields="files(id,name,mimeType,modifiedTime,owners(displayName))")
        return [DriveFile(f["id"], f["name"], f.get("mimeType", ""), f.get("modifiedTime", ""),
                          (f.get("owners") or [{}])[0].get("displayName", "")) for f in data.get("files", [])]

    def read_file(self, file_id: str, max_chars: int = 6000) -> str:
        self._need(DRIVE_SCOPE, "Drive")
        meta = self._get(f"{DRIVE_API}/files/{file_id}", fields="name,mimeType,size")
        mime = meta.get("mimeType", "")
        if mime in _EXPORT:
            text = self._ok(self.session.request("GET", f"{DRIVE_API}/files/{file_id}/export", params={"mimeType": _EXPORT[mime]}))
        elif mime.startswith("text/") or mime in ("application/json",):
            if int(meta.get("size") or 0) > 2_000_000:
                raise WorkspaceError("That file is too big to read out.")
            text = self._ok(self.session.request("GET", f"{DRIVE_API}/files/{file_id}", params={"alt": "media"}))
        else:
            raise WorkspaceError(f"I can't read {_KIND.get(mime, mime)} files from Drive yet, only docs, sheets, slides and text.")
        text = re.sub(r"\n{3,}", "\n\n", str(text)).strip()
        return f"{meta.get('name', 'File')}:\n{text[:max_chars]}" + ("\n[cut off]" if len(text) > max_chars else "")


def events_all(start: datetime, end: datetime, query: str = "", limit: int = 30) -> list[Event]:
    """Calendar events from every signed-in Google account, merged and de-duplicated. Accounts that fail are skipped;
    the error is raised only when none of them worked."""
    from jarvis_core.adapters.google import list_accounts

    from concurrent.futures import ThreadPoolExecutor

    out: list[Event] = []
    seen: set[tuple[str, str]] = set()
    errors: list[Exception] = []
    names = list_accounts() or ["gmail"]

    def fetch(name: str) -> list[Event] | Exception:
        try:
            return Workspace(name).events(start, end, query, limit)
        except Exception as e:  # noqa: BLE001
            return e

    # All accounts at once: one after another took 2-5 s each and pushed the briefing past its time limit.
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        results = list(pool.map(fetch, names))
    for res in results:
        if isinstance(res, Exception):
            errors.append(res)
            continue
        for ev in res:
            key = (ev.title.strip().lower(), ev.start.isoformat() if ev.start else "")
            if key not in seen:
                seen.add(key)
                out.append(ev)
    if errors and len(errors) == len(names):
        raise errors[0]
    out.sort(key=lambda ev: ev.start or datetime.max.astimezone())
    return out[:limit]

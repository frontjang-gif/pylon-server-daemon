"""capability: cap:com.outlook.calendar  (Windows, via Outlook COM).

The ONLY place win32com is imported. Reuses the approach verified live against
the real Outlook: open a room/user calendar via GetSharedDefaultFolder, restrict
to the target day (IncludeRecurrences), and extract each booking's time, subject
(often stripped by the room's Exchange config), organizer, and attendees with
their response status.

Methods (dispatched by the daemon):
  lookup(room, date=YYYY-MM-DD) -> {ok, source, bookings:[{start,end,subject,
      organizer, attendees:[{name,smtp,type,response}]}]}

Runs each call inside its own COM apartment (pythoncom.CoInitialize) because the
daemon handles invokes on worker threads.
"""
from __future__ import annotations

import datetime

_RESPONSE = {0: "none", 1: "organizer", 2: "tentative", 3: "accepted",
             4: "declined", 5: "notresponded"}
_RECIP_TYPE = {1: "required", 2: "optional", 3: "resource"}


def _fmt(dt):
    try:
        return dt.Format("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _smtp_of(addr_entry):
    try:
        ex = addr_entry.GetExchangeUser()
        if ex:
            return ex.PrimarySmtpAddress
    except Exception:
        pass
    try:
        return addr_entry.Address or ""
    except Exception:
        return ""


_FB_STATE = {"0": "free", "1": "tentative", "2": "busy", "3": "oof"}


def _freebusy_bookings(recip, day, slot_min=30):
    """Fallback when a room's full calendar can't be read: decode
    AddressEntry.GetFreeBusy into merged busy blocks for the LOCAL ``day``.

    GetFreeBusy(day, slot_min, True) returns one char per ``slot_min`` starting at
    the LOCAL midnight of ``day`` (verified against Outlook's own Scheduling
    Assistant display — the slots are already in the viewer's local time, NOT
    UTC). Char codes: '0' free, '1' tentative, '2' busy, '3' out-of-office.
    Returns [{start,end,subject,status}] in local time, or None if free/busy is
    unavailable.
    """
    import datetime as _dt
    try:
        ae = recip.AddressEntry
        if ae is None:
            return None
        fb = ae.GetFreeBusy(day, slot_min, True)   # local-midnight-relative slots
    except Exception:  # noqa: BLE001
        return None
    if not fb:
        return None
    base = _dt.datetime(day.year, day.month, day.day)   # local midnight
    # GetFreeBusy returns a FULL MONTH of slots from `day`; we only want the
    # requested day → cap to one day's worth (48 half-hour slots).
    slots_per_day = (24 * 60) // slot_min
    fb = fb[:slots_per_day]
    out = []
    n = len(fb)
    i = 0
    while i < n:
        c = fb[i]
        if c == "0":
            i += 1
            continue
        j = i
        while j < n and fb[j] == c:
            j += 1
        start = base + _dt.timedelta(minutes=i * slot_min)
        end = base + _dt.timedelta(minutes=j * slot_min)
        out.append({
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"),
            "subject": "(%s)" % _FB_STATE.get(c, "busy"),
            "organizer": "", "required": "", "optional": "",
            "status": _FB_STATE.get(c, "busy"), "attendees": [],
        })
        i = j
    return out


def _attendees(appt):
    out = []
    try:
        for r in appt.Recipients:
            try:
                ae = r.AddressEntry
                out.append({
                    "name": r.Name,
                    "smtp": _smtp_of(ae) if ae else "",
                    "type": _RECIP_TYPE.get(r.Type, str(r.Type)),
                    "response": _RESPONSE.get(getattr(r, "MeetingResponseStatus", 0), "none"),
                })
            except Exception as e:  # noqa: BLE001
                out.append({"name": getattr(r, "Name", "?"), "error": str(e)})
    except Exception as e:  # noqa: BLE001
        out.append({"error": str(e)})
    return out


def lookup(args, ctx):
    """args: {room, date}. Single-user: ctx.user is the logged-in Outlook user,
    so no per-user profile mapping is needed yet (the field is carried for the
    multi-user future)."""
    room = (args.get("room") or "").strip()
    date = (args.get("date") or "").strip()
    if not room or not date:
        return {"ok": False, "error": "room and date are required"}
    try:
        day = datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "error": "date must be YYYY-MM-DD"}

    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        try:
            ol = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            ol = win32com.client.Dispatch("Outlook.Application")
        ns = ol.GetNamespace("MAPI")

        recip = ns.CreateRecipient(room)
        recip.Resolve()
        if not recip.Resolved:
            return {"ok": False, "error": f"could not resolve room/user '{room}'",
                    "error_category": "application"}
        # Open the calendar. Prefer the SHARED calendar for the resolved recipient
        # (the room/other-user case). Two fallbacks make it robust:
        #   * 0x8004011D (MAPI_E_FAILONEPROVIDER) opening a shared folder is the
        #     classic signal that the target is actually the CURRENT user's own
        #     mailbox (or a profile/cached-mode quirk) → retry with the local
        #     GetDefaultFolder(9), which is the correct call for one's own calendar.
        #   * other HRESULTs are translated to an actionable message (below).
        cal = None
        _open_exc = None
        try:
            cal = ns.GetSharedDefaultFolder(recip, 9)  # olFolderCalendar
        except Exception as exc:  # noqa: BLE001
            _open_exc = exc
            hr = None
            try:
                ei = exc.args[2] if len(exc.args) > 2 else None
                hr = ei[5] if (ei and len(ei) > 5 and ei[5]) else exc.args[0]
            except Exception:  # noqa: BLE001
                hr = None
            hr32 = (hr & 0xFFFFFFFF) if isinstance(hr, int) else None
            if hr32 == 0x8004011D:   # provider error → likely our own mailbox
                try:
                    cal = ns.GetDefaultFolder(9)   # own calendar
                    _open_exc = None
                except Exception:  # noqa: BLE001
                    cal = None
            if cal is None and hr32 == 0x8004010F:
                # NOT_FOUND via the display-name recipient: re-resolve by the
                # recipient's SMTP (or Exchange legacyExchangeDN) and retry. A room
                # that resolves by display name sometimes yields a GAL object the
                # shared-folder open can't map in cached mode, but the explicit
                # SMTP recipient opens. Best-effort — keep the original error if
                # this doesn't help.
                try:
                    smtp = _smtp_of(recip.AddressEntry) if recip.AddressEntry else ""
                except Exception:  # noqa: BLE001
                    smtp = ""
                if smtp and smtp.lower() != room.lower():
                    try:
                        r2 = ns.CreateRecipient(smtp)
                        r2.Resolve()
                        if r2.Resolved:
                            cal = ns.GetSharedDefaultFolder(r2, 9)
                            if cal is not None:
                                _open_exc = None
                    except Exception:  # noqa: BLE001
                        cal = None
            if cal is None:
                if hr32 == 0x8004010F:   # MAPI_E_NOT_FOUND
                    msg = (f"resolved '{room}' but could not open its calendar — the "
                           f"name may not be a room/mailbox with a shared calendar, or "
                           f"you don't have permission to view it")
                elif hr32 == 0x80070005:  # E_ACCESSDENIED
                    msg = f"access denied opening the calendar for '{room}' (no permission)"
                elif hr32 == 0x8004011D:  # MAPI_E_FAILONEPROVIDER (and local fallback failed)
                    msg = (f"Outlook could not open the calendar for '{room}' "
                           f"(MAPI provider error 0x8004011D — a cached-mode / profile "
                           f"issue: the calendar can't be opened in this Outlook "
                           f"session; try opening it once in Outlook, or check Cached "
                           f"Exchange Mode / shared-folder download)")
                else:
                    hx = ("0x%08X" % hr32) if hr32 is not None else "?"
                    msg = f"could not open the calendar for '{room}' ({hx}): {exc}"
                return {"ok": False, "error": msg, "error_category": "application",
                        "resolved": True, "hresult": ("0x%08X" % hr32) if hr32 is not None else None}

        start = day.strftime("%m/%d/%Y 00:00 AM")
        end = (day + datetime.timedelta(days=1)).strftime("%m/%d/%Y 00:00 AM")
        # VALIDATE the opened folder is actually usable. GetSharedDefaultFolder can
        # return a LAZY handle that succeeds but throws 0x8004010F ("object could
        # not be found") on the FIRST real access (Name / Items / Restrict) — the
        # signature of a room/resource mailbox whose calendar the server will NOT
        # serve over MAPI shared-folder access (not published to you / resource
        # mailbox default perms). Probe + translate to an accurate message instead
        # of letting it fall through to the raw-tuple "COM lookup failed".
        try:
            items = cal.Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            restricted = items.Restrict(
                f"[Start] >= '{start}' AND [Start] < '{end}'")
            appt = restricted.GetFirst()   # force the first real read (throws if dead)
        except Exception as exc:  # noqa: BLE001
            hr = None
            try:
                ei = exc.args[2] if len(exc.args) > 2 else None
                hr = ei[5] if (ei and len(ei) > 5 and ei[5]) else exc.args[0]
            except Exception:  # noqa: BLE001
                hr = None
            hr32 = (hr & 0xFFFFFFFF) if isinstance(hr, int) else None
            if hr32 == 0x8004010F:
                # The full calendar can't be read (resource mailbox doesn't serve
                # its items over MAPI), BUT free/busy usually IS available. Fall
                # back to GetFreeBusy → busy time blocks (no subjects/organizer;
                # free/busy doesn't expose them). Still answers "when is the room
                # booked?", which is the primary question.
                fb_bookings = _freebusy_bookings(recip, day)
                if fb_bookings is not None:
                    try:
                        _rsmtp = _smtp_of(recip.AddressEntry) if recip.AddressEntry else ""
                    except Exception:  # noqa: BLE001
                        _rsmtp = ""
                    return {"ok": True, "source": "outlook-freebusy", "room": room,
                            "date": date, "limited": True, "smtp": _rsmtp,
                            "notes": ("Full calendar not readable for this room "
                                      "(no shared-calendar permission); showing "
                                      "FREE/BUSY only — busy time blocks, no "
                                      "subjects/organizer/attendees."),
                            "bookings": fb_bookings}
                msg = (f"'{room}' resolved, but its calendar can't be read over "
                       f"Outlook/MAPI and free/busy was unavailable — the room/"
                       f"resource mailbox doesn't grant shared-calendar access to "
                       f"you. Ask the room owner for calendar Reviewer permission, "
                       f"or view it in OWA. (MAPI 0x8004010F on item access.)")
            else:
                hx = ("0x%08X" % hr32) if hr32 is not None else "?"
                msg = f"could not read '{room}' calendar items ({hx}): {exc}"
            return {"ok": False, "error": msg, "error_category": "application",
                    "resolved": True, "opened": True,
                    "hresult": ("0x%08X" % hr32) if hr32 is not None else None}

        bookings = []
        while appt is not None:
            try:
                bookings.append({
                    "start": _fmt(appt.Start),
                    "end": _fmt(appt.End),
                    "subject": (appt.Subject or ""),
                    "organizer": getattr(appt, "Organizer", ""),
                    "required": getattr(appt, "RequiredAttendees", ""),
                    "optional": getattr(appt, "OptionalAttendees", ""),
                    "attendees": _attendees(appt),
                })
            except Exception as e:  # noqa: BLE001
                bookings.append({"error": str(e)})
            appt = restricted.GetNext()
        return {"ok": True, "source": "outlook-com", "room": room, "date": date,
                "bookings": bookings}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM lookup failed: {exc}",
                "error_category": "application"}
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def warmup(args, ctx):
    """Touch the default calendar once so the FIRST real lookup after a daemon
    (re)start doesn't pay the cold Outlook COM attach + MAPI spin-up — which,
    unguarded, could make the app's cap() call hang near its timeout and then
    retry, stacking into a ~100s cold lookup. The daemon calls this on connect
    (agent._safe_warmup). Best-effort; swallowed on failure."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        try:
            ol = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            ol = win32com.client.Dispatch("Outlook.Application")
        ns = ol.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)   # own calendar — cheap, always openable
        _ = cal.Items.Count
        return {"ok": True, "warmed": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "calendar warmup failed: %s" % exc,
                "error_category": "unavailable"}
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


# Capability method table the daemon dispatches on.
METHODS = {"lookup": lookup, "warmup": warmup}
CAPABILITY = "cap:com.outlook.calendar"
VARIANT = None
ATTRS = {"readonly": True}

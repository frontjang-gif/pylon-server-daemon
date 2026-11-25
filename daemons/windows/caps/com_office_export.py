"""capability: cap:com.office.export  (Windows, via Word/PowerPoint/Excel COM).

Exports an Office source document to a "truth" PDF using the REAL Office
renderer — Word for .doc/.docx/.rtf, PowerPoint for .ppt/.pptx, Excel for
.xls/.xlsx (#4) — so a converter's output can be pixel-diffed against what Office
itself produces (office_verify render_diff, MIGRATION #106). This is the ONLY
place win32com is imported for Office automation.

Methods (dispatched by the daemon):
  to_pdf(path, out_pdf=?) -> {ok, pdf, pages?}   export <path> to a PDF
  warmup() -> {ok}                               pay the cold-COM attach once

Runs each call inside its own COM apartment (pythoncom.CoInitialize) because the
daemon handles invokes on worker threads. Office apps are opened WITHOUT a window
and closed in a finally so a failed export never leaves a headless Office hung.
"""
from __future__ import annotations

import os
import tempfile

# Word/PowerPoint/Excel export format constants (COM enums).
_WD_EXPORT_PDF = 17          # wdExportFormatPDF
_PP_SAVE_AS_PDF = 32         # ppSaveAsPDF
_XL_TYPE_PDF = 0             # xlTypePDF (ExportAsFixedFormat Type arg)
_WORD_EXTS = (".doc", ".docx", ".docm", ".rtf", ".dotx", ".dotm", ".odt")
_PPT_EXTS = (".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".potx", ".potm", ".odp")
_XLS_EXTS = (".xls", ".xlsx", ".xlsm", ".xlsb", ".ods", ".csv")


def _kind(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in _WORD_EXTS:
        return "word"
    if ext in _PPT_EXTS:
        return "powerpoint"
    if ext in _XLS_EXTS:
        return "excel"
    return ""


def _export_word(path, out_pdf):
    import win32com.client
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(path, ReadOnly=True, AddToRecentFiles=False)
        # ExportAsFixedFormat(OutputFileName, ExportFormat=wdExportFormatPDF)
        doc.ExportAsFixedFormat(out_pdf, _WD_EXPORT_PDF)
        return True
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass


def _export_powerpoint(path, out_pdf):
    import win32com.client
    ppt = None
    pres = None
    # #23: PowerPoint's COM server is effectively a SINGLETON — unlike Word/Excel,
    # DispatchEx attaches to the user's ALREADY-RUNNING PowerPoint instead of
    # spawning a private hidden one. Calling Quit() in the finally would then
    # close the user's live session and lose unsaved work. So: detect whether
    # PowerPoint was already running BEFORE we touch COM; if it was, we must
    # NEVER Quit() — only Close() the presentation WE opened. Only Quit() when
    # WE launched the instance (nothing was running before).
    _ppt_was_running = False
    try:
        win32com.client.GetActiveObject("PowerPoint.Application")
        _ppt_was_running = True
    except Exception:
        _ppt_was_running = False
    try:
        ppt = win32com.client.DispatchEx("PowerPoint.Application")
        # PowerPoint refuses WithWindow=False on some builds; set it best-effort.
        try:
            pres = ppt.Presentations.Open(path, ReadOnly=True, WithWindow=False)
        except Exception:
            pres = ppt.Presentations.Open(path, ReadOnly=True)
        pres.SaveAs(out_pdf, _PP_SAVE_AS_PDF)
        return True
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        # Only quit the app if WE started it. If the user already had PowerPoint
        # open, DispatchEx handed us their singleton — quitting it would kill
        # their session, so we leave it running and only closed our presentation.
        if not _ppt_was_running:
            try:
                if ppt is not None:
                    ppt.Quit()
            except Exception:
                pass


def _export_excel(path, out_pdf):
    import win32com.client
    xl = None
    wb = None
    try:
        xl = win32com.client.DispatchEx("Excel.Application")
        xl.Visible = False
        xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(path, ReadOnly=True, UpdateLinks=0)
        # Workbook.ExportAsFixedFormat(Type=xlTypePDF, Filename=...)
        wb.ExportAsFixedFormat(_XL_TYPE_PDF, out_pdf)
        return True
    finally:
        try:
            if wb is not None:
                wb.Close(False)
        except Exception:
            pass
        try:
            if xl is not None:
                xl.Quit()
        except Exception:
            pass


def to_pdf(args, ctx):
    """Export an Office doc to a truth PDF via Word/PowerPoint COM.
    args: {path, out_pdf?}. Returns {ok, pdf, kind} or {ok:False, error}."""
    path = (args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path required", "error_category": "application"}
    if not os.path.isfile(path):
        return {"ok": False, "error": "file not found: %s" % path,
                "error_category": "application"}
    kind = _kind(path)
    if not kind:
        return {"ok": False, "error": "unsupported office type: %s" % os.path.splitext(path)[1],
                "error_category": "application"}
    out_pdf = (args.get("out_pdf") or "").strip()
    if not out_pdf:
        fd, out_pdf = tempfile.mkstemp(suffix=".pdf", prefix="office_truth_")
        os.close(fd)
    out_pdf = os.path.abspath(out_pdf)
    os.makedirs(os.path.dirname(out_pdf) or ".", exist_ok=True)

    import pythoncom
    pythoncom.CoInitialize()
    try:
        if kind == "word":
            _export_word(os.path.abspath(path), out_pdf)
        elif kind == "excel":
            _export_excel(os.path.abspath(path), out_pdf)
        else:
            _export_powerpoint(os.path.abspath(path), out_pdf)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "COM export failed: %s" % exc,
                "error_category": "application", "kind": kind}
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    if not os.path.isfile(out_pdf) or os.path.getsize(out_pdf) == 0:
        return {"ok": False, "error": "export produced no PDF", "error_category": "application"}
    return {"ok": True, "pdf": out_pdf, "kind": kind, "bytes": os.path.getsize(out_pdf)}


def warmup(args, ctx):
    """Pay the cold Office-COM attach once so the first real export isn't slow.

    args: {app?: 'word'|'powerpoint'|'both'}  (default 'both'). A .pptx export
    needs PowerPoint warmed, a .docx needs Word — so a kind-aware caller should
    warm the app it's about to use (office_verify passes the doc's kind).
    Best-effort; each app is swallowed on failure."""
    want = (args.get("app") or "both").strip().lower()
    apps = []
    if want in ("word", "both", "all"):
        apps.append(("word", "Word.Application"))
    if want in ("powerpoint", "ppt", "both", "all"):
        apps.append(("powerpoint", "PowerPoint.Application"))
    if want in ("excel", "xls", "all"):
        apps.append(("excel", "Excel.Application"))
    if not apps:
        apps = [("word", "Word.Application")]

    import pythoncom
    pythoncom.CoInitialize()
    warmed = []
    errors = {}
    try:
        import win32com.client
        for name, progid in apps:
            app = None
            # #23: never Quit() a PowerPoint the USER already had open (COM
            # singleton — DispatchEx attaches to it; Quit() would kill their
            # session). Detect a pre-existing instance and leave it alone.
            _was_running = False
            if name == "powerpoint":
                try:
                    win32com.client.GetActiveObject("PowerPoint.Application")
                    _was_running = True
                except Exception:  # noqa: BLE001
                    _was_running = False
            try:
                app = win32com.client.DispatchEx(progid)
                try:
                    app.Visible = False
                except Exception:  # noqa: BLE001 — PowerPoint may refuse Visible=False
                    pass
                warmed.append(name)
            except Exception as exc:  # noqa: BLE001
                errors[name] = str(exc)
            finally:
                if not _was_running:
                    try:
                        if app is not None:
                            app.Quit()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass
    if warmed:
        return {"ok": True, "warmed": warmed, "errors": errors or None}
    return {"ok": False, "error": "office warmup failed: %s" % (errors or "unknown"),
            "error_category": "unavailable"}


METHODS = {"to_pdf": to_pdf, "warmup": warmup}
CAPABILITY = "cap:com.office.export"
VARIANT = None
ATTRS = {"windows": True}

#!/usr/bin/env python3
"""FreeTalon Dashboard — local web UI with Gemini-style chat and Claw Monitor.

Run with:
    python dashboard.py

The dashboard binds to localhost only (127.0.0.1) to honour the project's
local-only security constraints.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

from nicegui import app, events, ui  # noqa: F401 – app imported for storage

# ---------------------------------------------------------------------------
# Upload safety constants
# ---------------------------------------------------------------------------

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB per file
_BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".msi",
    ".dll", ".so", ".dylib", ".bin",
}

# ---------------------------------------------------------------------------
# Workspace resolution (mirrors installer.py logic)
# ---------------------------------------------------------------------------

WORKSPACE = os.environ.get(
    "LOCAL_WORKSPACE",
    os.path.expanduser("~/freetalon-workspace"),
)

_env_path = Path(".env")
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        if _line.startswith("LOCAL_WORKSPACE="):
            WORKSPACE = _line.split("=", 1)[1].strip()
            break

Path(WORKSPACE).mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Shared mutable state (per-process, single user — local-only deployment)
# ---------------------------------------------------------------------------

_claw_values: list[float] = [round(random.uniform(0.15, 0.95), 2) for _ in range(10)]

# ---------------------------------------------------------------------------
# Inline CSS injected once into every page
# ---------------------------------------------------------------------------

_GLOBAL_CSS = """
/* Scrollbar styling */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #1e293b; }
::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #64748b; }

/* Chat bubbles */
.ft-bubble-user {
    background: #065f46;
    border-radius: 1rem 1rem 0.25rem 1rem;
    max-width: 42rem;
    word-break: break-word;
}
.ft-bubble-bot {
    background: #1e293b;
    border-radius: 1rem 1rem 1rem 0.25rem;
    max-width: 42rem;
    word-break: break-word;
}

/* Smooth bar fill animation */
.ft-bar-fill { transition: width 0.45s ease; }

/* Upload drop-zone hover */
.ft-dropzone { transition: border-color 0.2s, background-color 0.2s; }
.ft-dropzone:hover, .ft-dropzone.dragging {
    border-color: #10b981 !important;
    background-color: #0d2e22 !important;
}

/* Quasar textarea dark overrides */
.ft-textarea .q-field__control { background: #1e293b !important; }
.ft-textarea .q-field__native,
.ft-textarea .q-field__placeholder { color: #cbd5e1 !important; }
.ft-textarea .q-field__control:before { border-color: #334155 !important; }
.ft-textarea .q-field__control:hover:before { border-color: #10b981 !important; }
"""

# ---------------------------------------------------------------------------
# Upload panel style constants
# ---------------------------------------------------------------------------

_PANEL_STYLE_HIDDEN = (
    "position:fixed;bottom:5.5rem;right:2rem;z-index:99;width:22rem;"
    "background:#0f172a;border:1px solid #334155;border-radius:1rem;"
    "padding:1.25rem;display:none;"
    "box-shadow:0 8px 32px rgba(0,0,0,0.6);"
)
_PANEL_STYLE_VISIBLE = _PANEL_STYLE_HIDDEN.replace("display:none;", "display:block;")

# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------


@ui.page("/")
def index() -> None:
    # ── Global style injection ────────────────────────────────────────────
    ui.add_head_html(f"<style>{_GLOBAL_CSS}</style>")
    ui.query("body").style("background:#0f172a;color:#f1f5f9;margin:0;overflow:hidden;")

    # ── Per-page refs ─────────────────────────────────────────────────────
    bar_elements: list[ui.element] = []
    pct_labels: list[ui.label] = []
    upload_visible: list[bool] = [False]

    # ====================================================================== #
    # OUTER ROW — fills the viewport                                         #
    # ====================================================================== #
    with ui.row().classes("w-full gap-0").style("height:100vh;overflow:hidden;"):

        # ================================================================== #
        # LEFT PANEL — Chat interface                                        #
        # ================================================================== #
        with ui.column().classes("flex-1 h-full").style(
            "background:#0f172a;display:flex;flex-direction:column;overflow:hidden;"
        ):

            # -- Header ---------------------------------------------------
            with ui.row().classes("w-full px-6 py-4 items-center gap-3").style(
                "border-bottom:1px solid #1e293b;flex-shrink:0;"
            ):
                ui.icon("psychology_alt").classes("text-3xl").style("color:#10b981;")
                ui.label("FreeTalon").classes("text-2xl font-bold tracking-wide").style(
                    "color:#10b981;"
                )
                ui.label("• local openclaw hive").classes("text-sm").style(
                    "color:#475569;"
                )

            # -- Message area ---------------------------------------------
            msg_area = (
                ui.column()
                .classes("w-full px-6 py-4 gap-3")
                .style("flex:1;overflow-y:auto;")
            )
            with msg_area:
                ui.label("Send a message to get started.").classes(
                    "text-sm italic self-center mt-6"
                ).style("color:#475569;")

            # -- Input row ------------------------------------------------
            with ui.row().classes("w-full px-6 py-4 gap-3 items-end").style(
                "border-top:1px solid #1e293b;flex-shrink:0;background:#0f172a;"
            ):
                text_input = (
                    ui.textarea(placeholder="Message FreeTalon…")
                    .props("rows=1 autogrow outlined dense clearable")
                    .classes("flex-1 ft-textarea")
                )

                async def _send() -> None:
                    raw = (text_input.value or "").strip()
                    if not raw:
                        return
                    text_input.set_value("")

                    with msg_area:
                        with ui.row().classes("w-full justify-end"):
                            ui.label(raw).classes(
                                "ft-bubble-user px-4 py-2"
                            ).style("color:#ecfdf5;")

                    with msg_area:
                        with ui.row().classes("w-full justify-start gap-2 items-start"):
                            ui.icon("smart_toy").style("color:#10b981;margin-top:2px;")
                            ui.label(f"(echo) {raw}").classes(
                                "ft-bubble-bot px-4 py-2"
                            ).style("color:#cbd5e1;")

                    await ui.run_javascript(
                        "const el=document.querySelector('.ft-msg-area');"
                        "if(el){el.scrollTop=el.scrollHeight;}"
                    )

                msg_area.classes("ft-msg-area")

                ui.button(icon="send", on_click=_send).props(
                    "round unelevated"
                ).style("background:#059669;color:#fff;")

        # ================================================================== #
        # RIGHT PANEL — Claw Monitor sidebar                                 #
        # ================================================================== #
        with ui.column().classes("h-full").style(
            "width:18rem;background:#0f172a;border-left:1px solid #1e293b;"
            "display:flex;flex-direction:column;overflow:hidden;"
        ):

            # -- Sidebar header -------------------------------------------
            with ui.row().classes("w-full px-4 py-4 items-center gap-2").style(
                "border-bottom:1px solid #1e293b;flex-shrink:0;"
            ):
                ui.icon("memory").style("color:#10b981;font-size:1.3rem;")
                ui.label("Claw Monitor").classes("text-lg font-semibold").style(
                    "color:#10b981;"
                )

            # -- 10 progress bars -----------------------------------------
            with ui.column().classes("w-full px-4 py-3 gap-3").style(
                "flex:1;overflow-y:auto;"
            ):
                for i in range(10):
                    val = _claw_values[i]
                    pct = int(val * 100)

                    with ui.column().classes("w-full gap-1"):
                        with ui.row().classes("w-full justify-between items-center"):
                            ui.label(f"Claw {i}").classes("text-xs font-mono").style(
                                "color:#94a3b8;"
                            )
                            lbl = ui.label(f"{pct}%").classes(
                                "text-xs font-mono"
                            ).style("color:#34d399;")
                            pct_labels.append(lbl)

                        # Track
                        outer = ui.element("div").classes("w-full rounded-full").style(
                            "height:6px;background:#1e293b;"
                        )
                        with outer:
                            fill = (
                                ui.element("div")
                                .classes("ft-bar-fill h-full rounded-full")
                                .style(
                                    f"width:{pct}%;background:#10b981;"
                                )
                            )
                            bar_elements.append(fill)

            # -- Refresh button -------------------------------------------
            async def _refresh() -> None:
                for i in range(10):
                    new_val = round(random.uniform(0.05, 1.0), 2)
                    _claw_values[i] = new_val
                    pct = int(new_val * 100)
                    bar_elements[i].style(f"width:{pct}%;background:#10b981;")
                    pct_labels[i].set_text(f"{pct}%")
                ui.notify("Claw values refreshed", type="positive", position="top-right")

            with ui.row().classes("px-4 pb-4 pt-1"):
                ui.button("Refresh", icon="refresh", on_click=_refresh).props(
                    "flat dense"
                ).style("color:#10b981;")

    # ====================================================================== #
    # FLOATING UPLOAD PANEL                                                  #
    # ====================================================================== #

    # Toggle button (fixed, bottom-right)
    toggle_btn = (
        ui.button(icon="upload_file")
        .props("round fab")
        .style(
            "position:fixed;bottom:2rem;right:2rem;z-index:100;"
            "background:#059669;color:#fff;"
            "box-shadow:0 4px 24px rgba(16,185,129,0.4);"
        )
    )
    toggle_btn.tooltip("Upload files to workspace")

    # Upload panel (hidden by default)
    upload_panel = (
        ui.card()
        .classes("ft-dropzone")
        .style(_PANEL_STYLE_HIDDEN)
    )

    with upload_panel:
        with ui.row().classes("w-full justify-between items-center mb-3"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("folder_open").style("color:#10b981;")
                ui.label("Upload to Workspace").classes("font-semibold").style(
                    "color:#e2e8f0;"
                )
            ui.label(WORKSPACE).classes("text-xs truncate w-full mt-1").style(
                "color:#64748b;"
            )

        def _handle_upload(e: events.UploadEventArguments) -> None:
            # Strip directory components to prevent path traversal
            safe_name = Path(e.name).name
            if not safe_name:
                ui.notify("Invalid filename.", type="negative", position="top-right")
                return

            # Block potentially dangerous file extensions
            if Path(safe_name).suffix.lower() in _BLOCKED_EXTENSIONS:
                ui.notify(
                    f"File type not allowed: '{Path(safe_name).suffix}'",
                    type="negative",
                    position="top-right",
                )
                return

            # Enforce file size limit
            data = e.content.read()
            if len(data) > _MAX_UPLOAD_BYTES:
                ui.notify(
                    f"File exceeds 100 MB limit: '{safe_name}'",
                    type="negative",
                    position="top-right",
                )
                return

            dest = Path(WORKSPACE) / safe_name
            dest.write_bytes(data)
            ui.notify(
                f"✔ Saved '{safe_name}'",
                type="positive",
                position="top-right",
            )

        ui.upload(
            label="Drop files here or click to browse",
            on_upload=_handle_upload,
            multiple=True,
            auto_upload=True,
        ).props("color=green outlined").classes("w-full").style(
            "background:#1e293b;border-radius:0.75rem;"
        )

    def _toggle_upload() -> None:
        upload_visible[0] = not upload_visible[0]
        upload_panel.style(
            _PANEL_STYLE_VISIBLE if upload_visible[0] else _PANEL_STYLE_HIDDEN
        )

    toggle_btn.on_click(_toggle_upload)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="FreeTalon Dashboard",
        host="127.0.0.1",
        port=7860,
        dark=True,
        favicon="🦅",
        reload=False,
        storage_secret="freetalon-local-only",
    )

#!/usr/bin/env python3
"""FreeTalon Dashboard — local web UI with Gemini-style chat and Claw Monitor.

Run with:
    python dashboard.py

The dashboard binds to localhost only (127.0.0.1) to honour the project's
local-only security constraints.
"""

from __future__ import annotations

import asyncio
import heapq
import json as _json
import logging
import os
import random
import uuid
from pathlib import Path

from freetalon.bootstrap import ensure_module

_PROJECT_ROOT = Path(__file__).resolve().parent
ensure_module(
    "nicegui",
    _PROJECT_ROOT,
    f"python3 {_PROJECT_ROOT / 'installer.py'} --yes",
)

try:
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = None

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
# Claw Orchestrator (optional — requires a running Docker daemon)
# ---------------------------------------------------------------------------

_orchestrator: object | None = None
try:
    from orchestrator import ClawOrchestrator

    _orchestrator = ClawOrchestrator()
except Exception:  # noqa: BLE001 – Docker may not be installed/running
    logging.getLogger(__name__).warning(
        "Docker is not available — Agent Tasks panel will be disabled."
    )

# ---------------------------------------------------------------------------
# Orchestrator pipeline (intake → plan → execute)
# ---------------------------------------------------------------------------

_PIPELINE_AVAILABLE = False
_plan_store: "ExecutionPlanStateStore | None" = None  # type: ignore[name-defined]
_tool_registry: "ToolRegistry | None" = None  # type: ignore[name-defined]

try:
    from freetalon.orchestrator import (
        Executor,
        ExecutionPlanStateStore,
        PlanStatus,
        ToolRegistry,
    )
    from freetalon.orchestrator.intake import (
        LLMBackendError,
        LLMResponseError,
        intake_request,
    )
    from freetalon.orchestrator.planner import plan_task_intent

    # Persistent plan store — SQLite, restart-safe.
    _plan_store = ExecutionPlanStateStore()
    # Non-strict: nodes with unrecognised capabilities receive a no-op handler
    # instead of raising UnknownCapabilityError, so demo DAGs can reach COMPLETED.
    _tool_registry = ToolRegistry(strict=False)
    _PIPELINE_AVAILABLE = True
except Exception:  # noqa: BLE001
    logging.getLogger(__name__).warning(
        "Orchestrator pipeline unavailable — submit will show an error message."
    )

# ---------------------------------------------------------------------------
# Workspace resolution (mirrors installer.py logic)
# ---------------------------------------------------------------------------

_env_defaults: dict[str, str] = {}
_env_path = Path(".env")
if _env_path.exists():
    if dotenv_values is not None:
        _env_defaults = {
            _key: _value
            for _key, _value in dotenv_values(_env_path).items()
            if _key is not None and _value is not None
        }
    else:
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _value = _line.split("=", 1)
            _env_defaults[_key.strip()] = _value.strip()

WORKSPACE = os.environ.get(
    "LOCAL_WORKSPACE",
    _env_defaults.get("LOCAL_WORKSPACE", os.path.expanduser("~/freetalon-workspace")),
)
_default_ui_host = _env_defaults.get("FREETALON_UI_HOST", "127.0.0.1")
_default_ui_port = _env_defaults.get("FREETALON_UI_PORT", "7860")
UI_HOST = os.environ.get("FREETALON_UI_HOST", _default_ui_host)
_ui_port_raw = os.environ.get("FREETALON_UI_PORT", _default_ui_port)
try:
    UI_PORT = int(_ui_port_raw)
except ValueError as exc:
    raise SystemExit(
        f"Invalid FREETALON_UI_PORT value: {_ui_port_raw!r}. "
        "Re-run 'python3 installer.py --yes' or fix the .env file."
    ) from exc

Path(WORKSPACE).mkdir(parents=True, exist_ok=True)

# Screenshot directory — created here and served as static files so ui.image()
# can reference them by URL path (/screenshots/<filename>).
_SCREENSHOTS_DIR = Path(WORKSPACE) / "screenshots"
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
app.add_static_files("/screenshots", str(_SCREENSHOTS_DIR))

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
    # Swarm plan state — mutable single-element lists so inner closures can write.
    active_plan_id: list[str | None] = [None]
    plan_running: list[bool] = [False]
    send_btn_holder: list[ui.button] = []

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

            # -- DAG Progress Tree (hidden until a plan is submitted) ------
            _TREE_STYLE_HIDDEN = (
                "flex-shrink:0;max-height:14rem;overflow-y:auto;"
                "border-top:1px solid #1e293b;display:none;"
            )
            _TREE_STYLE_VISIBLE = _TREE_STYLE_HIDDEN.replace("display:none;", "display:block;")
            plan_tree_section = (
                ui.column()
                .classes("w-full px-6 py-2 gap-1")
                .style(_TREE_STYLE_HIDDEN)
            )
            with plan_tree_section:
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("account_tree").style("color:#94a3b8;font-size:1rem;")
                    ui.label("DAG Progress").classes("text-xs font-mono").style(
                        "color:#94a3b8;"
                    )
                plan_tree_rows = ui.column().classes("w-full gap-1 pb-1")

            # Status colours matching the existing dark palette.
            _NODE_STATUS_COLORS: dict[str, str] = {
                "completed": "#10b981",
                "running": "#3b82f6",
                "failed": "#ef4444",
                "cancelled": "#ef4444",
                "draft": "#475569",
                "ready": "#475569",
            }

            def _rebuild_plan_tree(plan: "ExecutionPlan") -> None:  # type: ignore[name-defined]
                """Rebuild the node rows from the current plan state."""
                plan_tree_rows.clear()
                with plan_tree_rows:
                    for node in plan.nodes:
                        color = _NODE_STATUS_COLORS.get(node.status.value, "#475569")
                        deps = ", ".join(node.depends_on) if node.depends_on else ""
                        with ui.row().classes("w-full items-start gap-2 py-1"):
                            ui.icon("circle").style(
                                f"color:{color};font-size:0.55rem;"
                                "margin-top:5px;flex-shrink:0;"
                            )
                            with ui.column().classes("flex-1 gap-0"):
                                with ui.row().classes("items-center gap-2 flex-wrap"):
                                    ui.label(node.id).classes(
                                        "text-xs font-mono font-semibold"
                                    ).style("color:#e2e8f0;")
                                    ui.label(node.status.value).classes(
                                        "text-xs font-mono"
                                    ).style(f"color:{color};")
                                obj = node.objective
                                if len(obj) > 80:
                                    obj = obj[:80] + "…"
                                ui.label(obj).classes("text-xs").style("color:#94a3b8;")
                                if deps:
                                    ui.label(f"↳ {deps}").classes(
                                        "text-xs font-mono"
                                    ).style("color:#475569;")
                                if node.error:
                                    err = node.error
                                    if len(err) > 80:
                                        err = err[:80] + "…"
                                    ui.label(err).classes("text-xs").style(
                                        "color:#ef4444;"
                                    )

            # -- Input row ------------------------------------------------
            with ui.row().classes("w-full px-6 py-4 gap-3 items-end").style(
                "border-top:1px solid #1e293b;flex-shrink:0;background:#0f172a;"
            ):
                text_input = (
                    ui.textarea(placeholder="Message FreeTalon…")
                    .props("rows=1 autogrow outlined dense clearable")
                    .classes("flex-1 ft-textarea")
                )

                def _re_enable() -> None:
                    """Re-enable input + send button after plan finishes."""
                    plan_running[0] = False
                    text_input.enable()
                    if send_btn_holder:
                        send_btn_holder[0].enable()

                async def _send() -> None:
                    raw = (text_input.value or "").strip()
                    if not raw:
                        return
                    if plan_running[0]:
                        ui.notify(
                            "A plan is already running — please wait.",
                            type="warning",
                            position="top-right",
                        )
                        return

                    text_input.set_value("")
                    text_input.disable()
                    if send_btn_holder:
                        send_btn_holder[0].disable()
                    plan_running[0] = True

                    # ── User message bubble ───────────────────────────────
                    with msg_area:
                        with ui.row().classes("w-full justify-end"):
                            ui.label(raw).classes(
                                "ft-bubble-user px-4 py-2"
                            ).style("color:#ecfdf5;")

                    # ── Bot status bubble (updated as pipeline progresses) ─
                    with msg_area:
                        with ui.row().classes(
                            "w-full justify-start gap-2 items-start"
                        ):
                            ui.icon("smart_toy").style("color:#10b981;margin-top:2px;")
                            status_lbl = (
                                ui.label("Analyzing request…")
                                .classes("ft-bubble-bot px-4 py-2")
                                .style("color:#cbd5e1;")
                            )

                    await ui.run_javascript(
                        "const el=document.querySelector('.ft-msg-area');"
                        "if(el){el.scrollTop=el.scrollHeight;}"
                    )

                    if not _PIPELINE_AVAILABLE:
                        status_lbl.set_text(
                            "⚠ Orchestrator pipeline is not available (check server logs)."
                        )
                        ui.notify(
                            "Orchestrator pipeline unavailable",
                            type="negative",
                            position="top-right",
                        )
                        _re_enable()
                        return

                    # ── Intake (blocking LLM call — off the event loop) ───
                    try:
                        intent = await asyncio.to_thread(intake_request, raw)
                    except (ValueError, LLMBackendError, LLMResponseError) as exc:  # noqa: BLE001
                        msg = str(exc)[:200]
                        status_lbl.set_text(f"⚠ Intake failed: {msg}")
                        ui.notify(msg, type="negative", position="top-right")
                        _re_enable()
                        return
                    except Exception as exc:  # noqa: BLE001
                        status_lbl.set_text(f"⚠ Unexpected error during intake: {exc}")
                        _re_enable()
                        return

                    # ── Planner (blocking LLM call — off the event loop) ──
                    status_lbl.set_text("Planning…")
                    try:
                        plan = await asyncio.to_thread(plan_task_intent, intent)
                    except (ValueError, LLMBackendError, LLMResponseError) as exc:  # noqa: BLE001
                        msg = str(exc)[:200]
                        status_lbl.set_text(f"⚠ Planning failed: {msg}")
                        ui.notify(msg, type="negative", position="top-right")
                        _re_enable()
                        return
                    except Exception as exc:  # noqa: BLE001
                        status_lbl.set_text(f"⚠ Unexpected error during planning: {exc}")
                        _re_enable()
                        return

                    # ── Persist plan + reveal progress tree ───────────────
                    _plan_store.save(plan)
                    active_plan_id[0] = plan.plan_id
                    plan_tree_section.style(_TREE_STYLE_VISIBLE)
                    _rebuild_plan_tree(plan)

                    # ── Executor (async, driven by the event loop) ────────
                    status_lbl.set_text(f"Executing… ({len(plan.nodes)} node(s))")
                    try:
                        executor = Executor(_plan_store, _tool_registry)
                        final_plan = await executor.run(plan.plan_id)
                        _rebuild_plan_tree(final_plan)
                        if final_plan.status.value == "completed":
                            status_lbl.set_text(
                                f"✓ Done — {len(final_plan.nodes)} node(s) completed."
                            )
                        else:
                            status_lbl.set_text(
                                f"Finished with status: {final_plan.status.value}"
                            )
                    except Exception as exc:  # noqa: BLE001
                        status_lbl.set_text(f"⚠ Execution error: {exc}")
                    finally:
                        _re_enable()
                        await ui.run_javascript(
                            "const el=document.querySelector('.ft-msg-area');"
                            "if(el){el.scrollTop=el.scrollHeight;}"
                        )

                msg_area.classes("ft-msg-area")

                _btn = (
                    ui.button(icon="send", on_click=_send)
                    .props("round unelevated")
                    .style("background:#059669;color:#fff;")
                )
                send_btn_holder.append(_btn)

        # ================================================================== #
        # RIGHT PANEL — Claw Monitor sidebar                                 #
        # ================================================================== #
        with ui.column().classes("h-full").style(
            "width:18rem;background:#0f172a;border-left:1px solid #1e293b;"
            "display:flex;flex-direction:column;overflow-y:auto;"
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
            with ui.column().classes("w-full px-4 py-3 gap-3"):
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

            # ── Divider ───────────────────────────────────────────────────
            ui.element("div").classes("w-full").style(
                "border-top:1px solid #1e293b;margin:0.25rem 0;"
            )

            # ============================================================= #
            # AGENT TASKS — orchestrator integration                         #
            # ============================================================= #

            with ui.row().classes("w-full px-4 py-2 items-center gap-2"):
                ui.icon("hub").style("color:#10b981;font-size:1.3rem;")
                ui.label("Agent Tasks").classes("text-lg font-semibold").style(
                    "color:#10b981;"
                )

            # -- Active claws list ----------------------------------------
            claw_list = ui.column().classes("w-full px-4 gap-1")
            with claw_list:
                if _orchestrator is None:
                    ui.label("Docker not connected").classes(
                        "text-xs italic"
                    ).style("color:#ef4444;")
                else:
                    ui.label("No active claws").classes(
                        "text-xs italic"
                    ).style("color:#475569;")

            # -- Live log viewer ------------------------------------------
            with ui.column().classes("w-full px-4 py-2 gap-1"):
                ui.label("Live Logs").classes("text-xs font-mono").style(
                    "color:#94a3b8;"
                )
                log_el = (
                    ui.log(max_lines=200)
                    .classes("w-full")
                    .style(
                        "height:10rem;background:#0f172a;color:#94a3b8;"
                        "font-size:0.7rem;font-family:monospace;"
                        "border:1px solid #1e293b;border-radius:0.5rem;"
                    )
                )

            # -- Spawn Claw dialog ----------------------------------------
            def _open_spawn_dialog() -> None:
                with ui.dialog() as dlg, ui.card().style(
                    "background:#1e293b;color:#f1f5f9;min-width:22rem;"
                ):
                    ui.label("Spawn New Claw").classes(
                        "text-lg font-bold"
                    ).style("color:#10b981;")

                    tid_input = ui.input(
                        "Task ID",
                        value=uuid.uuid4().hex[:8],
                    ).classes("w-full").style("color:#f1f5f9;")

                    desc_input = ui.textarea(
                        "Python code to execute",
                        placeholder="print('Hello from the claw!')",
                    ).classes("w-full").style("color:#f1f5f9;")

                    async def _do_spawn() -> None:
                        if _orchestrator is None:
                            ui.notify(
                                "Docker is not available",
                                type="negative",
                                position="top-right",
                            )
                            dlg.close()
                            return

                        tid = (tid_input.value or "").strip()
                        desc = (desc_input.value or "").strip()
                        if not tid or not desc:
                            ui.notify(
                                "Task ID and code are required",
                                type="warning",
                                position="top-right",
                            )
                            return

                        try:
                            short_id = await asyncio.to_thread(
                                _orchestrator.spawn_claw, tid, desc
                            )
                            ui.notify(
                                f"Spawned claw {short_id}",
                                type="positive",
                                position="top-right",
                            )
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(
                                f"Spawn failed: {exc}",
                                type="negative",
                                position="top-right",
                            )
                        dlg.close()

                    with ui.row().classes("w-full justify-end gap-2 mt-3"):
                        ui.button("Cancel", on_click=dlg.close).props(
                            "flat"
                        ).style("color:#94a3b8;")
                        ui.button(
                            "Spawn", icon="rocket_launch", on_click=_do_spawn,
                        ).style("background:#059669;color:#fff;")

                dlg.open()

            with ui.row().classes("w-full px-4 py-2"):
                ui.button(
                    "Spawn Claw",
                    icon="add_circle",
                    on_click=_open_spawn_dialog,
                ).props("flat dense").style("color:#10b981;")

            # -- Kill Switch ──────────────────────────────────────────────
            async def _kill_all() -> None:
                if _orchestrator is None:
                    ui.notify(
                        "Docker is not available",
                        type="negative",
                        position="top-right",
                    )
                    return
                count = await asyncio.to_thread(_orchestrator.kill_all)
                log_el.clear()
                ui.notify(
                    f"🛑 Kill Switch: stopped {count} container(s)",
                    type="warning",
                    position="top-right",
                )

            with ui.row().classes("w-full px-4 pb-4"):
                ui.button(
                    "⛔ KILL ALL CLAWS",
                    icon="dangerous",
                    on_click=_kill_all,
                ).classes("w-full").style(
                    "background:#7f1d1d;color:#fecaca;font-weight:bold;"
                    "border:1px solid #ef4444;border-radius:0.5rem;"
                )

            # ── Divider ───────────────────────────────────────────────────
            ui.element("div").classes("w-full").style(
                "border-top:1px solid #1e293b;margin:0.25rem 0;"
            )

            # ============================================================= #
            # BROWSER CLAW — spawn headless Chromium + screenshot gallery   #
            # ============================================================= #

            with ui.row().classes("w-full px-4 py-2 items-center gap-2"):
                ui.icon("travel_explore").style("color:#10b981;font-size:1.3rem;")
                ui.label("Browser Claw").classes("text-lg font-semibold").style(
                    "color:#10b981;"
                )

            # -- Spawn Browser Claw dialog --------------------------------
            def _open_browser_spawn_dialog() -> None:
                with ui.dialog() as dlg, ui.card().style(
                    "background:#1e293b;color:#f1f5f9;min-width:22rem;"
                ):
                    ui.label("Spawn Browser Claw").classes(
                        "text-lg font-bold"
                    ).style("color:#10b981;")

                    ui.label(
                        "Starts a headless Chromium container. "
                        "Screenshots are saved to your workspace."
                    ).classes("text-xs").style("color:#94a3b8;")

                    btid_input = ui.input(
                        "Task ID",
                        value=f"browser-{uuid.uuid4().hex[:6]}",
                    ).classes("w-full").style("color:#f1f5f9;")

                    async def _do_spawn_browser() -> None:
                        if _orchestrator is None:
                            ui.notify(
                                "Docker is not available",
                                type="negative",
                                position="top-right",
                            )
                            dlg.close()
                            return

                        tid = (btid_input.value or "").strip()
                        if not tid:
                            ui.notify(
                                "Task ID is required",
                                type="warning",
                                position="top-right",
                            )
                            return

                        shots_path = str(_SCREENSHOTS_DIR)
                        try:
                            short_id = await asyncio.to_thread(
                                _orchestrator.spawn_browser_claw, tid, shots_path
                            )
                            ui.notify(
                                f"Browser claw ready — {short_id}",
                                type="positive",
                                position="top-right",
                            )
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(
                                f"Browser claw spawn failed: {exc}",
                                type="negative",
                                position="top-right",
                            )
                        dlg.close()

                    with ui.row().classes("w-full justify-end gap-2 mt-3"):
                        ui.button("Cancel", on_click=dlg.close).props(
                            "flat"
                        ).style("color:#94a3b8;")
                        ui.button(
                            "Launch",
                            icon="rocket_launch",
                            on_click=_do_spawn_browser,
                        ).style("background:#059669;color:#fff;")

                dlg.open()

            # -- Send command to browser claw dialog ----------------------
            def _open_browser_cmd_dialog() -> None:
                with ui.dialog() as dlg, ui.card().style(
                    "background:#1e293b;color:#f1f5f9;min-width:24rem;"
                ):
                    ui.label("Send Browser Command").classes(
                        "text-lg font-bold"
                    ).style("color:#10b981;")

                    ui.label(
                        'JSON command — e.g. {"cmd":"navigate","url":"https://youtube.com"}'
                    ).classes("text-xs").style("color:#94a3b8;")

                    bcmd_tid = ui.input(
                        "Task ID of running browser claw",
                    ).classes("w-full").style("color:#f1f5f9;")

                    bcmd_json = ui.textarea(
                        "Command JSON",
                        placeholder='{"cmd": "screenshot"}',
                    ).classes("w-full").style("color:#f1f5f9;")

                    result_lbl = ui.label("").classes("text-xs font-mono w-full").style(
                        "color:#34d399;word-break:break-all;"
                    )

                    async def _do_send_cmd() -> None:
                        if _orchestrator is None:
                            result_lbl.set_text("Docker is not available")
                            return
                        tid = (bcmd_tid.value or "").strip()
                        raw = (bcmd_json.value or "").strip()
                        if not tid or not raw:
                            result_lbl.set_text("Task ID and command JSON are required")
                            return
                        try:
                            cmd_obj = _json.loads(raw)
                        except Exception as exc:  # noqa: BLE001
                            result_lbl.set_text(f"Invalid JSON: {exc}")
                            return
                        try:
                            response = await asyncio.to_thread(
                                _orchestrator.send_browser_command, tid, cmd_obj
                            )
                            result_lbl.set_text(str(response))
                        except Exception as exc:  # noqa: BLE001
                            result_lbl.set_text(f"Error: {exc}")

                    with ui.row().classes("w-full justify-end gap-2 mt-3"):
                        ui.button("Close", on_click=dlg.close).props(
                            "flat"
                        ).style("color:#94a3b8;")
                        ui.button(
                            "Send", icon="send", on_click=_do_send_cmd,
                        ).style("background:#059669;color:#fff;")

                dlg.open()

            with ui.row().classes("w-full px-4 gap-2"):
                ui.button(
                    "Launch Browser",
                    icon="add_circle",
                    on_click=_open_browser_spawn_dialog,
                ).props("flat dense").style("color:#10b981;")
                ui.button(
                    "Send Command",
                    icon="terminal",
                    on_click=_open_browser_cmd_dialog,
                ).props("flat dense").style("color:#10b981;")

            # -- Screenshots gallery --------------------------------------
            with ui.column().classes("w-full px-4 py-2 gap-1"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Screenshots").classes("text-xs font-mono").style(
                        "color:#94a3b8;"
                    )
                    screenshot_count_lbl = ui.label("0 files").classes(
                        "text-xs font-mono"
                    ).style("color:#475569;")

                screenshot_gallery = ui.column().classes("w-full gap-2")

            def _refresh_screenshots() -> None:
                all_pngs = list(_SCREENSHOTS_DIR.glob("*.png"))
                # Use heapq.nlargest to avoid sorting the full list when only
                # the 6 most-recent files are needed.
                recent_pngs = heapq.nlargest(
                    6, all_pngs, key=lambda p: p.stat().st_mtime
                )

                screenshot_count_lbl.set_text(f"{len(all_pngs)} files")

                screenshot_gallery.clear()
                with screenshot_gallery:
                    if not recent_pngs:
                        ui.label("No screenshots yet").classes(
                            "text-xs italic"
                        ).style("color:#475569;")
                    else:
                        for png in recent_pngs:
                            with ui.column().classes("w-full gap-0"):
                                ui.image(f"/screenshots/{png.name}").classes(
                                    "w-full rounded"
                                ).style(
                                    "border:1px solid #334155;"
                                    "border-radius:0.375rem;"
                                )
                                ui.label(png.name).classes(
                                    "text-xs font-mono truncate w-full"
                                ).style("color:#64748b;")

            ui.timer(2.0, _refresh_screenshots)

            # -- Periodic claw status + log drain -------------------------
            def _tick_claws() -> None:
                if _orchestrator is None:
                    return

                claws = _orchestrator.list_claws()

                # Rebuild the container list widget
                claw_list.clear()
                with claw_list:
                    if not claws:
                        ui.label("No active claws").classes(
                            "text-xs italic"
                        ).style("color:#475569;")
                    else:
                        for claw in claws:
                            status = claw["status"]
                            colour = {
                                "running": "#10b981",
                                "exited": "#f59e0b",
                                "created": "#3b82f6",
                                "removed": "#ef4444",
                            }.get(status, "#94a3b8")
                            with ui.row().classes(
                                "w-full items-center gap-2"
                            ):
                                ui.icon("circle").style(
                                    f"color:{colour};font-size:0.5rem;"
                                )
                                ui.label(claw["task_id"]).classes(
                                    "text-xs font-mono"
                                ).style("color:#e2e8f0;")
                                ui.label(status).classes(
                                    "text-xs font-mono"
                                ).style(
                                    f"color:{colour};margin-left:auto;"
                                )

                # Drain pending log lines into the viewer
                for claw in claws:
                    for line in _orchestrator.drain_logs(claw["task_id"]):
                        log_el.push(f"[{claw['task_id']}] {line}")

            ui.timer(1.0, _tick_claws)

            # -- Plan progress tree polling --------------------------------
            def _tick_plan() -> None:
                """Poll the plan store and refresh the DAG progress tree."""
                pid = active_plan_id[0]
                if pid is None or _plan_store is None:
                    return
                plan = _plan_store.load(pid)
                if plan is None:
                    return
                _rebuild_plan_tree(plan)

            ui.timer(0.75, _tick_plan)

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
        host=UI_HOST,
        port=UI_PORT,
        dark=True,
        favicon="🦅",
        reload=False,
        storage_secret="freetalon-local-only",
    )

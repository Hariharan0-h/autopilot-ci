"""
dashboard/app.py — Gradio live dashboard for AutoPilot CI.

Components:
  1. "Trigger demo run" button → POST to http://localhost:8001/webhook
  2. Agent status grid — 7 cards showing status + last message per agent
  3. Live event log — scrolling textbox, auto-refresh every 2s
  4. Final report accordion — visible after run completes
  5. AMD GPU utilization — reads rocm-smi every 3s
"""
from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path

import httpx
import gradio as gr

_BASE = os.getenv("WEBHOOK_HOST", "http://localhost:8001")
WEBHOOK_URL = f"{_BASE}/webhook"
STATUS_BASE = f"{_BASE}/status"
EVENTS_BASE = f"{_BASE}/events"
RUNS_URL    = f"{_BASE}/runs"

AGENT_NAMES = [
    "code_analyzer",
    "test_generator",
    "security_scanner",
    "perf_analyzer",
    "supervisor",
    "autofix",
    "deployment",
]

# Module-level state
_current_run_id: str = ""
_event_log: list[str] = []
_agent_state: dict[str, dict] = {
    name: {"status": "waiting", "message": "—"} for name in AGENT_NAMES
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _status_badge(status: str) -> str:
    """Return a colored emoji badge for an agent status string."""
    return {
        "waiting": "⚪ Waiting",
        "running": "🔵 Running",
        "done": "✅ Done",
        "failed": "🔴 Failed",
    }.get(status, f"❓ {status}")


def _get_gpu_util() -> str:
    """Read AMD GPU utilization via rocm-smi. Returns 'N/A (mock mode)' if unavailable."""
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "GPU use" in line or "%" in line:
                    return line.strip()
        return "N/A (rocm-smi unavailable)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "N/A (mock mode)"


def _fetch_run_status(run_id: str) -> dict | None:
    """Fetch pipeline run status from the webhook server."""
    if not run_id:
        return None
    try:
        resp = httpx.get(f"{STATUS_BASE}/{run_id}", timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _update_agent_state_from_run(run_data: dict) -> None:
    """Update module-level _agent_state from a PipelineRun dict."""
    status_map = {
        "code_result": "code_analyzer",
        "test_result": "test_generator",
        "security_result": "security_scanner",
        "perf_result": "perf_analyzer",
        "supervisor_decision": "supervisor",
        "autofix_result": "autofix",
        "deployment_result": "deployment",
    }
    for field, agent in status_map.items():
        result = run_data.get(field)
        if result:
            # SupervisorDecision has no status field — infer from presence
            agent_status = result.get("status", "done" if agent == "supervisor" else "waiting")
            # Build a summary message
            if agent == "supervisor":
                action = result.get("action", "")
                msg = f"{action} — {result.get('summary', 'Decision made.')[:100]}"
            elif agent == "autofix":
                msg = f"{result.get('fixes_applied', 0)} fix(es) applied."
            elif agent == "deployment":
                msg = f"Strategy: {result.get('strategy', '—')}"
            elif agent == "security_scanner":
                msg = (
                    f"bandit_score={result.get('bandit_score', 0)}, "
                    f"{len(result.get('cve_hits', []))} CVE(s)"
                )
            else:
                count = len(result.get("findings", []))
                msg = f"{count} finding(s)"
            _agent_state[agent] = {"status": agent_status, "message": msg}
        else:
            _agent_state[agent] = {"status": run_data.get("status", "waiting"), "message": "—"}


# ─── Gradio callbacks ──────────────────────────────────────────────────────────

def trigger_demo_run():
    """POST to webhook with demo/sample_repo path. Returns status message."""
    global _current_run_id, _event_log, _agent_state

    demo_repo = str(Path(__file__).parent.parent / "demo" / "sample_repo")

    # Reset state
    _event_log = []
    _agent_state = {name: {"status": "waiting", "message": "—"} for name in AGENT_NAMES}

    try:
        resp = httpx.post(WEBHOOK_URL, json={
            "repo_path": demo_repo,
            "base": "HEAD~1",
            "head": "HEAD",
            "branch": "main",
            "repo_url": "",
        }, timeout=10)
        if resp.status_code == 202:
            data = resp.json()
            _current_run_id = data["run_id"]
            return f"✅ Pipeline started — run_id: {_current_run_id[:8]}..."
        else:
            return f"❌ Webhook returned {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return f"❌ Could not reach webhook server: {e}\nMake sure `uvicorn server.webhook:app --port 8001` is running."


def refresh_dashboard():
    """Poll run status and return updated values for all dashboard components."""
    global _event_log

    run_data = _fetch_run_status(_current_run_id)
    if run_data:
        _update_agent_state_from_run(run_data)

    # Build agent grid text
    grid_lines = []
    for name in AGENT_NAMES:
        state = _agent_state[name]
        badge = _status_badge(state["status"])
        grid_lines.append(f"**{name}**  {badge}\n{state['message']}")
    agent_grid_text = "\n\n".join(grid_lines)

    # Build event log from /events/{run_id}
    if _current_run_id:
        try:
            resp = httpx.get(f"{EVENTS_BASE}/{_current_run_id}", timeout=3)
            if resp.status_code == 200:
                events = resp.json()
                _event_log = [
                    f"[{e['timestamp'][11:19]}] [{e['agent']}] {e['status'].upper()} — {e['message']}"
                    for e in events
                ]
        except Exception:
            pass

    log_text = "\n".join(_event_log[-50:])  # Last 50 lines

    # GPU utilization
    gpu_text = _get_gpu_util()

    # Final report (visible when done)
    report_visible = False
    report_text = ""
    if run_data and run_data.get("status") == "done":
        report_visible = True
        report_text = _build_final_report(run_data)

    return (
        agent_grid_text,
        log_text,
        gpu_text,
        gr.update(visible=report_visible),   # accordion visibility only
        report_text,                          # markdown content
    )


def _build_final_report(run_data: dict) -> str:
    """Build a markdown final report string from a completed PipelineRun dict."""
    sections = []

    # Code findings
    code = run_data.get("code_result") or {}
    findings = code.get("findings", [])
    if findings:
        lines = [f"### 🔍 Code Findings ({len(findings)})"]
        for f in findings[:5]:
            lines.append(f"- `{f['file']}:{f['line']}` [{f['severity']}] {f['message'][:100]}")
        sections.append("\n".join(lines))

    # Security findings
    sec = run_data.get("security_result") or {}
    sec_findings = sec.get("findings", [])
    cve_hits = sec.get("cve_hits", [])
    if sec_findings or cve_hits:
        lines = [f"### 🔐 Security Findings ({len(sec_findings)})"]
        for f in sec_findings[:5]:
            lines.append(f"- `{f['file']}:{f['line']}` [{f['severity']}] {f['message'][:100]}")
        for cve in cve_hits:
            lines.append(f"- ⚠️ CVE: {cve}")
        sections.append("\n".join(lines))

    # Perf findings
    perf = run_data.get("perf_result") or {}
    perf_findings = perf.get("findings", [])
    if perf_findings:
        lines = [f"### ⚡ Performance Findings ({len(perf_findings)})"]
        for f in perf_findings[:5]:
            lines.append(f"- `{f['file']}:{f['line']}` {f['message'][:100]}")
        sections.append("\n".join(lines))

    # Generated tests
    test = run_data.get("test_result") or {}
    test_files = test.get("generated_test_files", [])
    if test_files:
        lines = [f"### 🧪 Generated Tests ({test.get('tests_written', 0)} function(s) covered)"]
        for fp in test_files:
            lines.append(f"- `{fp}`")
        sections.append("\n".join(lines))

    # Auto-fix PR
    fix = run_data.get("autofix_result") or {}
    if fix:
        lines = [f"### 🔧 Auto-Fix PR"]
        lines.append(f"- Fixes applied: **{fix.get('fixes_applied', 0)}**")
        if fix.get("pr_url"):
            lines.append(f"- PR: [{fix['pr_url']}]({fix['pr_url']})")
        elif fix.get("branch_name"):
            lines.append(f"- Branch: `{fix['branch_name']}`")
        sections.append("\n".join(lines))

    # Supervisor decision
    sup = run_data.get("supervisor_decision") or {}
    if sup:
        sections.insert(0,
            f"### 🤖 Supervisor Decision: **{sup.get('action', '—')}**\n"
            f"{sup.get('summary', '')}"
        )

    return "\n\n---\n\n".join(sections) if sections else "No findings. All clear! ✅"


# ─── Custom CSS (AMD theme) ────────────────────────────────────────────────────

_CSS = """
/* ── Global ── */
body, .gradio-container {
    background: #0d0d0d !important;
    color: #f0f0f0 !important;
    font-family: 'Inter', 'Segoe UI', sans-serif !important;
}

/* ── Header banner ── */
#header-banner {
    background: linear-gradient(135deg, #1a0000 0%, #2d0000 50%, #1a0000 100%);
    border: 1px solid #E31837;
    border-radius: 12px;
    padding: 20px 28px;
    margin-bottom: 4px;
}
#header-banner h1 { color: #E31837 !important; font-size: 2rem !important; margin: 0 0 4px 0 !important; }
#header-banner p  { color: #aaa !important; margin: 0 !important; font-size: 0.95rem !important; }

/* ── Trigger button ── */
#trigger-btn {
    background: #E31837 !important;
    border: none !important;
    color: #fff !important;
    font-weight: 700 !important;
    font-size: 1rem !important;
    border-radius: 8px !important;
    padding: 12px 28px !important;
    transition: background 0.2s !important;
}
#trigger-btn:hover { background: #b0122a !important; }

/* ── Status box ── */
#status-box textarea {
    background: #1a1a1a !important;
    border: 1px solid #333 !important;
    color: #4ade80 !important;
    font-family: monospace !important;
    font-size: 0.85rem !important;
    border-radius: 8px !important;
}

/* ── Section headers ── */
.section-header {
    color: #E31837 !important;
    font-size: 0.75rem !important;
    font-weight: 700 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.1em !important;
    border-bottom: 1px solid #2a2a2a !important;
    padding-bottom: 6px !important;
    margin-bottom: 12px !important;
}

/* ── Agent grid markdown ── */
#agent-grid {
    background: #111 !important;
    border: 1px solid #222 !important;
    border-radius: 10px !important;
    padding: 14px !important;
    min-height: 340px !important;
}
#agent-grid p    { margin: 0 0 10px 0 !important; line-height: 1.6 !important; }
#agent-grid strong { color: #e0e0e0 !important; }

/* ── Event log ── */
#event-log textarea {
    background: #0a0a0a !important;
    border: 1px solid #222 !important;
    color: #7dd3a8 !important;
    font-family: 'Cascadia Code', 'Consolas', monospace !important;
    font-size: 0.78rem !important;
    border-radius: 10px !important;
    line-height: 1.6 !important;
}

/* ── GPU box ── */
#gpu-box {
    background: #111 !important;
    border: 1px solid #E31837 !important;
    border-radius: 8px !important;
}
#gpu-box textarea {
    background: transparent !important;
    color: #E31837 !important;
    font-family: monospace !important;
    font-weight: 600 !important;
}
#gpu-box label { color: #E31837 !important; font-weight: 700 !important; }

/* ── Final report accordion ── */
#report-accordion {
    background: #111 !important;
    border: 1px solid #333 !important;
    border-radius: 10px !important;
    margin-top: 8px !important;
}
#report-accordion .label-wrap { color: #E31837 !important; font-weight: 700 !important; }

/* ── Accordion inner markdown ── */
#report-md { padding: 12px !important; }
#report-md h3 { color: #E31837 !important; }
#report-md code { background: #1e1e1e !important; color: #7dd3a8 !important; }

/* ── Hide Gradio footer ── */
footer { display: none !important; }
"""

_HEADER_HTML = """
<div id="header-banner">
  <h1>🚀 AutoPilot CI</h1>
  <p>Multi-agent CI/CD system &nbsp;·&nbsp; AMD MI300X + vLLM &nbsp;·&nbsp;
     4 LLMs running in parallel &nbsp;·&nbsp; AMD Hackathon 2026</p>
</div>
"""


# ─── Gradio app ────────────────────────────────────────────────────────────────

def build_app() -> gr.Blocks:
    """Build and return the Gradio Blocks app."""
    with gr.Blocks(
        title="AutoPilot CI — AMD MI300X",
        theme=gr.themes.Base(
            primary_hue="red",
            neutral_hue="slate",
        ),
        css=_CSS,
    ) as demo:

        gr.HTML(_HEADER_HTML)

        with gr.Row():
            trigger_btn = gr.Button(
                "▶  Trigger Demo Run",
                variant="primary",
                scale=1,
                elem_id="trigger-btn",
            )
            trigger_status = gr.Textbox(
                label="Pipeline Status",
                scale=3,
                interactive=False,
                elem_id="status-box",
            )

        with gr.Row(equal_height=True):
            with gr.Column(scale=2):
                gr.HTML('<div class="section-header">🤖 Agent Status</div>')
                agent_grid = gr.Markdown(
                    "\n\n".join(
                        f"**{name}**  ⚪ Waiting\n—" for name in AGENT_NAMES
                    ),
                    elem_id="agent-grid",
                )

            with gr.Column(scale=3):
                gr.HTML('<div class="section-header">📋 Live Event Log</div>')
                event_log_box = gr.Textbox(
                    label="",
                    lines=16,
                    max_lines=16,
                    interactive=False,
                    placeholder="── waiting for pipeline events ──",
                    elem_id="event-log",
                )

        with gr.Row():
            gpu_box = gr.Textbox(
                label="⬛ AMD GPU Utilization  (rocm-smi --showuse)",
                value="N/A (mock mode — set mock: false + AMD MI300X for live data)",
                interactive=False,
                elem_id="gpu-box",
            )

        with gr.Accordion(
            "📊  Final Report",
            open=False,
            visible=False,
            elem_id="report-accordion",
        ) as report_accordion:
            report_md = gr.Markdown("", elem_id="report-md")

        # ── Callbacks ──
        trigger_btn.click(fn=trigger_demo_run, outputs=trigger_status)

        demo.load(
            fn=refresh_dashboard,
            outputs=[agent_grid, event_log_box, gpu_box, report_accordion, report_md],
            every=2,
        )

    return demo


if __name__ == "__main__":
    app = build_app()
    app.launch(server_name="0.0.0.0", server_port=7860, share=False)

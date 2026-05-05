"""
agents/report_generator.py — AutoPilot CI PDF report generator.

Redesigned v2: full cover banner, accent stat cards, amber-stripe section
headers, coloured decision badge, timeline deployment log.
"""
from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from schemas import AgentStatus, PipelineAction, PipelineRun

# ── Palette ────────────────────────────────────────────────────────────────────
_NAVY    = colors.HexColor("#0d1117")
_NAVY_L  = colors.HexColor("#161b22")
_NAVY_2  = colors.HexColor("#21262d")
_AMBER   = colors.HexColor("#9a6700")
_AMBER_B = colors.HexColor("#d4a853")   # brighter amber for cover
_AMBER_L = colors.HexColor("#fff8e1")
_JADE    = colors.HexColor("#1a7f37")
_JADE_L  = colors.HexColor("#dcffe4")
_ROSE    = colors.HexColor("#cf222e")
_ROSE_L  = colors.HexColor("#ffd7d9")
_AZURE   = colors.HexColor("#0550ae")
_AZURE_L = colors.HexColor("#ddf4ff")
_INK     = colors.HexColor("#24292f")
_INK2    = colors.HexColor("#57606a")
_WHITE   = colors.white
_LIGHT   = colors.HexColor("#f6f8fa")
_BORDER  = colors.HexColor("#d0d7de")

_SEV_FG = {"critical": _ROSE, "high": _ROSE, "medium": _AMBER, "low": _AZURE, "info": _INK2}
_SEV_BG = {"critical": _ROSE_L, "high": _ROSE_L, "medium": _AMBER_L, "low": _AZURE_L, "info": _LIGHT}

PAGE_W, PAGE_H = A4
L_MARGIN = 18 * mm
R_MARGIN = 18 * mm
T_MARGIN = 22 * mm
B_MARGIN = 18 * mm
BODY_W   = PAGE_W - L_MARGIN - R_MARGIN

# ── Styles ─────────────────────────────────────────────────────────────────────
def _s(name, **kw): return ParagraphStyle(name, **kw)

COVER_TITLE  = _s("ct",  fontSize=32, leading=38, fontName="Helvetica-Bold",
                   textColor=_WHITE, alignment=TA_LEFT)
COVER_ACCENT = _s("ca",  fontSize=32, leading=38, fontName="Helvetica-Bold",
                   textColor=_AMBER_B, alignment=TA_LEFT)
COVER_SUB    = _s("cs",  fontSize=11, leading=15, fontName="Helvetica",
                   textColor=colors.HexColor("#8b949e"), alignment=TA_LEFT)
KV_KEY       = _s("kk",  fontSize=9,  leading=14, fontName="Helvetica-Bold",
                   textColor=_INK2)
KV_VAL       = _s("kv",  fontSize=9,  leading=14, fontName="Helvetica",
                   textColor=_INK)
BODY         = _s("b",   fontSize=9,  leading=14, fontName="Helvetica",
                   textColor=_INK, spaceAfter=3)
SMALL        = _s("sm",  fontSize=8,  leading=12, fontName="Helvetica",
                   textColor=_INK2)
MONO         = _s("mo",  fontSize=7.5,leading=11, fontName="Courier",
                   textColor=_INK)
MONO_LIGHT   = _s("mol", fontSize=7.5,leading=11, fontName="Courier",
                   textColor=_INK2)
H3           = _s("h3",  fontSize=10, leading=14, fontName="Helvetica-Bold",
                   textColor=_NAVY, spaceBefore=6, spaceAfter=4)
SECTION_TXT  = _s("st",  fontSize=12, leading=16, fontName="Helvetica-Bold",
                   textColor=_WHITE)
TH           = _s("th",  fontSize=8,  leading=11, fontName="Helvetica-Bold",
                   textColor=_WHITE)
TD           = _s("td",  fontSize=8,  leading=11, fontName="Helvetica",
                   textColor=_INK)
TD_MONO      = _s("tdm", fontSize=7.5,leading=11, fontName="Courier",
                   textColor=_INK)
BADGE_TXT    = _s("bt",  fontSize=7,  leading=9,  fontName="Helvetica-Bold",
                   alignment=TA_CENTER)
FOOTER_TXT   = _s("ft",  fontSize=7.5,leading=10, fontName="Helvetica",
                   textColor=_INK2)


# ── Page template with header/footer ──────────────────────────────────────────

class _ReportDoc(BaseDocTemplate):
    def __init__(self, buf, run, **kw):
        super().__init__(buf, **kw)
        self._run = run
        frame = Frame(L_MARGIN, B_MARGIN + 8*mm,
                      BODY_W, PAGE_H - T_MARGIN - B_MARGIN - 8*mm,
                      leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="main", frames=[frame],
                                             onPage=self._draw_chrome)])

    def _draw_chrome(self, canvas, doc):
        canvas.saveState()
        # Cover banner on page 1 only
        if doc.page == 1:
            _draw_cover_banner(canvas, self._run)
        # Top hairline (skip on page 1 — banner covers that area)
        if doc.page > 1:
            canvas.setStrokeColor(_BORDER)
            canvas.setLineWidth(0.5)
            canvas.line(L_MARGIN, PAGE_H - T_MARGIN + 4*mm,
                        PAGE_W - R_MARGIN, PAGE_H - T_MARGIN + 4*mm)
        # Bottom footer bar
        canvas.setFillColor(_NAVY_L)
        canvas.rect(0, 0, PAGE_W, B_MARGIN - 2*mm, fill=1, stroke=0)
        canvas.setFillColor(_INK2)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(L_MARGIN, 5*mm, "AutoPilot CI  ·  Pipeline Scan Report")
        canvas.drawRightString(PAGE_W - R_MARGIN, 5*mm,
                               f"Page {doc.page}  ·  Run {doc._run.run_id[:8]}")
        canvas.restoreState()


# ── Cover banner (drawn directly on canvas, not in story) ─────────────────────

def _draw_cover_banner(canvas, run):
    """Paint the full-width dark cover banner onto page 1."""
    banner_h = 68 * mm
    y0 = PAGE_H - banner_h

    # Background
    canvas.setFillColor(_NAVY)
    canvas.rect(0, y0, PAGE_W, banner_h, fill=1, stroke=0)

    # Amber left accent stripe
    canvas.setFillColor(_AMBER_B)
    canvas.rect(0, y0, 4, banner_h, fill=1, stroke=0)

    # Brand: "Auto" white + "Pilot" amber + " CI" white
    x = L_MARGIN + 6
    y_brand = y0 + 36 * mm
    canvas.setFont("Helvetica-Bold", 34)
    canvas.setFillColor(_WHITE)
    canvas.drawString(x, y_brand, "Auto")
    w_auto = canvas.stringWidth("Auto", "Helvetica-Bold", 34)
    canvas.setFillColor(_AMBER_B)
    canvas.drawString(x + w_auto, y_brand, "Pilot")
    w_pilot = canvas.stringWidth("Pilot", "Helvetica-Bold", 34)
    canvas.setFillColor(_WHITE)
    canvas.drawString(x + w_auto + w_pilot, y_brand, " CI")

    # Sub-title
    canvas.setFont("Helvetica", 11)
    canvas.setFillColor(colors.HexColor("#8b949e"))
    canvas.drawString(x, y_brand - 16, "Multi-Agent Pipeline  ·  Scan Report")

    # Status chip (top-right of banner)
    status = (run.status.value if run.status else "unknown").upper()
    chip_color = _JADE if run.status == AgentStatus.DONE else _ROSE
    chip_x = PAGE_W - R_MARGIN - 6 - 28 * mm
    chip_y = y0 + banner_h - 14 * mm
    canvas.setFillColor(chip_color)
    canvas.roundRect(chip_x, chip_y, 28*mm, 7*mm, 3, fill=1, stroke=0)
    canvas.setFont("Helvetica-Bold", 8.5)
    canvas.setFillColor(_WHITE)
    canvas.drawCentredString(chip_x + 14*mm, chip_y + 2*mm, status)

    # Thin separator line at bottom of banner
    canvas.setStrokeColor(_AMBER_B)
    canvas.setLineWidth(1.5)
    canvas.line(0, y0, PAGE_W, y0)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hr(): return HRFlowable(width="100%", thickness=0.5, color=_BORDER,
                              spaceAfter=5, spaceBefore=3)


def _section(title: str, color=_AMBER_B) -> Table:
    """Section header: dark bar with left amber stripe."""
    inner = Table(
        [[Paragraph(title, SECTION_TXT)]],
        colWidths=[BODY_W - 5],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _NAVY_L),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 12),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))
    # Wrap with amber left stripe
    outer = Table([[_amber_stripe(5), inner]], colWidths=[5, BODY_W - 5])
    outer.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return outer


def _amber_stripe(w: int) -> Table:
    t = Table([[""]], colWidths=[w])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _AMBER_B),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    return t


def _stat_card(label: str, value: str, accent: colors.Color) -> Table:
    val_s = ParagraphStyle("v", fontSize=22, leading=26, fontName="Helvetica-Bold",
                            textColor=accent, alignment=TA_CENTER)
    lbl_s = ParagraphStyle("l", fontSize=7.5, leading=10, fontName="Helvetica",
                            textColor=_INK2, alignment=TA_CENTER,
                            textTransform="uppercase", letterSpacing=0.5)
    card = Table(
        [[Paragraph(value, val_s)], [Paragraph(label, lbl_s)]],
        colWidths=[36 * mm],
    )
    card.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _WHITE),
        ("BOX",           (0, 0), (-1, -1), 0.8, _BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]))
    # Top accent bar via outer wrapper
    accent_bar = Table([[""]], colWidths=[36*mm])
    accent_bar.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), accent),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("ROWHEIGHT",     (0, 0), (-1, -1), 3),
    ]))
    wrapper = Table([[accent_bar], [card]], colWidths=[36*mm])
    wrapper.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return wrapper


def _sev_pill(sev: str) -> Paragraph:
    fg = _SEV_FG.get(sev, _INK2)
    style = ParagraphStyle("sp", fontSize=7, leading=9, fontName="Helvetica-Bold",
                            textColor=fg, alignment=TA_CENTER)
    return Paragraph(sev.upper(), style)


def _findings_table(findings) -> Table | None:
    if not findings:
        return None
    rows = [[
        Paragraph("FILE", TH), Paragraph("LINE", TH),
        Paragraph("SEV", TH), Paragraph("CATEGORY", TH),
        Paragraph("MESSAGE", TH),
    ]]
    for f in findings:
        rows.append([
            Paragraph(f.file, TD_MONO),
            Paragraph(str(f.line), TD),
            _sev_pill(f.severity.value if hasattr(f.severity, "value") else str(f.severity)),
            Paragraph(f.category, TD),
            Paragraph(f.message[:180], TD),
        ])
    cw = [44*mm, 12*mm, 16*mm, 28*mm, None]
    t = Table(rows, colWidths=cw, repeatRows=1)
    base = [
        ("BACKGROUND",    (0, 0), (-1, 0), _NAVY_2),
        ("TOPPADDING",    (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("TOPPADDING",    (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_WHITE, _LIGHT]),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.4, _BORDER),
        ("BOX",           (0, 0), (-1, -1), 0.8, _BORDER),
    ]
    for i, f in enumerate(findings, 1):
        sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        if sev in ("critical", "high"):
            base.append(("BACKGROUND", (0, i), (-1, i), _ROSE_L))
    t.setStyle(TableStyle(base))
    return t


def _decision_badge(action: str) -> Table:
    cfg = {
        "AUTO_FIX":  (_AMBER,   _AMBER_L,  "Auto-Fix"),
        "DEPLOY":    (_JADE,    _JADE_L,   "Deploy"),
        "ESCALATE":  (_ROSE,    _ROSE_L,   "Escalate"),
        "SKIP":      (_INK2,    _LIGHT,    "Skip"),
    }
    fg, bg, label = cfg.get(action, (_NAVY, _LIGHT, action))
    label_s = ParagraphStyle("dl", fontSize=18, leading=22,
                              fontName="Helvetica-Bold", textColor=fg,
                              alignment=TA_CENTER)
    sub_s   = ParagraphStyle("ds", fontSize=8, leading=11,
                              fontName="Helvetica", textColor=fg,
                              alignment=TA_CENTER)
    t = Table([[Paragraph(action, label_s)], [Paragraph(label, sub_s)]],
              colWidths=[44*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), bg),
        ("BOX",           (0, 0), (-1, -1), 1.2, fg),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
    ]))
    return t


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(k, KV_KEY), Paragraph(v, KV_VAL)] for k, v in rows]
    t = Table(data, colWidths=[36*mm, None])
    t.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.3, _BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _timeline_log(log_lines: list[str]) -> Table:
    rows = []
    for i, line in enumerate(log_lines):
        dot_color = _JADE if "complete" in line.lower() or "passed" in line.lower() \
                    else (_ROSE if "fail" in line.lower() or "error" in line.lower()
                          else _AZURE)
        dot = Table([[""]], colWidths=[4])
        dot.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), dot_color),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("ROWHEIGHT",     (0, 0), (-1, -1), 4),
        ]))
        rows.append([dot, Paragraph(line, MONO)])

    t = Table(rows, colWidths=[8*mm, None])
    t.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [_WHITE, _LIGHT]),
        ("BOX",           (0, 0), (-1, -1), 0.8, _BORDER),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.3, _BORDER),
    ]))
    return t


# ── Main builder ───────────────────────────────────────────────────────────────

def generate_report(run: PipelineRun) -> bytes:
    """Generate and return PDF bytes for *run*."""
    buf = io.BytesIO()

    doc = _ReportDoc(
        buf, run,
        pagesize=A4,
        leftMargin=L_MARGIN, rightMargin=R_MARGIN,
        topMargin=T_MARGIN,  bottomMargin=B_MARGIN + 8*mm,
        title="AutoPilot CI Report",
        author="AutoPilot CI",
    )

    story = []

    # ── Cover page ──────────────────────────────────────────────────────────
    # Space for the painted banner (68 mm)
    story.append(Spacer(1, 68*mm))

    # Metadata key-value block
    repo_name = Path(run.repo_path).name
    started   = run.started_at[:19].replace("T", "  ") if run.started_at else "—"
    finished  = run.completed_at[:19].replace("T", "  ") if run.completed_at else "—"

    story.append(Spacer(1, 5*mm))
    story.append(_kv_table([
        ("Run ID",      run.run_id),
        ("Repository",  str(run.repo_path)),
        ("Diff Range",  f"{run.base_commit}  →  {run.head_commit}"),
        ("Started",     started + " UTC"),
        ("Completed",   finished + " UTC"),
    ]))
    story.append(Spacer(1, 7*mm))

    # Stat cards row
    total_findings = (
        (len(run.code_result.findings)     if run.code_result     else 0) +
        (len(run.security_result.findings) if run.security_result else 0) +
        (len(run.perf_result.findings)     if run.perf_result     else 0)
    )
    cve_count    = len(run.security_result.cve_hits) if run.security_result else 0
    tests_count  = run.test_result.tests_written      if run.test_result     else 0
    fixes_count  = run.autofix_result.fixes_applied   if run.autofix_result  else 0
    decision_val = run.supervisor_decision.action.value if run.supervisor_decision else "—"

    dec_accent = {
        "AUTO_FIX": _AMBER, "DEPLOY": _JADE,
        "ESCALATE": _ROSE,  "SKIP":   _INK2,
    }.get(decision_val, _NAVY)

    cards = [
        _stat_card("Total Findings",  str(total_findings),
                   _ROSE if total_findings else _JADE),
        _stat_card("CVEs Found",      str(cve_count),
                   _ROSE if cve_count else _JADE),
        _stat_card("Tests Generated", str(tests_count),  _AZURE),
        _stat_card("Patches Applied", str(fixes_count),  _AMBER),
        _stat_card("Decision",        decision_val,       dec_accent),
    ]
    gap = (BODY_W - 5 * 36*mm) / 4
    cards_t = Table([cards], colWidths=[36*mm]*5, hAlign="LEFT")
    cards_t.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(cards_t)
    story.append(PageBreak())

    # ── Code Analysis ───────────────────────────────────────────────────────
    story.append(_section("Code Analysis"))
    story.append(Spacer(1, 4*mm))
    cr = run.code_result
    if cr:
        story.append(Paragraph(
            f"<b>{cr.files_analyzed}</b> files analysed  ·  "
            f"<b>{len(cr.findings)}</b> findings  ·  "
            f"<i>{cr.duration_seconds:.1f}s</i>", BODY))
        story.append(Spacer(1, 3*mm))
        tbl = _findings_table(cr.findings)
        story.append(tbl if tbl else Paragraph("No code quality issues found.", SMALL))
    else:
        story.append(Paragraph("Agent did not run.", SMALL))
    story.append(Spacer(1, 6*mm))

    # ── Security Analysis ───────────────────────────────────────────────────
    story.append(_section("Security Analysis"))
    story.append(Spacer(1, 4*mm))
    sr = run.security_result
    if sr:
        score_color = "red" if sr.bandit_score >= 7 else \
                      ("orange" if sr.bandit_score >= 4 else "green")
        story.append(Paragraph(
            f"Bandit score: <b>{sr.bandit_score:.1f} / 10.0</b>  ·  "
            f"CVEs: <b>{len(sr.cve_hits)}</b>  ·  "
            f"Findings: <b>{len(sr.findings)}</b>  ·  "
            f"<i>{sr.duration_seconds:.1f}s</i>", BODY))
        story.append(Spacer(1, 3*mm))
        if sr.cve_hits:
            story.append(Paragraph("CVE Hits", H3))
            cve_rows = [[Paragraph("CVE REFERENCE", TH)]] + \
                       [[Paragraph(c, MONO)] for c in sr.cve_hits]
            ct = Table(cve_rows, colWidths=[None])
            ct.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0), _ROSE),
                ("BACKGROUND",    (0, 1), (-1, -1), _ROSE_L),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                ("BOX",           (0, 0), (-1, -1), 0.8, _ROSE),
                ("LINEBELOW",     (0, 0), (-1, -1), 0.4, _BORDER),
            ]))
            story.append(ct)
            story.append(Spacer(1, 3*mm))
        tbl = _findings_table(sr.findings)
        story.append(tbl if tbl else Paragraph("No security findings.", SMALL))
    else:
        story.append(Paragraph("Agent did not run.", SMALL))
    story.append(Spacer(1, 6*mm))

    # ── Performance Analysis ────────────────────────────────────────────────
    story.append(_section("Performance Analysis"))
    story.append(Spacer(1, 4*mm))
    pr = run.perf_result
    if pr:
        story.append(Paragraph(
            f"<b>{len(pr.findings)}</b> hotspots  ·  "
            f"<i>{pr.duration_seconds:.1f}s</i>", BODY))
        story.append(Spacer(1, 3*mm))
        tbl = _findings_table(pr.findings)
        story.append(tbl if tbl else Paragraph("No performance issues found.", SMALL))
    else:
        story.append(Paragraph("Agent did not run.", SMALL))
    story.append(Spacer(1, 6*mm))

    # ── Test Generation ─────────────────────────────────────────────────────
    story.append(_section("Test Generation"))
    story.append(Spacer(1, 4*mm))
    tr = run.test_result
    if tr:
        story.append(Paragraph(
            f"<b>{tr.tests_written}</b> tests generated  ·  "
            f"<b>{len(tr.generated_test_files)}</b> file(s)  ·  "
            f"<i>{tr.duration_seconds:.1f}s</i>", BODY))
        if tr.generated_test_files:
            story.append(Spacer(1, 2*mm))
            frows = [[Paragraph("GENERATED FILES", TH)]] + \
                    [[Paragraph(f, MONO)] for f in tr.generated_test_files]
            ft = Table(frows, colWidths=[None])
            ft.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0), _NAVY_2),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_WHITE, _LIGHT]),
                ("BOX",           (0, 0), (-1, -1), 0.8, _BORDER),
            ]))
            story.append(ft)
    else:
        story.append(Paragraph("Agent did not run.", SMALL))
    story.append(Spacer(1, 6*mm))

    # ── Docker Build ────────────────────────────────────────────────────────
    story.append(_section("Docker Build"))
    story.append(Spacer(1, 4*mm))
    dr = run.docker_result
    if dr:
        status_label = "SUCCESS" if dr.build_success else "FAILED"
        status_color = _JADE if dr.build_success else _ROSE
        status_bg    = _JADE_L if dr.build_success else _ROSE_L

        # Status chip inline
        story.append(Paragraph(
            f"Status: <b>{status_label}</b>  ·  "
            f"Dockerfiles found: <b>{len(dr.dockerfiles_found)}</b>  ·  "
            f"<i>{dr.duration_seconds:.1f}s</i>", BODY))

        if dr.dockerfiles_found:
            story.append(Spacer(1, 2*mm))
            for f in dr.dockerfiles_found:
                story.append(Paragraph(f"  • {f}", MONO))

        if dr.build_errors:
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph("Build Errors", H3))
            err_rows = [[Paragraph(ln.strip(), MONO_LIGHT)]
                        for ln in dr.build_errors.splitlines()[:25] if ln.strip()]
            if err_rows:
                et = Table(err_rows, colWidths=[None])
                et.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), _ROSE_L),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("BOX",           (0, 0), (-1, -1), 0.8, _ROSE),
                ]))
                story.append(et)
    else:
        story.append(Paragraph("Agent did not run.", SMALL))

    story.append(PageBreak())

    # ── Supervisor Decision ─────────────────────────────────────────────────
    story.append(_section("Supervisor Decision"))
    story.append(Spacer(1, 4*mm))
    sd = run.supervisor_decision
    if sd:
        dec_row = [
            _decision_badge(sd.action.value),
            Spacer(8*mm, 1),
            Table([
                [Paragraph("CONFIDENCE",   KV_KEY), Paragraph(f"{sd.confidence*100:.0f}%", KV_VAL)],
                [Paragraph("FINDINGS",     KV_KEY), Paragraph(str(sd.total_findings),       KV_VAL)],
                [Paragraph("CRITICAL",     KV_KEY), Paragraph(str(sd.critical_count),        KV_VAL)],
                [Paragraph("HIGH",         KV_KEY), Paragraph(str(sd.high_count),             KV_VAL)],
                [Paragraph("MEDIUM",       KV_KEY), Paragraph(str(sd.medium_count),           KV_VAL)],
                [Paragraph("LOW",          KV_KEY), Paragraph(str(sd.low_count),              KV_VAL)],
            ], colWidths=[24*mm, 20*mm]),
        ]
        dec_layout = Table([dec_row], colWidths=[44*mm, 8*mm, None])
        dec_layout.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(dec_layout)
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(sd.summary, BODY))

        if sd.auto_fixable_findings:
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph("Auto-Fixable Findings", H3))
            tbl = _findings_table(sd.auto_fixable_findings)
            if tbl: story.append(tbl)

        if sd.escalation_findings:
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph("Escalated for Human Review", H3))
            tbl = _findings_table(sd.escalation_findings)
            if tbl: story.append(tbl)
    else:
        story.append(Paragraph("Supervisor did not run.", SMALL))
    story.append(Spacer(1, 6*mm))

    # ── Auto-Fix ────────────────────────────────────────────────────────────
    story.append(_section("Auto-Fix"))
    story.append(Spacer(1, 4*mm))
    af = run.autofix_result
    if af and af.fixes_applied:
        story.append(_kv_table([
            ("Patches Applied", str(af.fixes_applied)),
            ("Branch",          af.branch_name or "—"),
            ("PR URL",          af.pr_url or "—"),
        ]))
        if af.pr_body:
            story.append(Spacer(1, 3*mm))
            story.append(Paragraph("PR Body", H3))
            for line in af.pr_body.splitlines()[:20]:
                story.append(Paragraph(line or "&nbsp;", MONO_LIGHT))
    else:
        story.append(Paragraph("No auto-fix was applied in this run.", SMALL))
    story.append(Spacer(1, 6*mm))

    # ── Deployment ──────────────────────────────────────────────────────────
    story.append(_section("Deployment"))
    story.append(Spacer(1, 4*mm))
    dep = run.deployment_result
    if dep:
        strategy = dep.strategy.value if hasattr(dep.strategy, "value") else str(dep.strategy)
        story.append(Paragraph(
            f"Strategy: <b>{strategy.upper()}</b>  ·  "
            f"Log lines: <b>{len(dep.log_lines)}</b>", BODY))
        if dep.log_lines:
            story.append(Spacer(1, 3*mm))
            story.append(_timeline_log(dep.log_lines))
    else:
        story.append(Paragraph("Deployment did not run.", SMALL))

    story.append(Spacer(1, 8*mm))
    story.append(_hr())
    completed_str = run.completed_at[:19].replace("T", "  ") if run.completed_at else "—"
    story.append(Paragraph(
        f"Report generated by AutoPilot CI  ·  Run {run.run_id}  ·  {completed_str} UTC",
        FOOTER_TXT))

    doc.build(story)
    return buf.getvalue()


def save_report(run: PipelineRun, output_dir: str) -> str:
    """Generate and save PDF to *output_dir/report_<run_id[:8]>.pdf*."""
    pdf_bytes = generate_report(run)
    out_path = Path(output_dir) / f"report_{run.run_id[:8]}.pdf"
    out_path.write_bytes(pdf_bytes)
    return str(out_path)

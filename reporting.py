"""Named estimation snapshots and shareable PDF reports."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
import re
import unicodedata
from xml.sax.saxutils import escape

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError:  # The UI gives a useful message if requirements were not installed.
    colors = None


def safe_filename(value: str, fallback: str = "estimacion_gateways") -> str:
    """Return a portable, readable filename stem."""
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("._-")
    return normalized[:80] or fallback


def normalize_for_json(value):
    """Convert pandas/numpy-like values into JSON-safe primitive values."""
    if isinstance(value, dict):
        return {str(key): normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_for_json(item) for item in value]
    if hasattr(value, "item"):
        try:
            return normalize_for_json(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        if value != value:
            return None
        if value in (float("inf"), float("-inf")):
            return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def scenarios_json(scenarios: dict) -> bytes:
    """Serialize the user's named scenario portfolio."""
    payload = {
        "format": "lorawan-gateway-estimation-scenarios",
        "version": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scenarios": normalize_for_json(scenarios),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def load_scenarios_json(data: bytes) -> dict:
    """Validate and load an exported scenario portfolio."""
    payload = json.loads(data.decode("utf-8-sig"))
    if payload.get("format") != "lorawan-gateway-estimation-scenarios":
        raise ValueError("El archivo no corresponde a una cartera de estimaciones válida.")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("El archivo no contiene escenarios válidos.")
    return scenarios


def _printable(value) -> str:
    """Keep PDF text compatible with ReportLab's standard Helvetica font."""
    text = "" if value is None else str(value)
    replacements = {
        "×": "x",
        "–": "-",
        "—": "-",
        "→": "->",
        "≤": "<=",
        "≥": ">=",
        "²": "2",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.encode("cp1252", "replace").decode("cp1252")


def _paragraph(value, style):
    return Paragraph(escape(_printable(value)).replace("\n", "<br/>"), style)


def _kv_table(items: list[tuple[str, object]], styles, widths=None):
    data = [[_paragraph(label, styles["Label"]), _paragraph(value, styles["BodyText"])] for label, value in items]
    table = Table(data, colWidths=widths or [65 * mm, 190 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF0F6")),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#19324D")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C4D0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _data_table(headers, rows, styles, widths=None):
    data = [[_paragraph(header, styles["TableHeader"]) for header in headers]]
    data.extend([[_paragraph(cell, styles["TableCell"]) for cell in row] for row in rows])
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#19324D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#AAB7C4")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _page_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D2D9E1"))
    canvas.line(14 * mm, 11 * mm, landscape(A4)[0] - 14 * mm, 11 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#607080"))
    canvas.drawString(14 * mm, 7 * mm, "Estimador LoRaWAN AU915 - Informe de dimensionamiento")
    canvas.drawRightString(landscape(A4)[0] - 14 * mm, 7 * mm, f"Página {doc.page}")
    canvas.restoreState()


def build_pdf_report(snapshot: dict) -> bytes:
    """Create a polished landscape PDF report from a saved estimation snapshot."""
    if colors is None:
        raise RuntimeError("Falta reportlab. Instale las dependencias de requirements.txt.")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=13 * mm,
        bottomMargin=16 * mm,
        title=_printable(snapshot.get("name", "Estimación LoRaWAN")),
        author="Estimador LoRaWAN AU915",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=colors.HexColor("#19324D"), alignment=TA_LEFT, spaceAfter=4))
    styles.add(ParagraphStyle(name="Subtitle", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#607080"), spaceAfter=10))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=colors.HexColor("#C33A32"), spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="Label", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=8.5, leading=11))
    styles.add(ParagraphStyle(name="TableHeader", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, alignment=TA_CENTER, textColor=colors.white))
    styles.add(ParagraphStyle(name="TableCell", parent=styles["BodyText"], fontName="Helvetica", fontSize=7.3, leading=9, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Note", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.HexColor("#5A4A00"), backColor=colors.HexColor("#FFF7D6"), borderPadding=6, spaceAfter=5))

    summary = snapshot.get("summary", {})
    parameters = snapshot.get("parameters", {})
    coverage = snapshot.get("coverage") or {}
    story = [
        _paragraph(snapshot.get("name", "Estimación LoRaWAN"), styles["ReportTitle"]),
        _paragraph(
            f"Informe generado {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')} | Escenario guardado {snapshot.get('saved_at', 'sin fecha')}",
            styles["Subtitle"],
        ),
        _paragraph("Resumen ejecutivo", styles["Section"]),
        _kv_table(
            [
                ("Gateways finales recomendados", summary.get("gateways_finales")),
                ("Gateways por capacidad", summary.get("gateways_por_capacidad")),
                ("Gateways por cobertura", summary.get("gateways_por_cobertura", "Sin polígono")),
                ("Condición dominante", summary.get("condicion_dominante")),
                ("Gateways por uplink", summary.get("gateways_por_uplink")),
                ("Gateways por ACK airtime", summary.get("gateways_por_airtime_ack")),
                ("Gateways por bloqueo RX", summary.get("gateways_por_blocking")),
                ("Airtime DL disponible", f"{summary.get('airtime_dl_disponible_s_hora', '')} s/h"),
            ],
            styles,
            [65 * mm, 65 * mm],
        ),
        Spacer(1, 4 * mm),
        _paragraph("Parámetros del escenario", styles["Section"]),
        _kv_table([(key, value) for key, value in parameters.items()], styles),
    ]

    if coverage:
        story.extend(
            [
                _paragraph("Cobertura geográfica", styles["Section"]),
                _kv_table([(key, value) for key, value in coverage.items()], styles),
            ]
        )

    warnings = snapshot.get("warnings") or []
    if warnings:
        warning_block = [_paragraph("Advertencias y supuestos", styles["Section"])]
        warning_block.extend(_paragraph(f"- {warning}", styles["Note"]) for warning in warnings)
        story.append(KeepTogether(warning_block))

    details = snapshot.get("details") or []
    if details:
        story.extend([Spacer(1, 4 * mm), _paragraph("Detalle de capacidad por spreading factor", styles["Section"])])
        wanted = [
            ("SF UL", "SF UL"),
            ("Nodos", "Nodos"),
            ("Uplinks/hora", "Uplinks/hora"),
            ("Payload UL bytes", "Payload UL"),
            ("ToA UL (ms)", "ToA UL ms"),
            ("Carga uplink", "Carga UL"),
            ("ToA ACK pond. (s)", "ToA ACK s"),
            ("Airtime ACK s/h", "ACK s/h"),
        ]
        headers = [label for _, label in wanted]
        rows = [[row.get(key, "") for key, _ in wanted] for row in details]
        story.append(_data_table(headers, rows, styles, [25 * mm, 24 * mm, 31 * mm, 27 * mm, 27 * mm, 27 * mm, 30 * mm, 28 * mm]))

    sites = snapshot.get("sites") or []
    if sites:
        story.extend([Spacer(1, 6 * mm), _paragraph("Ubicaciones preliminares de gateways", styles["Section"])])
        site_keys = ["gateway", "longitude", "latitude", "antena", "ganancia_dbi", "azimuth_deg", "downtilt_deg"]
        site_headers = ["Gateway", "Longitud", "Latitud", "Antena", "Ganancia dBi", "Azimut", "Downtilt"]
        site_rows = [[row.get(key, "") for key in site_keys] for row in sites]
        story.append(_data_table(site_headers, site_rows, styles, [24 * mm, 34 * mm, 34 * mm, 55 * mm, 29 * mm, 27 * mm, 27 * mm]))

    story.extend(
        [
            Spacer(1, 7 * mm),
            KeepTogether(
                [
                    _paragraph("Alcance del resultado", styles["Section"]),
                    _paragraph(
                        "Esta es una estimación de ingeniería basada en capacidad, link budget y geometría. En terminales de contenedores, confirme el diseño mediante site survey y mediciones RSSI/SNR antes de fijar ubicaciones definitivas.",
                        styles["BodyText"],
                    ),
                ]
            ),
        ]
    )
    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buffer.getvalue()

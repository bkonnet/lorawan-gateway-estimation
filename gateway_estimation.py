import base64
import json
import math
from dataclasses import replace
from datetime import datetime
from io import BytesIO

import pandas as pd

from coverage_model import (
    ANTENNA_PRESETS,
    ENVIRONMENT_PRESETS,
    AntennaConfig,
    RadioConfig,
    augment_gateway_deployments,
    deployment_link_analysis,
    gateway_sites_geojson,
    parse_polygon_file,
    path_loss_exponent_scenarios,
    plan_coverage,
    points_to_lon_lat,
    ray_length_within_geometry,
)
from reporting import (
    build_pdf_report,
    load_scenarios_json,
    safe_filename,
    scenarios_json,
)

try:
    import streamlit as st
except ImportError:
    st = None

try:
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Circle, Patch, Wedge
except ImportError:
    plt = None
    Circle = None
    Line2D = None
    Patch = None
    Wedge = None


if st is not None:
    @st.cache_data(show_spinner=False, max_entries=16)
    def cached_parse_polygon_file(filename, value, projection=None):
        """Parse each uploaded geometry once per file/projection combination."""
        return parse_polygon_file(filename, value, projection=projection)


    @st.cache_data(show_spinner=False, max_entries=24)
    def cached_plan_coverage(*args, **kwargs):
        """Reuse identical RF optimizations across Streamlit reruns."""
        return plan_coverage(*args, **kwargs)
else:
    cached_parse_polygon_file = parse_polygon_file
    cached_plan_coverage = plan_coverage

"""
Estimador interactivo de gateways LoRaWAN para AU915-928
=========================================================

Ejecutar:
    streamlit run gateway_estimation_au915.py

Dependencias:
    pip install streamlit pandas matplotlib openpyxl

Modelo AU915:
- Uplink 125 kHz.
- ACK/downlink 500 kHz.
- RX1 AU915 con RX1DROffset=0.
- RX2 fallback modelado como DR8 = SF12 @ 500 kHz.
- Gateway conservador: 1 transmisión downlink simultánea.

Modos de SF:
1. ADR OFF:
   Los equipos usan DR/SF fijo. La distribución SF queda 100% en el DR/SF seleccionado.

2. ADR ON:
   Se habilitan perfiles de distribución SF, porque ADR puede mover nodos a SF más bajos.
"""

SF_ORDER = ["SF7", "SF8", "SF9", "SF10", "SF11", "SF12"]

AU915_UL_TO_DL_SF = {
    12: 12,
    11: 11,
    10: 10,
    9: 9,
    8: 8,
    7: 7,
}

AU915_DOWNLINK_BW = 500_000
AU915_DOWNLINK_CHANNELS = 8
AU915_DOWNLINK_TX_CHAINS = 1
AU915_RX2_SF = 12
AU915_DWELL_TIME_MS = 400

ESTIMATION_WIDGET_KEYS = (
    "input_profile",
    "input_nodes",
    "input_messages_hour",
    "input_payload_ul",
    "input_uplink_channels",
    "input_aloha_efficiency",
    "input_confirmed_ratio",
    "input_ack_payload",
    "input_rx2_ratio",
    "input_ack_efficiency",
    "input_max_blocking",
    "input_retransmission_factor",
    "input_fopts_uplink",
    "input_fopts_downlink",
    "input_uplink_dwell",
    "input_safety_factor",
    "input_adr_enabled",
    "input_fixed_dr",
    "input_adr_profile",
    "input_edit_sf",
    "input_sf_SF7",
    "input_sf_SF8",
    "input_sf_SF9",
    "input_sf_SF10",
    "input_sf_SF11",
    "input_sf_SF12",
    "input_coverage_enabled",
    "input_environment",
    "input_antenna",
    "input_redundancy",
    "input_require_hpbw_redundancy",
    "input_target_sf",
    "input_tx_eirp",
    "input_device_antenna_gain",
    "input_device_installation_loss",
    "input_gateway_tx_eirp",
    "input_device_sensitivity",
    "input_validate_downlink",
    "input_gateway_gain",
    "input_cable_loss",
    "input_horizontal_beamwidth",
    "input_vertical_beamwidth",
    "input_max_antenna_attenuation",
    "input_gateway_height",
    "input_device_height",
    "input_downtilt",
    "input_resolution",
    "input_minimum_site_separation",
    "input_path_loss_exponent",
    "input_analyze_path_loss_range",
    "input_path_loss_variation",
    "input_additional_loss",
    "input_fade_margin",
    "input_obstacle_loss",
    "input_maximum_obstacle_loss",
    "input_strategy",
    "input_edge_priority",
    "input_dispersion_weight",
    "input_use_coverage_distribution",
    "input_sensitivity_SF7",
    "input_sensitivity_SF8",
    "input_sensitivity_SF9",
    "input_sensitivity_SF10",
    "input_sensitivity_SF11",
    "input_sensitivity_SF12",
)


def capture_estimation_input_state(state) -> dict:
    """Capture every restorable model control from Streamlit session state."""
    return {
        key: state[key]
        for key in ESTIMATION_WIDGET_KEYS
        if key in state
    }


def restorable_input_state(snapshot: dict) -> dict:
    """Return current-format inputs, with a small migration for legacy snapshots."""
    stored = snapshot.get("input_state")
    if isinstance(stored, dict) and stored:
        restored = {
            key: value
            for key, value in stored.items()
            if key in ESTIMATION_WIDGET_KEYS
        }
        return restored

    parameters = snapshot.get("parameters") or {}
    coverage = snapshot.get("coverage") or {}

    def percent(value):
        if isinstance(value, str) and value.endswith("%"):
            return float(value[:-1]) / 100
        return float(value)

    migrated = {}
    legacy_mapping = {
        "Nodos totales": "input_nodes",
        "Mensajes por nodo por hora": "input_messages_hour",
        "Payload aplicación uplink (bytes)": "input_payload_ul",
        "Canales uplink por gateway": "input_uplink_channels",
        "Eficiencia ALOHA uplink": "input_aloha_efficiency",
        "Payload aplicación ACK (bytes)": "input_ack_payload",
        "Factor retransmisiones": "input_retransmission_factor",
        "Factor de seguridad": "input_safety_factor",
    }
    for label, key in legacy_mapping.items():
        if label in parameters:
            migrated[key] = parameters[label]
    if "Perfil operativo" in parameters:
        migrated["input_profile"] = parameters["Perfil operativo"]
    if "ADR" in parameters:
        migrated["input_adr_enabled"] = parameters["ADR"] == "Habilitado"
    if "Uplinks confirmados" in parameters:
        migrated["input_confirmed_ratio"] = percent(parameters["Uplinks confirmados"])
    if "ACK en RX2" in parameters:
        migrated["input_rx2_ratio"] = percent(parameters["ACK en RX2"])
    if coverage:
        migrated["input_coverage_enabled"] = True
        migrated["input_require_hpbw_redundancy"] = True
        migrated["input_analyze_path_loss_range"] = True
        migrated["input_path_loss_variation"] = 0.3
    return migrated


def antenna_preset_input_state(antenna_name: str) -> dict:
    """Map an antenna preset to every RF control that it owns."""
    preset = ANTENNA_PRESETS[antenna_name]
    return {
        "input_gateway_gain": float(preset["gain_dbi"]),
        "input_horizontal_beamwidth": float(preset["horizontal_beamwidth_deg"]),
        "input_vertical_beamwidth": float(preset["vertical_beamwidth_deg"]),
        "input_max_antenna_attenuation": float(preset["max_attenuation_db"]),
        "input_downtilt": float(preset["downtilt_deg"]),
    }


def environment_preset_input_state(environment_name: str) -> dict:
    """Map an environment preset to its propagation controls."""
    preset = ENVIRONMENT_PRESETS[environment_name]
    return {
        "input_path_loss_exponent": float(preset["path_loss_exponent"]),
        "input_additional_loss": float(preset["additional_loss_db"]),
        "input_fade_margin": float(preset["fade_margin_db"]),
    }


def calcular_toa(sf: int, payload_bytes: int, bw: int = 125_000) -> float:
    """Calcula Time on Air aproximado para LoRa, CR=4/5, header explícito, CRC on, preámbulo 8."""
    cr = 1
    ih = 0
    crc = 1
    # Low Data Rate Optimization applies when the symbol exceeds 16 ms.
    de = 1 if ((2 ** sf) / bw) > 0.016 else 0
    preamble = 8

    t_sym = (2 ** sf) / bw
    t_preamble = (preamble + 4.25) * t_sym
    payload_symbols = 8 + max(
        math.ceil(
            (8 * payload_bytes - 4 * sf + 28 + 16 * crc - 20 * ih)
            / (4 * (sf - 2 * de))
        )
        * (cr + 4),
        0,
    )
    return round(t_preamble + payload_symbols * t_sym, 4)


def payload_fisico_uplink(payload_aplicacion_bytes: int, fopts_bytes: int = 0) -> int:
    """LoRaWAN data uplink: MHDR + FHDR + FPort + FRMPayload + MIC."""
    return 13 + max(payload_aplicacion_bytes, 0) + max(fopts_bytes, 0)


def payload_fisico_downlink(payload_aplicacion_bytes: int, fopts_bytes: int = 0) -> int:
    """ACK-only is 12 bytes; a frame with application payload also carries FPort."""
    base = 13 if payload_aplicacion_bytes > 0 else 12
    return base + max(payload_aplicacion_bytes, 0) + max(fopts_bytes, 0)


def validar_distribucion(distribucion_sf: dict) -> None:
    esperados = set(SF_ORDER)
    recibidos = set(distribucion_sf.keys())
    if recibidos != esperados:
        faltan = esperados - recibidos
        sobran = recibidos - esperados
        raise ValueError(f"La distribución debe contener exactamente SF7 a SF12. Faltan: {faltan}; sobran: {sobran}")

    suma = sum(distribucion_sf.values())
    if not math.isclose(suma, 1.0, abs_tol=1e-6):
        raise ValueError(f"La suma de la distribución SF debe ser 1.0. Suma actual: {suma:.4f}")


def normalizar_distribucion(valores: dict) -> dict:
    total = sum(valores.values())
    if total <= 0:
        return distribucion_por_dr_fijo("SF10")
    return {sf: valor / total for sf, valor in valores.items()}


def distribucion_por_dr_fijo(sf_fijo: str) -> dict:
    return {sf: 1.0 if sf == sf_fijo else 0.0 for sf in SF_ORDER}


def au915_dr_to_sf() -> dict:
    return {
        "DR0 / SF12 / 125 kHz": "SF12",
        "DR1 / SF11 / 125 kHz": "SF11",
        "DR2 / SF10 / 125 kHz": "SF10",
        "DR3 / SF9 / 125 kHz": "SF9",
        "DR4 / SF8 / 125 kHz": "SF8",
        "DR5 / SF7 / 125 kHz": "SF7",
    }


def perfiles_adr() -> dict:
    return {
        "ADR conservador puerto": {
            "SF7": 0.10,
            "SF8": 0.20,
            "SF9": 0.25,
            "SF10": 0.25,
            "SF11": 0.15,
            "SF12": 0.05,
        },
        "ADR optimizado buena cobertura": {
            "SF7": 0.25,
            "SF8": 0.30,
            "SF9": 0.25,
            "SF10": 0.15,
            "SF11": 0.04,
            "SF12": 0.01,
        },
        "ADR cobertura difícil / indoor": {
            "SF7": 0.05,
            "SF8": 0.10,
            "SF9": 0.20,
            "SF10": 0.30,
            "SF11": 0.25,
            "SF12": 0.10,
        },
    }


def estimar_gateways(
    nodos_totales: int,
    mensajes_por_nodo_por_hora: float,
    eficiencia_aloha_uplink: float,
    canales_uplink_por_gateway: int,
    factor_seguridad: float,
    distribucion_sf: dict,
    payload_uplink_bytes: int,
    confirmed_ratio: float,
    ack_payload_bytes: int,
    rx2_fallback_ratio: float,
    eficiencia_downlink_ack: float,
    max_blocking_rx: float,
    retransmission_factor: float,
    fopts_uplink_bytes: int = 0,
    fopts_downlink_bytes: int = 0,
    uplink_dwell_time_enabled: bool = True,
) -> tuple[pd.DataFrame, dict, list[str]]:
    validar_distribucion(distribucion_sf)
    advertencias = []

    filas = []
    carga_uplink_total = 0.0
    ack_attempts_hora_total = 0.0
    airtime_ack_total_s_hora = 0.0
    blocking_total = 0.0

    for sf_label in SF_ORDER:
        sf_ul = int(sf_label.replace("SF", ""))
        proporcion = distribucion_sf[sf_label]
        nodos_sf = int(round(nodos_totales * proporcion))
        mensajes_base_hora_sf = nodos_sf * mensajes_por_nodo_por_hora
        # The retransmission factor describes retries of confirmed transactions.
        multiplicador_uplink = 1 + confirmed_ratio * (retransmission_factor - 1)
        mensajes_hora_sf = mensajes_base_hora_sf * multiplicador_uplink

        payload_ul_fisico = payload_fisico_uplink(payload_uplink_bytes, fopts_uplink_bytes)
        toa_uplink = calcular_toa(sf_ul, payload_ul_fisico, bw=125_000)
        toa_ul_ms = toa_uplink * 1000
        if uplink_dwell_time_enabled and toa_ul_ms > AU915_DWELL_TIME_MS and nodos_sf > 0:
            advertencias.append(
                f"⚠ DWELL TIME UPLINK: SF{sf_ul} con {payload_ul_fisico} bytes físicos = "
                f"{toa_ul_ms:.0f} ms > 400 ms. Revisar payload, SF o dwell-time regional."
            )

        capacidad_teorica_uplink_canal = 3600 / toa_uplink
        capacidad_aloha_uplink_gw = capacidad_teorica_uplink_canal * eficiencia_aloha_uplink * canales_uplink_por_gateway
        carga_uplink = mensajes_hora_sf / capacidad_aloha_uplink_gw if capacidad_aloha_uplink_gw else 0

        sf_dl_rx1 = AU915_UL_TO_DL_SF[sf_ul]
        payload_ack_fisico = payload_fisico_downlink(ack_payload_bytes, fopts_downlink_bytes)
        toa_ack_rx1 = calcular_toa(sf_dl_rx1, payload_ack_fisico, bw=AU915_DOWNLINK_BW)
        toa_ack_rx2 = calcular_toa(AU915_RX2_SF, payload_ack_fisico, bw=AU915_DOWNLINK_BW)
        toa_ack_ponderado = (toa_ack_rx1 * (1 - rx2_fallback_ratio)) + (toa_ack_rx2 * rx2_fallback_ratio)

        ack_attempts_hora_sf = mensajes_base_hora_sf * confirmed_ratio * retransmission_factor
        airtime_ack_s_hora_sf = ack_attempts_hora_sf * toa_ack_ponderado
        blocking_fraction_sf = airtime_ack_s_hora_sf / 3600

        carga_uplink_total += carga_uplink
        ack_attempts_hora_total += ack_attempts_hora_sf
        airtime_ack_total_s_hora += airtime_ack_s_hora_sf
        blocking_total += blocking_fraction_sf

        filas.append(
            {
                "SF UL": sf_label,
                "Distribución": round(proporcion, 3),
                "Nodos": nodos_sf,
                "Uplinks base/hora": round(mensajes_base_hora_sf, 1),
                "Uplinks aire/hora": round(mensajes_hora_sf, 1),
                "Payload app UL": payload_uplink_bytes,
                "Payload físico UL": payload_ul_fisico,
                "ToA UL (s)": toa_uplink,
                "ToA UL (ms)": round(toa_ul_ms, 0),
                "Dwell OK UL": (
                    "no aplica"
                    if not uplink_dwell_time_enabled
                    else ("✓" if toa_ul_ms <= AU915_DWELL_TIME_MS else "⚠ revisar")
                ),
                "Carga uplink": round(carga_uplink, 3),
                "SF DL RX1": f"SF{sf_dl_rx1}",
                "ToA ACK RX1 (s)": toa_ack_rx1,
                "ToA ACK RX2 (s)": toa_ack_rx2,
                "ToA ACK pond. (s)": round(toa_ack_ponderado, 4),
                "ACK/downlink attempts hora": round(ack_attempts_hora_sf, 1),
                "Airtime ACK s/h": round(airtime_ack_s_hora_sf, 1),
                "Bloqueo RX por ACK": round(blocking_fraction_sf, 4),
            }
        )

    gateways_por_uplink = carga_uplink_total
    airtime_ack_disponible_s_hora = 3600 * eficiencia_downlink_ack * AU915_DOWNLINK_TX_CHAINS
    gateways_por_airtime_ack = airtime_ack_total_s_hora / airtime_ack_disponible_s_hora if airtime_ack_disponible_s_hora else float("inf")
    gateways_por_blocking = blocking_total / max_blocking_rx if max_blocking_rx else float("inf")
    gateways_por_ack = max(gateways_por_airtime_ack, gateways_por_blocking)

    cuello_botella = "ACK/downlink" if gateways_por_ack > gateways_por_uplink else "uplink"
    gateways_base = max(gateways_por_uplink, gateways_por_ack)
    gateways_estimados = round(gateways_base * factor_seguridad, 2)
    gateways_recomendados = max(1, math.ceil(gateways_estimados))

    filas.append(
        {
            "SF UL": "TOTAL",
            "Distribución": round(sum(distribucion_sf.values()), 3),
            "Nodos": nodos_totales,
            "Uplinks base/hora": round(nodos_totales * mensajes_por_nodo_por_hora, 1),
            "Uplinks aire/hora": round(
                nodos_totales
                * mensajes_por_nodo_por_hora
                * (1 + confirmed_ratio * (retransmission_factor - 1)),
                1,
            ),
            "Payload app UL": payload_uplink_bytes,
            "Payload físico UL": payload_fisico_uplink(payload_uplink_bytes, fopts_uplink_bytes),
            "ToA UL (s)": None,
            "ToA UL (ms)": None,
            "Dwell OK UL": "",
            "Carga uplink": round(carga_uplink_total, 3),
            "SF DL RX1": "",
            "ToA ACK RX1 (s)": None,
            "ToA ACK RX2 (s)": None,
            "ToA ACK pond. (s)": None,
            "ACK/downlink attempts hora": round(ack_attempts_hora_total, 1),
            "Airtime ACK s/h": round(airtime_ack_total_s_hora, 1),
            "Bloqueo RX por ACK": round(blocking_total, 4),
        }
    )

    resumen = {
        "gateways_por_uplink": round(gateways_por_uplink, 2),
        "gateways_por_airtime_ack": round(gateways_por_airtime_ack, 2),
        "gateways_por_blocking": round(gateways_por_blocking, 2),
        "gateways_por_ack": round(gateways_por_ack, 2),
        "gateways_estimados": gateways_estimados,
        "gateways_recomendados": gateways_recomendados,
        "cuello_botella": cuello_botella,
        "ack_attempts_hora_total": round(ack_attempts_hora_total, 1),
        "airtime_ack_total_s_hora": round(airtime_ack_total_s_hora, 1),
        "blocking_total": round(blocking_total, 4),
        "canales_downlink_au915": AU915_DOWNLINK_CHANNELS,
        "tx_chains_downlink_modeladas": AU915_DOWNLINK_TX_CHAINS,
        "airtime_dl_disponible_s_hora": round(airtime_ack_disponible_s_hora, 1),
        "payload_fisico_uplink_bytes": payload_fisico_uplink(
            payload_uplink_bytes, fopts_uplink_bytes
        ),
        "payload_fisico_ack_bytes": payload_fisico_downlink(
            ack_payload_bytes, fopts_downlink_bytes
        ),
    }

    return pd.DataFrame(filas), resumen, advertencias


def app_streamlit():
    st.set_page_config(page_title="Estimador LoRaWAN AU915", layout="wide")
    if "saved_estimations" not in st.session_state:
        st.session_state.saved_estimations = {}
    st.title("Estimador de gateways LoRaWAN — AU915-928")
    loaded_notice = st.session_state.pop("_loaded_estimation_notice", None)
    if loaded_notice:
        st.success(loaded_notice)
    st.caption(
        "ADR ON/OFF · DR fijo sin ADR · perfiles con ADR · ACK downlink 500 kHz · RX2 DR8 SF12"
    )

    with st.sidebar:
        st.header("Perfil operativo")
        perfil_operativo = st.selectbox(
            "Preset de entorno",
            options=["Personalizado", "Terminal Contenedores"],
            index=0,
            key="input_profile",
            help="Terminal Contenedores aplica tanto con ADR OFF como con ADR ON. Ajusta canal/ACK/seguridad para patios con contenedores metálicos, multipath y pérdidas intermitentes. En ADR OFF no cambia el DR/SF fijo seleccionado.",
        )

        if perfil_operativo == "Terminal Contenedores":
            preset_eficiencia_aloha = 0.10
            preset_confirmed_ratio = 1.0
            preset_rx2_fallback = 0.20
            preset_retransmission_factor = 2.0
            preset_eficiencia_ack = 0.10
            preset_max_blocking_rx = 0.06
            preset_factor_seguridad = 1.4
            st.info(
                "Perfil Terminal Contenedores aplicado. Este preset también aplica con ADR OFF: no cambia el DR/SF fijo, solo ajusta eficiencia uplink, RX2 fallback, retransmisiones ACK, bloqueo RX y margen operacional.",
                icon="🏗️",
            )
        else:
            preset_eficiencia_aloha = 0.15
            preset_confirmed_ratio = 1.0
            preset_rx2_fallback = 0.10
            preset_retransmission_factor = 1.5
            preset_eficiencia_ack = 0.10
            preset_max_blocking_rx = 0.10
            preset_factor_seguridad = 1.2

        st.header("Tráfico uplink")
        nodos = st.number_input(
            "Nodos totales",
            min_value=1,
            value=5000,
            step=100,
            key="input_nodes",
            help="Cantidad total de dispositivos LoRaWAN activos en la red. A mayor cantidad de nodos, mayor carga uplink y mayor cantidad de ACK/downlink.",
        )
        mensajes_hora = st.number_input(
            "Mensajes por nodo por hora",
            min_value=0.01,
            value=1.0,
            step=0.25,
            key="input_messages_hour",
            help="Frecuencia de transmisión de cada nodo. Subir este valor incrementa linealmente uplinks, ACK y airtime total.",
        )
        payload_ul = st.slider(
            "Payload de aplicación uplink (bytes)",
            min_value=1,
            max_value=250,
            value=10,
            key="input_payload_ul",
            help="Solo FRMPayload. El estimador añade automáticamente los 13 bytes mínimos de LoRaWAN.",
        )
        canales_ul = st.number_input(
            "Canales uplink por gateway",
            min_value=1,
            max_value=64,
            value=8,
            step=1,
            key="input_uplink_channels",
            help="Cantidad de canales uplink de 125 kHz disponibles por gateway. Más canales aumentan capacidad uplink.",
        )
        eficiencia_aloha = st.slider(
            "Eficiencia ALOHA uplink",
            min_value=0.01,
            max_value=0.30,
            value=preset_eficiencia_aloha,
            step=0.01,
            key="input_aloha_efficiency",
            help="Representa eficiencia real del acceso uplink tipo ALOHA: colisiones, pérdidas y reutilización imperfecta del canal. Subirlo aumenta capacidad estimada; bajarlo vuelve el modelo más conservador.",
        )

        st.header("ACK / downlink AU915")
        confirmed_ratio = st.slider(
            "Fracción de uplinks confirmados",
            0.0,
            1.0,
            preset_confirmed_ratio,
            0.05,
            key="input_confirmed_ratio",
            help="Porcentaje de uplinks que requieren ACK. 1.0 = todos los mensajes requieren ACK. A mayor valor, mayor carga downlink y bloqueo half-duplex.",
        )
        ack_payload = st.slider(
            "Payload de aplicación en ACK/downlink (bytes)",
            min_value=0,
            max_value=20,
            value=0,
            key="input_ack_payload",
            help="Un ACK vacío usa 0 aquí. El estimador añade automáticamente el paquete LoRaWAN mínimo de 12 bytes.",
        )
        rx2_fallback = st.slider(
            "Fracción de ACK que caen en RX2",
            0.0,
            0.50,
            preset_rx2_fallback,
            0.05,
            key="input_rx2_ratio",
            help="Porcentaje de ACK que no logran RX1 y terminan transmitiéndose en RX2. Subir este valor aumenta significativamente el airtime downlink.",
        )
        eficiencia_ack = st.slider(
            "Uso máximo de airtime downlink ACK por gateway",
            0.01,
            0.50,
            preset_eficiencia_ack,
            0.01,
            key="input_ack_efficiency",
            help="Fracción máxima del tiempo que un gateway puede usar transmitiendo ACK. Valores bajos vuelven el modelo más conservador.",
        )
        max_blocking_rx = st.slider(
            "Máximo bloqueo RX tolerable por ACK",
            0.01,
            0.50,
            preset_max_blocking_rx,
            0.01,
            key="input_max_blocking",
            help="Fracción máxima del tiempo que el gateway puede permanecer transmitiendo ACK y no recibiendo uplinks. Bajarlo exige más gateways.",
        )
        retransmission_factor = st.slider(
            "Factor de retransmisiones por ACK perdido",
            1.0,
            8.0,
            preset_retransmission_factor,
            0.5,
            key="input_retransmission_factor",
            help="Representa retransmisiones causadas por pérdida de uplinks o ACK. 1.0 = sin retransmisiones. Valores altos incrementan mucho la carga total.",
        )

        with st.expander("Opciones LoRaWAN avanzadas"):
            fopts_uplink = st.slider(
                "FOpts promedio uplink (bytes)", 0, 15, 0,
                key="input_fopts_uplink",
                help="Bytes promedio de comandos MAC transportados en FHDR.",
            )
            fopts_downlink = st.slider(
                "FOpts promedio downlink (bytes)", 0, 15, 0,
                key="input_fopts_downlink",
                help="Bytes promedio de comandos MAC transportados en el ACK/downlink.",
            )
            uplink_dwell_time_enabled = st.toggle(
                "UplinkDwellTime = 1 (límite 400 ms)",
                value=True,
                key="input_uplink_dwell",
                help="Desactivar solo si la configuración regional/red confirma UplinkDwellTime=0.",
            )

        st.header("Margen final")
        factor_seguridad = st.slider(
            "Factor de seguridad final",
            1.0,
            3.0,
            preset_factor_seguridad,
            0.1,
            key="input_safety_factor",
            help="Margen ingenieril para crecimiento futuro, variabilidad RF e incertidumbre operacional. No debe duplicar el efecto ACK.",
        )

    st.subheader("ADR y configuración DR/SF")
    adr_enabled = st.toggle(
        "ADR habilitado",
        value=False,
        key="input_adr_enabled",
        help="ADR OFF: los sensores usan DR/SF fijo. ADR ON: se habilitan perfiles de distribución SF.",
    )

    if not adr_enabled:
        st.info("ADR OFF: selecciona el DR/SF fijo configurado en los equipos. El perfil operativo, si está activo, sí ajusta canal/ACK/seguridad, pero no cambia el DR/SF.", icon="ℹ️")
        dr_map = au915_dr_to_sf()
        dr_fijo = st.selectbox(
            "DR/SF fijo de los equipos",
            list(dr_map.keys()),
            index=2,
            key="input_fixed_dr",
        )
        sf_fijo = dr_map[dr_fijo]
        distribucion = distribucion_por_dr_fijo(sf_fijo)
        st.write(f"Distribución usada: **100% de los nodos en {sf_fijo}**")
        st.dataframe(pd.DataFrame([{"SF": sf, "Distribución": distribucion[sf]} for sf in SF_ORDER]), use_container_width=True, hide_index=True)
    else:
        st.info("ADR ON: se puede usar un perfil de distribución SF o editarlo manualmente.", icon="ℹ️")
        perfiles = perfiles_adr()
        perfil_nombre = st.selectbox(
            "Perfil ADR", list(perfiles.keys()), key="input_adr_profile"
        )
        editar = st.checkbox(
            "Editar distribución SF manualmente",
            value=False,
            key="input_edit_sf",
        )
        base = perfiles[perfil_nombre]
        cols = st.columns(6)
        valores = {}
        for col, sf in zip(cols, SF_ORDER):
            with col:
                valores[sf] = st.number_input(
                    sf,
                    0.0,
                    1.0,
                    float(base[sf]),
                    0.01,
                    disabled=not editar,
                    key=f"input_sf_{sf}",
                )
        suma_sf = sum(valores.values())
        st.write(f"Suma actual distribución SF: **{suma_sf:.3f}**")
        distribucion = normalizar_distribucion(valores) if not math.isclose(suma_sf, 1.0, abs_tol=1e-6) else valores
        if not math.isclose(suma_sf, 1.0, abs_tol=1e-6):
            st.warning("La suma de SF no es 1.0. Se normalizará automáticamente para el cálculo.")

    traffic_distribution = dict(distribucion)

    st.divider()
    st.subheader("Cobertura geográfica y redundancia")
    coverage_enabled = st.toggle(
        "Incorporar polígono y cobertura RF",
        value=False,
        key="input_coverage_enabled",
        help="Combina la cantidad requerida por cobertura con el cuello de botella de capacidad.",
    )
    coverage_plan = None
    coverage_geometry = None
    use_coverage_distribution = False
    polygon_name = None
    polygon_bytes = None
    obstacle_name = None
    obstacle_bytes = None
    coverage_sensitivity_plans = []
    path_loss_sensitivity_rows = []
    coverage_map_png_base64 = None
    capacity_charts_png_base64 = None

    if coverage_enabled:
        st.info(
            "La planificación propone sitios dentro del polígono y valida el link budget "
            "punto por punto, incluyendo patrón de antena, sensibilidad, margen y obstáculos. "
            "En contenedores debe calibrarse posteriormente con mediciones RSSI/SNR.",
            icon="🗺️",
        )
        def polygon_upload_changed():
            st.session_state["_prefer_restored_polygon"] = False

        geojson_file = st.file_uploader(
            "Polígono del recinto (KMZ, KML o GeoJSON)",
            type=["kmz", "kml", "geojson", "json"],
            key="polygon_upload",
            on_change=polygon_upload_changed,
            help="KMZ/KML de Google Earth o GeoJSON Polygon/MultiPolygon en WGS84.",
        )
        prefer_restored_polygon = st.session_state.get(
            "_prefer_restored_polygon", False
        )
        active_polygon = st.session_state.get("_active_polygon")
        if prefer_restored_polygon and active_polygon:
            polygon_name = active_polygon["name"]
            polygon_bytes = base64.b64decode(active_polygon["data_base64"])
            st.info(f"Polígono restaurado: {polygon_name}")
        elif prefer_restored_polygon:
            polygon_name = None
            polygon_bytes = None
        elif geojson_file is not None:
            polygon_name = geojson_file.name
            polygon_bytes = geojson_file.getvalue()
            st.session_state["_active_polygon"] = {
                "name": polygon_name,
                "data_base64": base64.b64encode(polygon_bytes).decode("ascii"),
            }
        elif active_polygon:
            polygon_name = active_polygon["name"]
            polygon_bytes = base64.b64decode(active_polygon["data_base64"])
            st.info(f"Polígono restaurado: {polygon_name}")
        else:
            polygon_name = None
            polygon_bytes = None

        def obstacle_upload_changed():
            st.session_state["_prefer_restored_obstacles"] = False

        obstacle_file = st.file_uploader(
            "Filas/bloques de contenedores (opcional: KMZ, KML o GeoJSON)",
            type=["kmz", "kml", "geojson", "json"],
            key="obstacle_upload",
            on_change=obstacle_upload_changed,
            help=(
                "Cada polígono se interpreta como un obstáculo distinto. El enlace recibe "
                "la pérdida configurada por cada bloque que atraviesa."
            ),
        )
        prefer_restored_obstacles = st.session_state.get(
            "_prefer_restored_obstacles", False
        )
        active_obstacles = st.session_state.get("_active_obstacles")
        if prefer_restored_obstacles and active_obstacles:
            obstacle_name = active_obstacles["name"]
            obstacle_bytes = base64.b64decode(active_obstacles["data_base64"])
            st.info(f"Obstáculos restaurados: {obstacle_name}")
        elif prefer_restored_obstacles:
            obstacle_name = None
            obstacle_bytes = None
        elif obstacle_file is not None:
            obstacle_name = obstacle_file.name
            obstacle_bytes = obstacle_file.getvalue()
            st.session_state["_active_obstacles"] = {
                "name": obstacle_name,
                "data_base64": base64.b64encode(obstacle_bytes).decode("ascii"),
            }
        elif active_obstacles:
            obstacle_name = active_obstacles["name"]
            obstacle_bytes = base64.b64decode(active_obstacles["data_base64"])
            st.info(f"Obstáculos restaurados: {obstacle_name}")
        def environment_preset_changed():
            environment_state = environment_preset_input_state(
                st.session_state["input_environment"]
            )
            for key, value in environment_state.items():
                st.session_state[key] = value

        environment_name = st.selectbox(
            "Ambiente RF",
            list(ENVIRONMENT_PRESETS.keys()),
            index=list(ENVIRONMENT_PRESETS.keys()).index("Terminal de contenedores"),
            key="input_environment",
            on_change=environment_preset_changed,
        )
        environment = ENVIRONMENT_PRESETS[environment_name]

        def antenna_preset_changed():
            antenna_state = antenna_preset_input_state(
                st.session_state["input_antenna"]
            )
            for key, value in antenna_state.items():
                st.session_state[key] = value

        antenna_name = st.selectbox(
            "Tipo de antena por gateway",
            list(ANTENNA_PRESETS.keys()),
            index=list(ANTENNA_PRESETS.keys()).index("Sectorial 60° × 35°"),
            key="input_antenna",
            on_change=antenna_preset_changed,
            help="El modelo considera una antena y un azimut por gateway.",
        )
        antenna_preset = ANTENNA_PRESETS[antenna_name]

        cov1, cov2, cov3, cov4 = st.columns(4)
        with cov1:
            redundancy = st.number_input(
                "Gateways mínimos por punto",
                min_value=1,
                max_value=3,
                value=2,
                step=1,
                key="input_redundancy",
                help="2 exige que cada punto de evaluación sea cubierto por dos gateways distintos.",
            )
            target_sf = st.selectbox(
                "SF máximo de diseño",
                SF_ORDER,
                index=3,
                key="input_target_sf",
                help="SF10 es una referencia prudente si el dwell time de 400 ms está activo.",
            )
        with cov2:
            tx_eirp = st.number_input(
                "Potencia TX conducida del dispositivo (dBm)",
                0.0,
                36.0,
                20.0,
                1.0,
                key="input_tx_eirp",
                help="Potencia en el conector RF, antes de la ganancia y pérdidas de instalación.",
            )
            gateway_gain = st.number_input(
                "Ganancia máxima de antena (dBi)",
                0.0,
                30.0,
                float(antenna_preset["gain_dbi"]),
                0.5,
                key="input_gateway_gain",
            )
            cable_loss = st.number_input(
                "Pérdidas cable/conectores (dB)",
                0.0,
                10.0,
                2.0,
                0.5,
                key="input_cable_loss",
            )
        with cov3:
            horizontal_beamwidth = st.number_input(
                "Haz horizontal HPBW (°)",
                5.0,
                360.0,
                float(antenna_preset["horizontal_beamwidth_deg"]),
                5.0,
                key="input_horizontal_beamwidth",
            )
            vertical_beamwidth = st.number_input(
                "Haz vertical HPBW (°)",
                5.0,
                180.0,
                float(antenna_preset["vertical_beamwidth_deg"]),
                5.0,
                key="input_vertical_beamwidth",
            )
            max_antenna_attenuation = st.number_input(
                "Atenuación lateral/trasera máx. (dB)",
                0.0,
                50.0,
                float(antenna_preset["max_attenuation_db"]),
                1.0,
                key="input_max_antenna_attenuation",
            )
        with cov4:
            gateway_height = st.number_input(
                "Altura del gateway (m)", 2.0, 100.0, 20.0, 1.0,
                key="input_gateway_height",
            )
            device_height = st.number_input(
                "Altura del dispositivo (m)", 0.0, 20.0, 1.5, 0.5,
                key="input_device_height",
            )
            downtilt = st.number_input(
                "Downtilt hacia el suelo (°)",
                -10.0,
                45.0,
                float(antenna_preset["downtilt_deg"]),
                1.0,
                key="input_downtilt",
            )

        require_hpbw_redundancy = st.checkbox(
            "Exigir redundancia dentro del HPBW horizontal",
            value=True,
            key="input_require_hpbw_redundancy",
            help=(
                "Para antenas sectoriales o direccionales, cada gateway que cuenta para la "
                "redundancia debe apuntar al punto dentro de su haz principal. Los enlaces "
                "laterales pueden mostrarse en el mapa, pero no satisfacen la redundancia robusta."
            ),
        )

        st.markdown("##### Dispositivo, sensibilidades y enlace de retorno")
        device_col1, device_col2, device_col3, device_col4 = st.columns(4)
        with device_col1:
            device_antenna_gain = st.number_input(
                "Ganancia antena dispositivo (dBi)",
                -20.0,
                15.0,
                0.0,
                0.5,
                key="input_device_antenna_gain",
                help="Para el ZRSM comenzar con 0 dBi si no existe una medición mejor.",
            )
        with device_col2:
            device_installation_loss = st.number_input(
                "Pérdida instalación/puerta (dB)",
                0.0,
                40.0,
                6.0,
                0.5,
                key="input_device_installation_loss",
                help="Pérdida efectiva por montaje sobre metal, orientación y encapsulado.",
            )
        with device_col3:
            gateway_tx_eirp = st.number_input(
                "EIRP downlink gateway (dBm)",
                0.0,
                36.0,
                30.0,
                1.0,
                key="input_gateway_tx_eirp",
            )
        with device_col4:
            device_sensitivity = st.number_input(
                "Sensibilidad RX del dispositivo (dBm)",
                -150.0,
                -80.0,
                -129.0,
                1.0,
                key="input_device_sensitivity",
                help="Usar el valor de la ficha técnica para el DR del ACK/RX1 o RX2.",
            )
        validate_downlink = st.checkbox(
            "Exigir cobertura bidireccional para ACK/downlink",
            value=confirmed_ratio > 0,
            key="input_validate_downlink",
            help=(
                "Un punto solo cumple si tanto el uplink como el downlink superan sensibilidad "
                "más margen. Desactívelo únicamente para tráfico no confirmado sin comandos."
            ),
        )

        with st.expander("Sensibilidad del gateway por SF (BW125)"):
            st.caption(
                "Reemplace estos valores genéricos por la ficha técnica del gateway completo. "
                "El SF máximo de diseño usa el valor correspondiente de esta tabla."
            )
            default_sensitivities = {
                "SF7": -123.0,
                "SF8": -126.0,
                "SF9": -129.0,
                "SF10": -132.0,
                "SF11": -134.5,
                "SF12": -137.0,
            }
            sensitivity_cols = st.columns(6)
            sf_sensitivities = {}
            for sensitivity_col, sf_label in zip(sensitivity_cols, SF_ORDER):
                with sensitivity_col:
                    sf_sensitivities[sf_label] = st.number_input(
                        sf_label,
                        -150.0,
                        -80.0,
                        default_sensitivities[sf_label],
                        0.5,
                        key=f"input_sensitivity_{sf_label}",
                    )

        rf1, rf2, rf3, rf4 = st.columns(4)
        with rf1:
            resolution_m = st.number_input(
                "Resolución de evaluación (m)", 25.0, 1000.0, 100.0, 25.0,
                key="input_resolution",
                help=(
                    "Tamaño máximo de la celda de diseño. El modelo verifica internamente "
                    "con una malla dos veces más densa para detectar huecos entre puntos."
                ),
            )
            minimum_site_separation = st.number_input(
                "Separación mínima entre sitios (m)",
                0.0,
                5000.0,
                100.0,
                25.0,
                key="input_minimum_site_separation",
                help="Impide que distintas orientaciones en una misma coordenada se contabilicen como gateways redundantes. También distribuye los gateways añadidos por capacidad.",
            )
        with rf2:
            path_loss_exponent = st.number_input(
                "Exponente de pérdida",
                1.5,
                6.0,
                float(environment["path_loss_exponent"]),
                0.1,
                key="input_path_loss_exponent",
            )
        with rf3:
            additional_loss = st.number_input(
                "Pérdida adicional ambiente (dB)",
                0.0,
                50.0,
                float(environment["additional_loss_db"]),
                1.0,
                key="input_additional_loss",
            )
        with rf4:
            fade_margin = st.number_input(
                "Margen de desvanecimiento (dB)",
                0.0,
                40.0,
                float(environment["fade_margin_db"]),
                1.0,
                key="input_fade_margin",
            )

        sensitivity_col1, sensitivity_col2 = st.columns(2)
        with sensitivity_col1:
            analyze_path_loss_range = st.checkbox(
                "Calcular rango por incertidumbre del exponente",
                value=False,
                key="input_analyze_path_loss_range",
                help=(
                    "Ejecuta dos optimizaciones adicionales (favorable y crítica). Active esta "
                    "opción para el informe final, después de ajustar el escenario base."
                ),
            )
        with sensitivity_col2:
            path_loss_variation = st.number_input(
                "Variación del exponente (±)",
                0.0,
                1.0,
                0.3,
                0.1,
                key="input_path_loss_variation",
                disabled=not analyze_path_loss_range,
                help="Con base 3,6 y variación 0,3 se evalúan 3,3 / 3,6 / 3,9.",
            )

        obstacle_col1, obstacle_col2 = st.columns(2)
        with obstacle_col1:
            obstacle_loss = st.number_input(
                "Pérdida por bloque/fila atravesada (dB)",
                0.0,
                40.0,
                8.0,
                0.5,
                key="input_obstacle_loss",
                disabled=obstacle_bytes is None,
                help="Se aplica una vez por cada polígono de obstáculos cruzado por el enlace.",
            )
        with obstacle_col2:
            maximum_obstacle_loss = st.number_input(
                "Pérdida máxima acumulada por obstáculos (dB)",
                0.0,
                80.0,
                32.0,
                1.0,
                key="input_maximum_obstacle_loss",
                disabled=obstacle_bytes is None,
            )

        strategy_name = st.selectbox(
            "Estrategia de distribución espacial",
            [
                "Cobertura balanceada de perímetro y área",
                "Cantidad mínima de sitios",
            ],
            index=0,
            key="input_strategy",
            help=(
                "La estrategia balanceada agrega muestras en todos los bordes, prioriza extremos "
                "y favorece sitios separados. La estrategia mínima concentra gateways cuando eso "
                "reduce la cantidad total."
            ),
        )
        if strategy_name == "Cobertura balanceada de perímetro y área":
            strategy_col1, strategy_col2 = st.columns(2)
            with strategy_col1:
                edge_priority = st.slider(
                    "Prioridad del perímetro",
                    1.0,
                    6.0,
                    3.0,
                    0.5,
                    key="input_edge_priority",
                    help="Valores altos dan mayor importancia a extremos, entrantes y bordes del polígono.",
                )
            with strategy_col2:
                dispersion_weight = st.slider(
                    "Preferencia por distribución espacial",
                    0.0,
                    1.0,
                    0.30,
                    0.05,
                    key="input_dispersion_weight",
                    help="Favorece candidatos alejados de sitios ya seleccionados sin abandonar la cobertura RF.",
                )
        else:
            edge_priority = 1.0
            dispersion_weight = 0.0

        if polygon_bytes is not None:
            try:
                coverage_geometry = cached_parse_polygon_file(
                    polygon_name, polygon_bytes
                )
                coverage_obstacles = (
                    cached_parse_polygon_file(
                        obstacle_name,
                        obstacle_bytes,
                        projection=coverage_geometry.projection,
                    )
                    if obstacle_name and obstacle_bytes is not None
                    else None
                )
                radio_config = RadioConfig(
                    tx_eirp_dbm=float(tx_eirp),
                    device_antenna_gain_dbi=float(device_antenna_gain),
                    device_installation_loss_db=float(device_installation_loss),
                    gateway_gain_dbi=float(gateway_gain),
                    gateway_cable_loss_db=float(cable_loss),
                    gateway_tx_eirp_dbm=float(gateway_tx_eirp),
                    device_receiver_sensitivity_dbm=float(device_sensitivity),
                    validate_downlink=bool(validate_downlink),
                    target_sf=target_sf,
                    sf_sensitivities_dbm=tuple(
                        float(sf_sensitivities[sf_label])
                        for sf_label in SF_ORDER
                    ),
                    path_loss_exponent=float(path_loss_exponent),
                    additional_loss_db=float(additional_loss),
                    fade_margin_db=float(fade_margin),
                    obstacle_loss_db=(
                        float(obstacle_loss) if coverage_obstacles is not None else 0.0
                    ),
                    maximum_obstacle_loss_db=float(maximum_obstacle_loss),
                )
                antenna_config = AntennaConfig(
                    antenna_type=antenna_name,
                    gain_dbi=float(gateway_gain),
                    horizontal_beamwidth_deg=float(horizontal_beamwidth),
                    vertical_beamwidth_deg=float(vertical_beamwidth),
                    max_attenuation_db=float(max_antenna_attenuation),
                    downtilt_deg=float(downtilt),
                    gateway_height_m=float(gateway_height),
                    device_height_m=float(device_height),
                )
                coverage_plan = cached_plan_coverage(
                    coverage_geometry,
                    radio_config,
                    antenna=antenna_config,
                    redundancy=int(redundancy),
                    require_hpbw_redundancy=bool(require_hpbw_redundancy),
                    resolution_m=float(resolution_m),
                    minimum_site_separation_m=float(minimum_site_separation),
                    edge_priority=float(edge_priority),
                    dispersion_weight=float(dispersion_weight),
                    obstacles=coverage_obstacles,
                )
                if analyze_path_loss_range:
                    for scenario_name, scenario_exponent in path_loss_exponent_scenarios(
                        path_loss_exponent,
                        path_loss_variation,
                    ):
                        scenario_plan = (
                            coverage_plan
                            if scenario_name == "Base"
                            else cached_plan_coverage(
                                coverage_geometry,
                                replace(
                                    radio_config,
                                    path_loss_exponent=float(scenario_exponent),
                                ),
                                antenna=antenna_config,
                                redundancy=int(redundancy),
                                require_hpbw_redundancy=bool(
                                    require_hpbw_redundancy
                                ),
                                resolution_m=float(resolution_m),
                                minimum_site_separation_m=float(
                                    minimum_site_separation
                                ),
                                edge_priority=float(edge_priority),
                                dispersion_weight=float(dispersion_weight),
                                obstacles=coverage_obstacles,
                            )
                        )
                        coverage_sensitivity_plans.append(
                            (scenario_name, float(scenario_exponent), scenario_plan)
                        )
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Superficie", f"{coverage_plan.area_m2 / 1_000_000:.2f} km²")
                m2.metric("Radio máximo en boresight", f"{coverage_plan.radius_m:.0f} m")
                m3.metric("Gateways por cobertura", len(coverage_plan.selected_points))
                m4.metric("Puntos con redundancia", f"{coverage_plan.coverage_fraction:.1%}")
                st.caption(
                    f"Modelo: una antena {antenna_name} por gateway, ganancia {gateway_gain:.1f} dBi, "
                    f"HPBW {horizontal_beamwidth:.0f}° × {vertical_beamwidth:.0f}° y downtilt {downtilt:.0f}°."
                    f" Referencia isotrópica 0 dBi: {coverage_plan.isotropic_radius_m:.0f} m."
                    f" Verificación: {len(coverage_plan.evaluation_points)} puntos, incluyendo "
                    f"{coverage_plan.boundary_point_count} puntos específicos de perímetro. "
                    f"Paso interno efectivo: {coverage_plan.effective_resolution_m:.0f} m. "
                    f"Enlace exigido: {'uplink + downlink' if validate_downlink else 'solo uplink'}. "
                    f"Redundancia: {'dentro del HPBW horizontal' if require_hpbw_redundancy else 'por link budget, incluidos lóbulos laterales'}."
                )
                if coverage_obstacles is not None:
                    st.caption(
                        f"Obstáculos: {len(coverage_obstacles.polygons)} bloques, "
                        f"{obstacle_loss:.1f} dB por cruce, máximo {maximum_obstacle_loss:.1f} dB."
                    )

                for warning in coverage_plan.warnings:
                    st.warning(warning)

                with st.expander("Comparar perfiles de antena en este polígono"):
                    st.caption(
                        "La comparación mantiene ambiente, redundancia, EIRP, alturas y resolución. "
                        "Cada perfil usa su ganancia, HPBW y downtilt iniciales. Ejecuta tres "
                        "optimizaciones adicionales."
                    )
                    run_antenna_comparison = st.checkbox(
                        "Calcular comparación de antenas",
                        value=False,
                        key="run_antenna_comparison",
                    )
                    comparison_rows = []
                    comparison_names = (
                        (
                            "Omnidireccional",
                            "Sectorial 60° × 35°",
                            "Direccional 30° × 30°",
                        )
                        if run_antenna_comparison
                        else ()
                    )
                    for comparison_name in comparison_names:
                        comparison_preset = ANTENNA_PRESETS[comparison_name]
                        comparison_antenna = AntennaConfig(
                            antenna_type=comparison_name,
                            gain_dbi=float(comparison_preset["gain_dbi"]),
                            horizontal_beamwidth_deg=float(
                                comparison_preset["horizontal_beamwidth_deg"]
                            ),
                            vertical_beamwidth_deg=float(
                                comparison_preset["vertical_beamwidth_deg"]
                            ),
                            max_attenuation_db=float(
                                comparison_preset["max_attenuation_db"]
                            ),
                            downtilt_deg=float(comparison_preset["downtilt_deg"]),
                            gateway_height_m=float(gateway_height),
                            device_height_m=float(device_height),
                        )
                        comparison_radio = RadioConfig(
                            tx_eirp_dbm=float(tx_eirp),
                            device_antenna_gain_dbi=float(device_antenna_gain),
                            device_installation_loss_db=float(device_installation_loss),
                            gateway_gain_dbi=comparison_antenna.gain_dbi,
                            gateway_cable_loss_db=float(cable_loss),
                            gateway_tx_eirp_dbm=float(gateway_tx_eirp),
                            device_receiver_sensitivity_dbm=float(device_sensitivity),
                            validate_downlink=bool(validate_downlink),
                            target_sf=target_sf,
                            sf_sensitivities_dbm=tuple(
                                float(sf_sensitivities[sf_label])
                                for sf_label in SF_ORDER
                            ),
                            path_loss_exponent=float(path_loss_exponent),
                            additional_loss_db=float(additional_loss),
                            fade_margin_db=float(fade_margin),
                            obstacle_loss_db=(
                                float(obstacle_loss)
                                if coverage_obstacles is not None
                                else 0.0
                            ),
                            maximum_obstacle_loss_db=float(maximum_obstacle_loss),
                        )
                        comparison_plan = cached_plan_coverage(
                            coverage_geometry,
                            comparison_radio,
                            antenna=comparison_antenna,
                            redundancy=int(redundancy),
                            require_hpbw_redundancy=bool(require_hpbw_redundancy),
                            resolution_m=float(resolution_m),
                            minimum_site_separation_m=float(minimum_site_separation),
                            edge_priority=float(edge_priority),
                            dispersion_weight=float(dispersion_weight),
                            obstacles=coverage_obstacles,
                        )
                        comparison_rows.append(
                            {
                                "Antena": comparison_name,
                                "Ganancia (dBi)": comparison_antenna.gain_dbi,
                                "HPBW H × V": (
                                    f"{comparison_antenna.horizontal_beamwidth_deg:.0f}° × "
                                    f"{comparison_antenna.vertical_beamwidth_deg:.0f}°"
                                ),
                                "Radio boresight (m)": round(comparison_plan.radius_m),
                                "Gateways cobertura": len(comparison_plan.selected_points),
                                "Redundancia lograda": f"{comparison_plan.coverage_fraction:.1%}",
                            }
                        )
                    if comparison_rows:
                        st.dataframe(
                            pd.DataFrame(comparison_rows),
                            use_container_width=True,
                            hide_index=True,
                        )
                    else:
                        st.info("Activa la comparación solo cuando necesites evaluar alternativas.")

                use_coverage_distribution = st.checkbox(
                    "Usar la distribución SF derivada de la cobertura en el cálculo de capacidad",
                    value=True,
                    key="input_use_coverage_distribution",
                )
                if use_coverage_distribution:
                    distribucion = coverage_plan.sf_distribution
                    st.dataframe(
                        pd.DataFrame(
                            [{"SF": sf, "Distribución RF": distribucion[sf]} for sf in SF_ORDER]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )
            except Exception as coverage_exc:
                st.error(f"No fue posible procesar la cobertura RF: {coverage_exc}")

    with st.expander("📋 Tabla de mapeo DR AU915 usada por el modelo"):
        tabla_dr = pd.DataFrame(
            {
                "SF uplink (DR)": ["SF12 (DR0)", "SF11 (DR1)", "SF10 (DR2)", "SF9 (DR3)", "SF8 (DR4)", "SF7 (DR5)"],
                "BW uplink": ["125 kHz"] * 6,
                "SF ACK RX1 (DR)": ["SF12 (DR8)", "SF11 (DR9)", "SF10 (DR10)", "SF9 (DR11)", "SF8 (DR12)", "SF7 (DR13)"],
                "BW ACK RX1": ["500 kHz"] * 6,
                "ACK RX2": ["SF12 / DR8 @ 500 kHz"] * 6,
            }
        )
        st.dataframe(tabla_dr, use_container_width=True, hide_index=True)

    try:
        def run_capacity_estimate(distribution):
            return estimar_gateways(
                nodos_totales=int(nodos),
                mensajes_por_nodo_por_hora=float(mensajes_hora),
                eficiencia_aloha_uplink=float(eficiencia_aloha),
                canales_uplink_por_gateway=int(canales_ul),
                factor_seguridad=float(factor_seguridad),
                distribucion_sf=distribution,
                payload_uplink_bytes=int(payload_ul),
                confirmed_ratio=float(confirmed_ratio),
                ack_payload_bytes=int(ack_payload),
                rx2_fallback_ratio=float(rx2_fallback),
                eficiencia_downlink_ack=float(eficiencia_ack),
                max_blocking_rx=float(max_blocking_rx),
                retransmission_factor=float(retransmission_factor),
                fopts_uplink_bytes=int(fopts_uplink),
                fopts_downlink_bytes=int(fopts_downlink),
                uplink_dwell_time_enabled=bool(uplink_dwell_time_enabled),
            )

        df, resumen, advertencias = run_capacity_estimate(distribucion)

        if advertencias:
            st.subheader("⚠️ Advertencias")
            for adv in advertencias:
                st.warning(adv)

        st.subheader("Resultado de dimensionamiento")
        gateways_capacidad = resumen["gateways_recomendados"]
        gateways_cobertura = (
            len(coverage_plan.selected_points) if coverage_plan is not None else 0
        )
        gateways_finales = max(gateways_capacidad, gateways_cobertura, 1)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Gateways finales recomendados", gateways_finales)
        c2.metric("Gateways por capacidad", gateways_capacidad)
        c3.metric("Gateways por cobertura", gateways_cobertura if coverage_plan else "sin polígono")
        c4.metric("Condición dominante", "cobertura" if gateways_cobertura > gateways_capacidad else resumen["cuello_botella"])

        if coverage_sensitivity_plans:
            for scenario_name, scenario_exponent, scenario_plan in coverage_sensitivity_plans:
                scenario_distribution = (
                    scenario_plan.sf_distribution
                    if use_coverage_distribution
                    else traffic_distribution
                )
                scenario_summary = (
                    resumen
                    if scenario_name == "Base"
                    else run_capacity_estimate(scenario_distribution)[1]
                )
                scenario_capacity = int(scenario_summary["gateways_recomendados"])
                scenario_coverage = len(scenario_plan.selected_points)
                scenario_final = max(scenario_capacity, scenario_coverage, 1)
                path_loss_sensitivity_rows.append(
                    {
                        "Escenario": scenario_name,
                        "Exponente n": round(scenario_exponent, 2),
                        "Radio boresight (m)": round(scenario_plan.radius_m),
                        "Gateways capacidad": scenario_capacity,
                        "Gateways cobertura": scenario_coverage,
                        "Gateways finales": scenario_final,
                        "Cobertura robusta": f"{scenario_plan.coverage_fraction:.1%}",
                        "Estado": (
                            "Resuelto"
                            if scenario_plan.coverage_fraction >= 0.999
                            else "Incompleto"
                        ),
                    }
                )

            base_final = next(
                row["Gateways finales"]
                for row in path_loss_sensitivity_rows
                if row["Escenario"] == "Base"
            )
            for row in path_loss_sensitivity_rows:
                row["Variación vs base"] = row["Gateways finales"] - base_final

            st.subheader("Rango por incertidumbre del exponente de pérdida")
            display_sensitivity_rows = []
            for row in path_loss_sensitivity_rows:
                display_row = dict(row)
                if row["Estado"] != "Resuelto":
                    display_row["Gateways finales"] = (
                        f"{row['Gateways finales']} usados (incompleto)"
                    )
                    display_row["Variación vs base"] = "—"
                display_sensitivity_rows.append(display_row)
            st.dataframe(
                pd.DataFrame(display_sensitivity_rows),
                use_container_width=True,
                hide_index=True,
            )
            resolved_scenarios = [
                row for row in path_loss_sensitivity_rows if row["Estado"] == "Resuelto"
            ]
            incomplete_scenarios = [
                row for row in path_loss_sensitivity_rows if row["Estado"] != "Resuelto"
            ]
            base_row = next(
                row for row in path_loss_sensitivity_rows if row["Escenario"] == "Base"
            )
            if resolved_scenarios:
                resolved_minimum = min(
                    row["Gateways finales"] for row in resolved_scenarios
                )
                resolved_maximum = max(
                    row["Gateways finales"] for row in resolved_scenarios
                )
                base_text = (
                    str(base_row["Gateways finales"])
                    if base_row["Estado"] == "Resuelto"
                    else "no resuelto"
                )
                st.info(
                    f"Rango de diseños cerrados: {resolved_minimum}–{resolved_maximum} gateways; "
                    f"valor base: {base_text}. Solo se incluyen escenarios que alcanzan toda "
                    "la redundancia exigida."
                )
            if incomplete_scenarios:
                incomplete_description = "; ".join(
                    f"{row['Escenario']}: {row['Gateways finales']} gateways usados, "
                    f"{row['Cobertura robusta']} de cobertura robusta"
                    for row in incomplete_scenarios
                )
                st.warning(
                    "Escenario sin solución completa — "
                    + incomplete_description
                    + ". No se incluye como límite superior del rango. Revise separación mínima, "
                    "resolución, HPBW o ubicaciones permitidas antes de adoptar una cantidad."
                )

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Gateways por uplink (cuello uplink)", resumen["gateways_por_uplink"])
        c6.metric("Gateways por ACK airtime", resumen["gateways_por_airtime_ack"], help="No se suma con bloqueo RX; se usa el máximo.")
        c7.metric("Gateways por ACK blocking", resumen["gateways_por_blocking"], help="No se suma con airtime ACK; se usa el máximo.")
        c8.metric("Airtime DL disponible (s/h)", resumen["airtime_dl_disponible_s_hora"])

        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Descargar CSV", csv, "estimacion_gateways_au915.csv", "text/csv")

        sites_df = None
        if coverage_plan is not None and coverage_geometry is not None:
            st.subheader("Ubicaciones preliminares de gateways")
            final_sites, final_azimuths = augment_gateway_deployments(
                coverage_plan, gateways_finales
            )
            if len(final_sites) < gateways_finales:
                st.warning(
                    f"El modelo requiere {gateways_finales} gateways, pero solo pudo ubicar "
                    f"{len(final_sites)} sitios distintos respetando una separación mínima de "
                    f"{minimum_site_separation:.0f} m. Reduzca la separación, aumente la "
                    "resolución espacial de candidatos o habilite ubicaciones adicionales."
                )
            lon_lat_sites = points_to_lon_lat(final_sites, coverage_geometry.projection)
            final_link_analysis = deployment_link_analysis(
                coverage_plan, final_sites, final_azimuths
            )
            final_coverage_counts = [
                int(item["count"]) for item in final_link_analysis
            ]
            final_radio_counts = [
                int(item["radio_count"]) for item in final_link_analysis
            ]
            covered_evaluation_points = sum(
                count >= coverage_plan.redundancy
                for count in final_coverage_counts
            )
            partially_covered_evaluation_points = sum(
                0 < count < coverage_plan.redundancy
                for count in final_coverage_counts
            )
            uncovered_evaluation_points = sum(
                count == 0 for count in final_coverage_counts
            )
            radio_covered_evaluation_points = sum(
                count >= coverage_plan.redundancy
                for count in final_radio_counts
            )
            final_coverage_fraction = (
                covered_evaluation_points / len(final_coverage_counts)
                if final_coverage_counts
                else 0.0
            )
            final_radio_coverage_fraction = (
                radio_covered_evaluation_points / len(final_radio_counts)
                if final_radio_counts
                else 0.0
            )
            all_surpluses = sorted(
                float(item["design_surplus_db"])
                for item in final_link_analysis
            )
            minimum_surplus = all_surpluses[0] if all_surpluses else -math.inf
            percentile_10_index = (
                min(int((len(all_surpluses) - 1) * 0.10), len(all_surpluses) - 1)
                if all_surpluses
                else 0
            )
            percentile_10_surplus = (
                all_surpluses[percentile_10_index]
                if all_surpluses
                else -math.inf
            )
            verification_col1, verification_col2, verification_col3, verification_col4, verification_col5, verification_col6 = st.columns(6)
            verification_col1.metric(
                "Cobertura final verificada",
                f"{final_coverage_fraction:.1%}",
            )
            verification_col2.metric(
                "Puntos que cumplen redundancia",
                f"{covered_evaluation_points}/{len(final_coverage_counts)}",
            )
            verification_col3.metric(
                "Redundancia exigida",
                f"{coverage_plan.redundancy} gateways",
            )
            verification_col4.metric(
                "RF incl. laterales",
                f"{final_radio_coverage_fraction:.1%}",
                help="Cobertura con link budget suficiente aunque algún enlace quede fuera del HPBW principal.",
            )
            verification_col5.metric(
                "Margen mínimo vs diseño",
                f"{minimum_surplus:.1f} dB" if math.isfinite(minimum_surplus) else "sin enlace",
                help="Margen del gateway que completa la redundancia, después de descontar el margen de diseño.",
            )
            verification_col6.metric(
                "Margen P10 vs diseño",
                f"{percentile_10_surplus:.1f} dB" if math.isfinite(percentile_10_surplus) else "sin enlace",
            )
            st.caption(
                f"Desglose de la malla interna: {covered_evaluation_points} puntos cumplen "
                f"{coverage_plan.redundancy}×; {partially_covered_evaluation_points} tienen "
                f"cobertura parcial y {uncovered_evaluation_points} no tienen cobertura robusta."
            )
            finite_rank_links = [
                item
                for item in final_link_analysis
                if math.isfinite(float(item["limiting_margin_db"]))
            ]
            if validate_downlink and finite_rank_links:
                downlink_bottlenecks = sum(
                    float(item["downlink_margin_db"])
                    <= float(item["uplink_margin_db"])
                    for item in finite_rank_links
                )
                st.caption(
                    f"En {downlink_bottlenecks / len(finite_rank_links):.1%} de los puntos "
                    "el enlace limitante es el downlink; en el resto es el uplink."
                )
            if final_coverage_fraction >= 0.999:
                st.success(
                    "Todos los puntos de evaluación del polígono cumplen la redundancia requerida."
                )
            else:
                st.error(
                    f"Hay {len(final_coverage_counts) - covered_evaluation_points} puntos de evaluación "
                    "que todavía no cumplen la redundancia robusta. Las zonas dependientes de lóbulos "
                    "laterales se muestran en amarillo y las que no tienen redundancia RF, en rojo."
                )
            sites_df = pd.DataFrame(
                [
                    {
                        "gateway": f"GW-{index:02d}",
                        "longitude": lon,
                        "latitude": lat,
                        "antena": coverage_plan.antenna_config.antenna_type,
                        "ganancia_dbi": coverage_plan.antenna_config.gain_dbi,
                        "azimuth_deg": round(final_azimuths[index - 1], 1),
                        "downtilt_deg": coverage_plan.antenna_config.downtilt_deg,
                    }
                    for index, (lon, lat) in enumerate(lon_lat_sites, start=1)
                ]
            )
            st.map(sites_df, latitude="latitude", longitude="longitude", size=60)
            st.dataframe(sites_df, use_container_width=True, hide_index=True)
            if plt is not None:
                display_col1, display_col2 = st.columns(2)
                with display_col1:
                    show_coverage_points = st.checkbox(
                        "Mostrar verificación de cobertura por puntos",
                        value=True,
                    )
                with display_col2:
                    show_isotropic_reference = st.checkbox(
                        "Mostrar referencia isotrópica 0 dBi",
                        value=True,
                        disabled=coverage_plan.antenna_config.is_omnidirectional,
                    )
                fig_cov, ax_cov = plt.subplots(figsize=(11, 7))
                footprint_color = "tab:blue"
                for polygon in coverage_geometry.polygons:
                    for ring_index, ring in enumerate(polygon):
                        xs = [point[0] for point in ring]
                        ys = [point[1] for point in ring]
                        ax_cov.plot(
                            xs,
                            ys,
                            color="black" if ring_index == 0 else "gray",
                            linewidth=1.5,
                        )
                if coverage_plan.obstacles is not None:
                    for obstacle_polygon in coverage_plan.obstacles.polygons:
                        outer = obstacle_polygon[0]
                        ax_cov.fill(
                            [point[0] for point in outer],
                            [point[1] for point in outer],
                            facecolor="tab:orange",
                            edgecolor="darkorange",
                            linewidth=0.8,
                            alpha=0.22,
                            hatch="///",
                            zorder=1,
                        )
                for point, azimuth in zip(final_sites, final_azimuths):
                    if coverage_plan.antenna_config.is_omnidirectional:
                        footprint = Circle(
                            point,
                            coverage_plan.radius_m,
                            facecolor=footprint_color,
                            edgecolor=footprint_color,
                            linewidth=1.0,
                            alpha=0.10,
                            zorder=1,
                        )
                    else:
                        matplotlib_angle = 90.0 - azimuth
                        half_beamwidth = (
                            coverage_plan.antenna_config.horizontal_beamwidth_deg / 2
                        )
                        footprint = Wedge(
                            point,
                            coverage_plan.radius_m,
                            matplotlib_angle - half_beamwidth,
                            matplotlib_angle + half_beamwidth,
                            facecolor=footprint_color,
                            edgecolor=footprint_color,
                            linewidth=1.0,
                            alpha=0.12,
                            zorder=1,
                        )
                    ax_cov.add_patch(footprint)
                    if (
                        show_isotropic_reference
                        and not coverage_plan.antenna_config.is_omnidirectional
                    ):
                        ax_cov.add_patch(
                            Circle(
                                point,
                                coverage_plan.isotropic_radius_m,
                                fill=False,
                                edgecolor="tab:purple",
                                linestyle="--",
                                linewidth=0.8,
                                alpha=0.45,
                                zorder=1,
                            )
                        )
                if show_coverage_points:
                    compliant_points = [
                        point
                        for point, count in zip(
                            coverage_plan.evaluation_points,
                            final_coverage_counts,
                        )
                        if count >= coverage_plan.redundancy
                    ]
                    partial_points = [
                        point
                        for point, robust_count in zip(
                            coverage_plan.evaluation_points,
                            final_coverage_counts,
                        )
                        if 0 < robust_count < coverage_plan.redundancy
                    ]
                    uncovered_points = [
                        point
                        for point, robust_count in zip(
                            coverage_plan.evaluation_points,
                            final_coverage_counts,
                        )
                        if robust_count == 0
                    ]
                    lateral_fallback_points = [
                        point
                        for point, robust_count, radio_count in zip(
                            coverage_plan.evaluation_points,
                            final_coverage_counts,
                            final_radio_counts,
                        )
                        if robust_count < coverage_plan.redundancy
                        and radio_count >= coverage_plan.redundancy
                    ]
                    if compliant_points:
                        ax_cov.scatter(
                            [point[0] for point in compliant_points],
                            [point[1] for point in compliant_points],
                            color="tab:green",
                            marker=".",
                            s=12,
                            alpha=0.45,
                            zorder=2,
                        )
                    if partial_points:
                        ax_cov.scatter(
                            [point[0] for point in partial_points],
                            [point[1] for point in partial_points],
                            color="darkorange",
                            marker="o",
                            s=18,
                            alpha=0.75,
                            zorder=3,
                        )
                    if uncovered_points:
                        ax_cov.scatter(
                            [point[0] for point in uncovered_points],
                            [point[1] for point in uncovered_points],
                            color="tab:red",
                            marker="x",
                            s=24,
                            linewidth=0.8,
                            zorder=4,
                        )
                    if lateral_fallback_points:
                        ax_cov.scatter(
                            [point[0] for point in lateral_fallback_points],
                            [point[1] for point in lateral_fallback_points],
                            facecolors="none",
                            edgecolors="goldenrod",
                            marker="o",
                            s=34,
                            linewidth=0.8,
                            zorder=4,
                        )
                ax_cov.scatter(
                    [point[0] for point in final_sites],
                    [point[1] for point in final_sites],
                    color="tab:red",
                    marker="^",
                    s=70,
                    label="Gateway",
                )
                if not coverage_plan.antenna_config.is_omnidirectional:
                    for point, azimuth in zip(final_sites, final_azimuths):
                        desired_arrow_length = coverage_plan.radius_m * 0.28
                        arrow_length = ray_length_within_geometry(
                            coverage_geometry,
                            point,
                            azimuth,
                            desired_arrow_length,
                        ) * 0.92
                        if arrow_length < max(coverage_plan.radius_m * 0.03, 5):
                            continue
                        azimuth_rad = math.radians(azimuth)
                        ax_cov.arrow(
                            point[0],
                            point[1],
                            math.sin(azimuth_rad) * arrow_length,
                            math.cos(azimuth_rad) * arrow_length,
                            width=max(coverage_plan.radius_m * 0.006, 1),
                            head_width=max(coverage_plan.radius_m * 0.04, 5),
                            color="tab:red",
                            alpha=0.65,
                            length_includes_head=True,
                        )
                ax_cov.set_aspect("equal", adjustable="box")
                min_x, min_y, max_x, max_y = coverage_geometry.bounds
                map_span = max(max_x - min_x, max_y - min_y, 1.0)
                map_padding = map_span * 0.06
                ax_cov.set_xlim(min_x - map_padding, max_x + map_padding)
                ax_cov.set_ylim(min_y - map_padding, max_y + map_padding)
                ax_cov.set_title(
                    "Huellas nominales y verificación de cobertura "
                    f"({final_coverage_fraction:.1%} con redundancia {coverage_plan.redundancy})"
                )
                ax_cov.set_xlabel("Este local (m)")
                ax_cov.set_ylabel("Norte local (m)")
                ax_cov.grid(True, alpha=0.25)
                legend_handles = [
                    Line2D(
                        [0], [0], marker="^", color="none", markerfacecolor="tab:red",
                        markeredgecolor="tab:red", markersize=8, label="Gateway",
                    ),
                    Patch(
                        facecolor=footprint_color,
                        edgecolor=footprint_color,
                        alpha=0.15,
                        label=(
                            "Cobertura circular nominal"
                            if coverage_plan.antenna_config.is_omnidirectional
                            else "Cono HPBW nominal"
                        ),
                    ),
                ]
                if show_coverage_points:
                    legend_handles.extend(
                        [
                            Line2D(
                                [0], [0], marker=".", color="tab:green", linestyle="none",
                                markersize=8,
                                label=(
                                    f"Cobertura robusta {coverage_plan.redundancy}× HPBW"
                                    if coverage_plan.require_hpbw_redundancy
                                    else "Cumple redundancia RF"
                                ),
                            ),
                            Line2D(
                                [0], [0], marker="o", color="darkorange", linestyle="none",
                                markersize=5,
                                label=(
                                    "Cobertura parcial: solo 1 gateway"
                                    if coverage_plan.redundancy == 2
                                    else "Cobertura menor que la redundancia exigida"
                                ),
                            ),
                            Line2D(
                                [0], [0], marker="x", color="tab:red", linestyle="none",
                                markersize=6, label="Sin cobertura robusta",
                            ),
                            Line2D(
                                [0], [0], marker="o", color="goldenrod",
                                markerfacecolor="none", linestyle="none",
                                markersize=6, label="RF suficiente solo fuera del HPBW",
                            ),
                        ]
                    )
                if (
                    show_isotropic_reference
                    and not coverage_plan.antenna_config.is_omnidirectional
                ):
                    legend_handles.append(
                        Line2D(
                            [0], [0], color="tab:purple", linestyle="--",
                            linewidth=1, label="Referencia isotrópica 0 dBi",
                        )
                    )
                if coverage_plan.obstacles is not None:
                    legend_handles.append(
                        Patch(
                            facecolor="tab:orange",
                            edgecolor="darkorange",
                            alpha=0.25,
                            hatch="///",
                            label="Bloques de contenedores",
                        )
                    )
                ax_cov.legend(
                    handles=legend_handles,
                    fontsize=8,
                    loc="upper left",
                    bbox_to_anchor=(1.02, 1.0),
                    borderaxespad=0.0,
                )
                fig_cov.subplots_adjust(right=0.72)
                coverage_map_buffer = BytesIO()
                fig_cov.savefig(
                    coverage_map_buffer,
                    format="png",
                    dpi=110,
                    facecolor="white",
                )
                coverage_map_png = coverage_map_buffer.getvalue()
                coverage_map_png_base64 = base64.b64encode(
                    coverage_map_png
                ).decode("ascii")
                st.image(coverage_map_png, use_container_width=True)
                plt.close(fig_cov)
                st.caption(
                    "La figura azul representa la huella nominal al HPBW y alcance máximo en boresight. "
                    f"Verde significa {coverage_plan.redundancy} o más gateways dentro del HPBW; "
                    + (
                        "naranjo significa exactamente un gateway y rojo ninguno. "
                        if coverage_plan.redundancy == 2
                        else "naranjo significa cobertura parcial y rojo ninguno. "
                    )
                    + "El aro amarillo indica "
                    "que el link budget solo se completa usando señal fuera del haz principal. "
                    "La leyenda se muestra fuera del área evaluada."
                )

            sites_geojson = gateway_sites_geojson(
                final_sites,
                coverage_geometry.projection,
                final_azimuths,
                coverage_plan.antenna_config,
            )
            st.download_button(
                "Descargar sitios propuestos (GeoJSON)",
                json.dumps(sites_geojson, indent=2),
                "gateways_propuestos.geojson",
                "application/geo+json",
            )
            st.download_button(
                "Descargar sitios propuestos (CSV)",
                sites_df.to_csv(index=False),
                "gateways_propuestos.csv",
                "text/csv",
            )

        if plt is not None:
            df_plot = df[df["SF UL"].str.startswith("SF")].copy()
            fig, axes = plt.subplots(1, 3, figsize=(15, 5))
            fig.suptitle("AU915 — Análisis por Spreading Factor", fontsize=13)

            axes[0].bar(df_plot["SF UL"], df_plot["Carga uplink"])
            axes[0].set_title("Carga uplink")
            axes[0].set_ylabel("Carga relativa")
            axes[0].axhline(y=1.0, color="red", linestyle="--", linewidth=0.8)
            axes[0].grid(True, axis="y", linestyle="--", alpha=0.4)

            x = range(len(df_plot))
            w = 0.35
            axes[1].bar([i - w / 2 for i in x], df_plot["ToA ACK RX1 (s)"], width=w, label="RX1")
            axes[1].bar([i + w / 2 for i in x], df_plot["ToA ACK RX2 (s)"], width=w, label="RX2")
            axes[1].set_title("ToA ACK RX1 vs RX2")
            axes[1].set_xticks(list(x))
            axes[1].set_xticklabels(df_plot["SF UL"])
            axes[1].legend(fontsize=8)
            axes[1].grid(True, axis="y", linestyle="--", alpha=0.4)

            axes[2].bar(df_plot["SF UL"], df_plot["Airtime ACK s/h"])
            axes[2].set_title("Airtime ACK por SF")
            axes[2].set_ylabel("s/hora")
            axes[2].axhline(y=resumen["airtime_dl_disponible_s_hora"], color="red", linestyle="--", linewidth=0.8)
            axes[2].grid(True, axis="y", linestyle="--", alpha=0.4)

            plt.tight_layout()
            capacity_chart_buffer = BytesIO()
            fig.savefig(
                capacity_chart_buffer,
                format="png",
                dpi=110,
                facecolor="white",
            )
            capacity_chart_png = capacity_chart_buffer.getvalue()
            capacity_charts_png_base64 = base64.b64encode(
                capacity_chart_png
            ).decode("ascii")
            st.image(capacity_chart_png, use_container_width=True)
            plt.close(fig)

        scenario_parameters = {
            "Perfil operativo": perfil_operativo,
            "Nodos totales": int(nodos),
            "Mensajes por nodo por hora": float(mensajes_hora),
            "Payload aplicación uplink (bytes)": int(payload_ul),
            "Payload físico uplink (bytes)": resumen["payload_fisico_uplink_bytes"],
            "Canales uplink por gateway": int(canales_ul),
            "Eficiencia ALOHA uplink": float(eficiencia_aloha),
            "ADR": "Habilitado" if adr_enabled else "Deshabilitado",
            "Distribución SF": ", ".join(f"{sf}: {value:.1%}" for sf, value in distribucion.items()),
            "Uplinks confirmados": f"{confirmed_ratio:.1%}",
            "Payload aplicación ACK (bytes)": int(ack_payload),
            "ACK en RX2": f"{rx2_fallback:.1%}",
            "Factor retransmisiones": float(retransmission_factor),
            "Factor de seguridad": float(factor_seguridad),
        }
        scenario_coverage = None
        if coverage_plan is not None:
            scenario_coverage = {
                "Superficie": f"{coverage_plan.area_m2 / 1_000_000:.3f} km2",
                "Ambiente RF": environment_name,
                "Redundancia requerida": int(redundancy),
                "Criterio de redundancia": (
                    "Dentro del HPBW horizontal"
                    if require_hpbw_redundancy
                    else "Link budget incluyendo lóbulos laterales"
                ),
                "Separación mínima entre sitios": f"{minimum_site_separation:.0f} m",
                "Estrategia espacial": strategy_name,
                "Prioridad del perímetro": float(edge_priority),
                "Preferencia de dispersión": float(dispersion_weight),
                "Puntos de evaluación del perímetro": coverage_plan.boundary_point_count,
                "Redundancia lograda": f"{coverage_plan.coverage_fraction:.1%}",
                "SF máximo de diseño": target_sf,
                "Sensibilidad gateway objetivo": f"{radio_config.receiver_sensitivity_dbm:.1f} dBm",
                "Enlace exigido": "Uplink + downlink" if validate_downlink else "Solo uplink",
                "Potencia TX dispositivo": f"{tx_eirp:.1f} dBm",
                "Ganancia antena dispositivo": f"{device_antenna_gain:.1f} dBi",
                "Pérdida instalación/puerta": f"{device_installation_loss:.1f} dB",
                "EIRP downlink gateway": f"{gateway_tx_eirp:.1f} dBm",
                "Sensibilidad RX dispositivo": f"{device_sensitivity:.1f} dBm",
                "Radio máximo en boresight": f"{coverage_plan.radius_m:.0f} m",
                "Radio isotrópico de referencia (0 dBi)": f"{coverage_plan.isotropic_radius_m:.0f} m",
                "Cobertura final verificada": f"{final_coverage_fraction:.1%}",
                "Cobertura RF incluyendo lóbulos laterales": f"{final_radio_coverage_fraction:.1%}",
                "Tipo de antena": antenna_name,
                "Ganancia": f"{gateway_gain:.1f} dBi",
                "HPBW horizontal / vertical": f"{horizontal_beamwidth:.0f}° / {vertical_beamwidth:.0f}°",
                "Downtilt": f"{downtilt:.1f}°",
                "Altura gateway / dispositivo": f"{gateway_height:.1f} m / {device_height:.1f} m",
                "Pérdida adicional ambiente": f"{additional_loss:.1f} dB",
                "Variación evaluada del exponente": (
                    f"±{path_loss_variation:.1f}"
                    if analyze_path_loss_range
                    else "No evaluada"
                ),
                "Rango de gateways por exponente": (
                    "; ".join(
                        f"{row['Escenario']} n={row['Exponente n']}: "
                        f"{row['Gateways finales']} ({row['Estado'].lower()})"
                        for row in path_loss_sensitivity_rows
                    )
                    if path_loss_sensitivity_rows
                    else "No calculado"
                ),
                "Margen de desvanecimiento": f"{fade_margin:.1f} dB",
                "Margen mínimo vs diseño": (
                    f"{minimum_surplus:.1f} dB"
                    if math.isfinite(minimum_surplus)
                    else "Sin enlace redundante"
                ),
                "Margen P10 vs diseño": (
                    f"{percentile_10_surplus:.1f} dB"
                    if math.isfinite(percentile_10_surplus)
                    else "Sin enlace redundante"
                ),
                "Obstáculos": (
                    f"{len(coverage_plan.obstacles.polygons)} bloques, {obstacle_loss:.1f} dB/cruce"
                    if coverage_plan.obstacles is not None
                    else "No cargados"
                ),
            }

        current_snapshot = {
            "snapshot_version": 5,
            "name": "Estimación actual",
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "input_state": capture_estimation_input_state(st.session_state),
            "polygon": (
                {
                    "name": polygon_name,
                    "data_base64": base64.b64encode(polygon_bytes).decode("ascii"),
                }
                if polygon_name and polygon_bytes is not None
                else None
            ),
            "obstacles": (
                {
                    "name": obstacle_name,
                    "data_base64": base64.b64encode(obstacle_bytes).decode("ascii"),
                }
                if obstacle_name and obstacle_bytes is not None
                else None
            ),
            "parameters": scenario_parameters,
            "summary": {
                "gateways_finales": gateways_finales,
                "gateways_por_capacidad": gateways_capacidad,
                "gateways_por_cobertura": gateways_cobertura if coverage_plan else "Sin polígono",
                "condicion_dominante": "cobertura" if gateways_cobertura > gateways_capacidad else resumen["cuello_botella"],
                "gateways_por_uplink": resumen["gateways_por_uplink"],
                "gateways_por_airtime_ack": resumen["gateways_por_airtime_ack"],
                "gateways_por_blocking": resumen["gateways_por_blocking"],
                "airtime_dl_disponible_s_hora": resumen["airtime_dl_disponible_s_hora"],
            },
            "coverage": scenario_coverage,
            "warnings": list(advertencias) + (list(coverage_plan.warnings) if coverage_plan else []),
            "details": json.loads(df.to_json(orient="records")),
            "sites": json.loads(sites_df.to_json(orient="records")) if sites_df is not None else [],
            "figures": {
                "coverage_map_png_base64": coverage_map_png_base64,
                "capacity_charts_png_base64": capacity_charts_png_base64,
            },
        }

        st.divider()
        st.subheader("Guardar y compartir estimaciones")
        st.caption(
            "Los escenarios se guardan durante esta sesión. Al seleccionar uno se restauran sus "
            "controles y se recalcula. Descarga la cartera JSON para conservarlos y volver a "
            "importarlos posteriormente."
        )
        save_col, pdf_col = st.columns([2, 1])
        with save_col:
            scenario_name = st.text_input(
                "Nombre de esta estimación",
                placeholder="Ej.: Terminal norte - sectoriales 60° - alternativa A",
            )
            if st.button("Guardar estimación", type="primary", use_container_width=True):
                clean_name = scenario_name.strip()
                if not clean_name:
                    st.warning("Ingresa un nombre antes de guardar.")
                else:
                    saved_snapshot = dict(current_snapshot)
                    saved_snapshot["name"] = clean_name
                    saved_snapshot["saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    st.session_state.saved_estimations[clean_name] = saved_snapshot
                    st.success(f"Estimación '{clean_name}' guardada.")
        with pdf_col:
            current_report_name = scenario_name.strip() or "Estimación actual"
            report_snapshot = dict(current_snapshot)
            report_snapshot["name"] = current_report_name
            st.download_button(
                "Descargar PDF actual",
                build_pdf_report(report_snapshot),
                f"{safe_filename(current_report_name)}.pdf",
                "application/pdf",
                use_container_width=True,
            )

        saved_names = sorted(st.session_state.saved_estimations)
        if saved_names:
            st.markdown("#### Estimaciones guardadas")

            def load_selected_estimation():
                name = st.session_state.get("saved_estimation_selector")
                snapshot = st.session_state.saved_estimations.get(name)
                if not snapshot:
                    return
                restored = restorable_input_state(snapshot)
                for key, value in restored.items():
                    st.session_state[key] = value
                polygon = snapshot.get("polygon")
                if isinstance(polygon, dict) and polygon.get("data_base64"):
                    st.session_state["_active_polygon"] = polygon
                else:
                    st.session_state.pop("_active_polygon", None)
                st.session_state["_prefer_restored_polygon"] = True
                obstacles = snapshot.get("obstacles")
                if isinstance(obstacles, dict) and obstacles.get("data_base64"):
                    st.session_state["_active_obstacles"] = obstacles
                else:
                    st.session_state.pop("_active_obstacles", None)
                st.session_state["_prefer_restored_obstacles"] = True
                if snapshot.get("input_state"):
                    st.session_state["_refresh_saved_estimation_name"] = name
                    st.session_state["_loaded_estimation_notice"] = (
                        f"Estimación '{name}' cargada y recalculada con sus parámetros guardados."
                    )
                else:
                    st.session_state["_loaded_estimation_notice"] = (
                        f"Estimación antigua '{name}' cargada parcialmente. Guárdala nuevamente "
                        "para conservar todos los controles y el polígono."
                    )

            selected_name = st.selectbox(
                "Seleccionar estimación",
                saved_names,
                key="saved_estimation_selector",
                on_change=load_selected_estimation,
            )
            selected_snapshot = st.session_state.saved_estimations[selected_name]
            refresh_saved_name = st.session_state.pop("_refresh_saved_estimation_name", None)
            if refresh_saved_name == selected_name:
                refreshed_figures = current_snapshot.get("figures") or {}
                if any(refreshed_figures.values()):
                    refreshed_snapshot = dict(selected_snapshot)
                    refreshed_snapshot["snapshot_version"] = current_snapshot.get(
                        "snapshot_version", 5
                    )
                    refreshed_snapshot["figures"] = dict(refreshed_figures)
                    st.session_state.saved_estimations[selected_name] = refreshed_snapshot
                    selected_snapshot = refreshed_snapshot

            saved_summary = selected_snapshot.get("summary", {})
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Nombre": selected_name,
                            "Guardada": selected_snapshot.get("saved_at", ""),
                            "Gateways finales": saved_summary.get("gateways_finales", ""),
                            "Capacidad": saved_summary.get("gateways_por_capacidad", ""),
                            "Cobertura": saved_summary.get("gateways_por_cobertura", ""),
                            "Dominante": saved_summary.get("condicion_dominante", ""),
                        }
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
            load_col, saved_pdf_col, portfolio_col, delete_col = st.columns(4)
            with load_col:
                st.button(
                    "Cargar seleccionada",
                    on_click=load_selected_estimation,
                    use_container_width=True,
                    type="primary",
                )
            with saved_pdf_col:
                saved_figures = selected_snapshot.get("figures") or {}
                if not any(saved_figures.values()):
                    st.caption("Cárgala antes de exportar para incorporar sus mapas y gráficos.")
                st.download_button(
                    "Descargar PDF guardado",
                    build_pdf_report(selected_snapshot),
                    f"{safe_filename(selected_name)}.pdf",
                    "application/pdf",
                    use_container_width=True,
                )
            with portfolio_col:
                st.download_button(
                    "Descargar cartera JSON",
                    scenarios_json(st.session_state.saved_estimations),
                    "estimaciones_gateways.json",
                    "application/json",
                    use_container_width=True,
                )
            with delete_col:
                def delete_selected_estimation():
                    name = st.session_state.get("saved_estimation_selector")
                    st.session_state.saved_estimations.pop(name, None)
                    remaining = sorted(st.session_state.saved_estimations)
                    if remaining:
                        st.session_state["saved_estimation_selector"] = remaining[0]
                    else:
                        st.session_state.pop("saved_estimation_selector", None)

                st.button(
                    "Eliminar seleccionada",
                    on_click=delete_selected_estimation,
                    use_container_width=True,
                )

        with st.expander("Importar estimaciones guardadas"):
            portfolio_file = st.file_uploader(
                "Archivo de cartera JSON",
                type=["json"],
                key="scenario_portfolio_upload",
            )
            if st.button("Importar cartera", disabled=portfolio_file is None):
                try:
                    imported = load_scenarios_json(portfolio_file.getvalue())
                    st.session_state.saved_estimations.update(imported)
                    st.success(f"Se importaron {len(imported)} estimaciones.")
                    st.rerun()
                except Exception as import_exc:
                    st.error(f"No fue posible importar la cartera: {import_exc}")

        with st.expander("📖 Interpretación"):
            st.markdown(
                f"""
**ADR OFF:** se usa 100% de nodos en el DR/SF fijo seleccionado.

**ADR ON:** se usan perfiles de distribución SF porque ADR puede mover nodos entre data rates.

**Resultado final:** se toma el máximo entre capacidad, cobertura geográfica y redundancia.

- Payload físico uplink modelado: **{resumen['payload_fisico_uplink_bytes']} bytes**
- Payload físico ACK modelado: **{resumen['payload_fisico_ack_bytes']} bytes**
- Airtime ACK consumido: **{resumen['airtime_ack_total_s_hora']} s/hora**
- Airtime ACK disponible: **{resumen['airtime_dl_disponible_s_hora']} s/hora**
- Bloqueo RX total equivalente: **{resumen['blocking_total']}**
- ACK/downlink attempts hora: **{resumen['ack_attempts_hora_total']}**
                """
            )

    except Exception as exc:
        st.error(f"Error en la estimación: {exc}")
        raise


if __name__ == "__main__":
    if st is None:
        print("Streamlit no está instalado.")
        print("Instalar: pip install streamlit pandas matplotlib openpyxl")
        print("Ejecutar: streamlit run gateway_estimation_au915.py")
    else:
        app_streamlit()

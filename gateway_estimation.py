import json
import math
from datetime import datetime

import pandas as pd

from coverage_model import (
    ANTENNA_PRESETS,
    ENVIRONMENT_PRESETS,
    AntennaConfig,
    RadioConfig,
    augment_gateway_deployments,
    gateway_sites_geojson,
    parse_polygon_file,
    plan_coverage,
    points_to_lon_lat,
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
except ImportError:
    plt = None

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
            "ToA UL (s)": "",
            "ToA UL (ms)": "",
            "Dwell OK UL": "",
            "Carga uplink": round(carga_uplink_total, 3),
            "SF DL RX1": "",
            "ToA ACK RX1 (s)": "",
            "ToA ACK RX2 (s)": "",
            "ToA ACK pond. (s)": "",
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
    st.caption(
        "ADR ON/OFF · DR fijo sin ADR · perfiles con ADR · ACK downlink 500 kHz · RX2 DR8 SF12"
    )

    with st.sidebar:
        st.header("Perfil operativo")
        perfil_operativo = st.selectbox(
            "Preset de entorno",
            options=["Personalizado", "Terminal Contenedores"],
            index=0,
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
            help="Cantidad total de dispositivos LoRaWAN activos en la red. A mayor cantidad de nodos, mayor carga uplink y mayor cantidad de ACK/downlink.",
        )
        mensajes_hora = st.number_input(
            "Mensajes por nodo por hora",
            min_value=0.01,
            value=1.0,
            step=0.25,
            help="Frecuencia de transmisión de cada nodo. Subir este valor incrementa linealmente uplinks, ACK y airtime total.",
        )
        payload_ul = st.slider(
            "Payload de aplicación uplink (bytes)",
            min_value=1,
            max_value=250,
            value=10,
            help="Solo FRMPayload. El estimador añade automáticamente los 13 bytes mínimos de LoRaWAN.",
        )
        canales_ul = st.number_input(
            "Canales uplink por gateway",
            min_value=1,
            max_value=64,
            value=8,
            step=1,
            help="Cantidad de canales uplink de 125 kHz disponibles por gateway. Más canales aumentan capacidad uplink.",
        )
        eficiencia_aloha = st.slider(
            "Eficiencia ALOHA uplink",
            min_value=0.01,
            max_value=0.30,
            value=preset_eficiencia_aloha,
            step=0.01,
            help="Representa eficiencia real del acceso uplink tipo ALOHA: colisiones, pérdidas y reutilización imperfecta del canal. Subirlo aumenta capacidad estimada; bajarlo vuelve el modelo más conservador.",
        )

        st.header("ACK / downlink AU915")
        confirmed_ratio = st.slider(
            "Fracción de uplinks confirmados",
            0.0,
            1.0,
            preset_confirmed_ratio,
            0.05,
            help="Porcentaje de uplinks que requieren ACK. 1.0 = todos los mensajes requieren ACK. A mayor valor, mayor carga downlink y bloqueo half-duplex.",
        )
        ack_payload = st.slider(
            "Payload de aplicación en ACK/downlink (bytes)",
            min_value=0,
            max_value=20,
            value=0,
            help="Un ACK vacío usa 0 aquí. El estimador añade automáticamente el paquete LoRaWAN mínimo de 12 bytes.",
        )
        rx2_fallback = st.slider(
            "Fracción de ACK que caen en RX2",
            0.0,
            0.50,
            preset_rx2_fallback,
            0.05,
            help="Porcentaje de ACK que no logran RX1 y terminan transmitiéndose en RX2. Subir este valor aumenta significativamente el airtime downlink.",
        )
        eficiencia_ack = st.slider(
            "Uso máximo de airtime downlink ACK por gateway",
            0.01,
            0.50,
            preset_eficiencia_ack,
            0.01,
            help="Fracción máxima del tiempo que un gateway puede usar transmitiendo ACK. Valores bajos vuelven el modelo más conservador.",
        )
        max_blocking_rx = st.slider(
            "Máximo bloqueo RX tolerable por ACK",
            0.01,
            0.50,
            preset_max_blocking_rx,
            0.01,
            help="Fracción máxima del tiempo que el gateway puede permanecer transmitiendo ACK y no recibiendo uplinks. Bajarlo exige más gateways.",
        )
        retransmission_factor = st.slider(
            "Factor de retransmisiones por ACK perdido",
            1.0,
            8.0,
            preset_retransmission_factor,
            0.5,
            help="Representa retransmisiones causadas por pérdida de uplinks o ACK. 1.0 = sin retransmisiones. Valores altos incrementan mucho la carga total.",
        )

        with st.expander("Opciones LoRaWAN avanzadas"):
            fopts_uplink = st.slider(
                "FOpts promedio uplink (bytes)", 0, 15, 0,
                help="Bytes promedio de comandos MAC transportados en FHDR.",
            )
            fopts_downlink = st.slider(
                "FOpts promedio downlink (bytes)", 0, 15, 0,
                help="Bytes promedio de comandos MAC transportados en el ACK/downlink.",
            )
            uplink_dwell_time_enabled = st.toggle(
                "UplinkDwellTime = 1 (límite 400 ms)",
                value=True,
                help="Desactivar solo si la configuración regional/red confirma UplinkDwellTime=0.",
            )

        st.header("Margen final")
        factor_seguridad = st.slider(
            "Factor de seguridad final",
            1.0,
            3.0,
            preset_factor_seguridad,
            0.1,
            help="Margen ingenieril para crecimiento futuro, variabilidad RF e incertidumbre operacional. No debe duplicar el efecto ACK.",
        )

    st.subheader("ADR y configuración DR/SF")
    adr_enabled = st.toggle(
        "ADR habilitado",
        value=False,
        help="ADR OFF: los sensores usan DR/SF fijo. ADR ON: se habilitan perfiles de distribución SF.",
    )

    if not adr_enabled:
        st.info("ADR OFF: selecciona el DR/SF fijo configurado en los equipos. El perfil operativo, si está activo, sí ajusta canal/ACK/seguridad, pero no cambia el DR/SF.", icon="ℹ️")
        dr_map = au915_dr_to_sf()
        dr_fijo = st.selectbox("DR/SF fijo de los equipos", list(dr_map.keys()), index=2)
        sf_fijo = dr_map[dr_fijo]
        distribucion = distribucion_por_dr_fijo(sf_fijo)
        st.write(f"Distribución usada: **100% de los nodos en {sf_fijo}**")
        st.dataframe(pd.DataFrame([{"SF": sf, "Distribución": distribucion[sf]} for sf in SF_ORDER]), use_container_width=True, hide_index=True)
    else:
        st.info("ADR ON: se puede usar un perfil de distribución SF o editarlo manualmente.", icon="ℹ️")
        perfiles = perfiles_adr()
        perfil_nombre = st.selectbox("Perfil ADR", list(perfiles.keys()))
        editar = st.checkbox("Editar distribución SF manualmente", value=False)
        base = perfiles[perfil_nombre]
        cols = st.columns(6)
        valores = {}
        for col, sf in zip(cols, SF_ORDER):
            with col:
                valores[sf] = st.number_input(sf, 0.0, 1.0, float(base[sf]), 0.01, disabled=not editar)
        suma_sf = sum(valores.values())
        st.write(f"Suma actual distribución SF: **{suma_sf:.3f}**")
        distribucion = normalizar_distribucion(valores) if not math.isclose(suma_sf, 1.0, abs_tol=1e-6) else valores
        if not math.isclose(suma_sf, 1.0, abs_tol=1e-6):
            st.warning("La suma de SF no es 1.0. Se normalizará automáticamente para el cálculo.")

    st.divider()
    st.subheader("Cobertura geográfica y redundancia")
    coverage_enabled = st.toggle(
        "Incorporar polígono y cobertura RF",
        value=False,
        help="Combina la cantidad requerida por cobertura con el cuello de botella de capacidad.",
    )
    coverage_plan = None
    coverage_geometry = None
    use_coverage_distribution = False

    if coverage_enabled:
        st.info(
            "La planificación propone sitios dentro del polígono con un modelo isotrópico. "
            "En contenedores debe calibrarse posteriormente con mediciones RSSI/SNR.",
            icon="🗺️",
        )
        geojson_file = st.file_uploader(
            "Polígono del recinto (KMZ, KML o GeoJSON)",
            type=["kmz", "kml", "geojson", "json"],
            help="KMZ/KML de Google Earth o GeoJSON Polygon/MultiPolygon en WGS84.",
        )
        environment_name = st.selectbox(
            "Ambiente RF",
            list(ENVIRONMENT_PRESETS.keys()),
            index=list(ENVIRONMENT_PRESETS.keys()).index("Terminal de contenedores"),
        )
        environment = ENVIRONMENT_PRESETS[environment_name]
        antenna_name = st.selectbox(
            "Tipo de antena por gateway",
            list(ANTENNA_PRESETS.keys()),
            index=list(ANTENNA_PRESETS.keys()).index("Sectorial 60° × 35°"),
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
                help="2 exige que cada punto de evaluación sea cubierto por dos gateways distintos.",
            )
            target_sf = st.selectbox(
                "SF máximo de diseño",
                SF_ORDER,
                index=3,
                help="SF10 es una referencia prudente si el dwell time de 400 ms está activo.",
            )
        with cov2:
            tx_eirp = st.number_input("EIRP del dispositivo (dBm)", 0.0, 36.0, 20.0, 1.0)
            gateway_gain = st.number_input(
                "Ganancia máxima de antena (dBi)",
                0.0,
                30.0,
                float(antenna_preset["gain_dbi"]),
                0.5,
            )
            cable_loss = st.number_input("Pérdidas cable/conectores (dB)", 0.0, 10.0, 2.0, 0.5)
        with cov3:
            horizontal_beamwidth = st.number_input(
                "Haz horizontal HPBW (°)",
                5.0,
                360.0,
                float(antenna_preset["horizontal_beamwidth_deg"]),
                5.0,
            )
            vertical_beamwidth = st.number_input(
                "Haz vertical HPBW (°)",
                5.0,
                180.0,
                float(antenna_preset["vertical_beamwidth_deg"]),
                5.0,
            )
            max_antenna_attenuation = st.number_input(
                "Atenuación lateral/trasera máx. (dB)",
                0.0,
                50.0,
                float(antenna_preset["max_attenuation_db"]),
                1.0,
            )
        with cov4:
            gateway_height = st.number_input("Altura del gateway (m)", 2.0, 100.0, 20.0, 1.0)
            device_height = st.number_input("Altura del dispositivo (m)", 0.0, 20.0, 1.5, 0.5)
            downtilt = st.number_input(
                "Downtilt hacia el suelo (°)",
                -10.0,
                45.0,
                float(antenna_preset["downtilt_deg"]),
                1.0,
            )

        rf1, rf2, rf3, rf4 = st.columns(4)
        with rf1:
            resolution_m = st.number_input(
                "Resolución de evaluación (m)", 25.0, 1000.0, 100.0, 25.0
            )
        with rf2:
            path_loss_exponent = st.number_input(
                "Exponente de pérdida",
                1.5,
                6.0,
                float(environment["path_loss_exponent"]),
                0.1,
            )
        with rf3:
            additional_loss = st.number_input(
                "Pérdida adicional ambiente (dB)",
                0.0,
                50.0,
                float(environment["additional_loss_db"]),
                1.0,
            )
        with rf4:
            fade_margin = st.number_input(
                "Margen de desvanecimiento (dB)",
                0.0,
                40.0,
                float(environment["fade_margin_db"]),
                1.0,
            )

        if geojson_file is not None:
            try:
                coverage_geometry = parse_polygon_file(
                    geojson_file.name, geojson_file.getvalue()
                )
                radio_config = RadioConfig(
                    tx_eirp_dbm=float(tx_eirp),
                    gateway_gain_dbi=float(gateway_gain),
                    gateway_cable_loss_db=float(cable_loss),
                    target_sf=target_sf,
                    path_loss_exponent=float(path_loss_exponent),
                    additional_loss_db=float(additional_loss),
                    fade_margin_db=float(fade_margin),
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
                coverage_plan = plan_coverage(
                    coverage_geometry,
                    radio_config,
                    antenna=antenna_config,
                    redundancy=int(redundancy),
                    resolution_m=float(resolution_m),
                )
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Superficie", f"{coverage_plan.area_m2 / 1_000_000:.2f} km²")
                m2.metric("Radio máximo en boresight", f"{coverage_plan.radius_m:.0f} m")
                m3.metric("Gateways por cobertura", len(coverage_plan.selected_points))
                m4.metric("Puntos con redundancia", f"{coverage_plan.coverage_fraction:.1%}")
                st.caption(
                    f"Modelo: una antena {antenna_name} por gateway, ganancia {gateway_gain:.1f} dBi, "
                    f"HPBW {horizontal_beamwidth:.0f}° × {vertical_beamwidth:.0f}° y downtilt {downtilt:.0f}°."
                )

                for warning in coverage_plan.warnings:
                    st.warning(warning)

                with st.expander("Comparar perfiles de antena en este polígono"):
                    st.caption(
                        "La comparación mantiene ambiente, redundancia, EIRP, alturas y resolución. "
                        "Cada perfil usa su ganancia, HPBW y downtilt iniciales."
                    )
                    comparison_rows = []
                    for comparison_name in (
                        "Omnidireccional",
                        "Sectorial 60° × 35°",
                        "Direccional 30° × 30°",
                    ):
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
                            gateway_gain_dbi=comparison_antenna.gain_dbi,
                            gateway_cable_loss_db=float(cable_loss),
                            target_sf=target_sf,
                            path_loss_exponent=float(path_loss_exponent),
                            additional_loss_db=float(additional_loss),
                            fade_margin_db=float(fade_margin),
                        )
                        comparison_plan = plan_coverage(
                            coverage_geometry,
                            comparison_radio,
                            antenna=comparison_antenna,
                            redundancy=int(redundancy),
                            resolution_m=float(resolution_m),
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
                    st.dataframe(
                        pd.DataFrame(comparison_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

                use_coverage_distribution = st.checkbox(
                    "Usar la distribución SF derivada de la cobertura en el cálculo de capacidad",
                    value=True,
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
                st.error(f"No fue posible procesar el polígono: {coverage_exc}")

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
        df, resumen, advertencias = estimar_gateways(
            nodos_totales=int(nodos),
            mensajes_por_nodo_por_hora=float(mensajes_hora),
            eficiencia_aloha_uplink=float(eficiencia_aloha),
            canales_uplink_por_gateway=int(canales_ul),
            factor_seguridad=float(factor_seguridad),
            distribucion_sf=distribucion,
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
            lon_lat_sites = points_to_lon_lat(final_sites, coverage_geometry.projection)
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
                fig_cov, ax_cov = plt.subplots(figsize=(9, 7))
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
                ax_cov.scatter(
                    [point[0] for point in final_sites],
                    [point[1] for point in final_sites],
                    color="tab:red",
                    marker="^",
                    s=70,
                    label="Gateway",
                )
                if not coverage_plan.antenna_config.is_omnidirectional:
                    arrow_length = coverage_plan.radius_m * 0.28
                    for point, azimuth in zip(final_sites, final_azimuths):
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
                ax_cov.set_title("Polígono y orientación preliminar de antenas")
                ax_cov.set_xlabel("Este local (m)")
                ax_cov.set_ylabel("Norte local (m)")
                ax_cov.grid(True, alpha=0.25)
                ax_cov.legend()
                st.pyplot(fig_cov)
                plt.close(fig_cov)

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
            st.pyplot(fig)
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
                "Redundancia lograda": f"{coverage_plan.coverage_fraction:.1%}",
                "SF máximo de diseño": target_sf,
                "Radio máximo en boresight": f"{coverage_plan.radius_m:.0f} m",
                "Tipo de antena": antenna_name,
                "Ganancia": f"{gateway_gain:.1f} dBi",
                "HPBW horizontal / vertical": f"{horizontal_beamwidth:.0f}° / {vertical_beamwidth:.0f}°",
                "Downtilt": f"{downtilt:.1f}°",
                "Altura gateway / dispositivo": f"{gateway_height:.1f} m / {device_height:.1f} m",
                "Pérdida adicional ambiente": f"{additional_loss:.1f} dB",
                "Margen de desvanecimiento": f"{fade_margin:.1f} dB",
            }

        current_snapshot = {
            "name": "Estimación actual",
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
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
        }

        st.divider()
        st.subheader("Guardar y compartir estimaciones")
        st.caption(
            "Los escenarios se guardan durante esta sesión. Descarga la cartera JSON para conservarlos y volver a importarlos posteriormente."
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
            selected_name = st.selectbox("Seleccionar estimación", saved_names)
            selected_snapshot = st.session_state.saved_estimations[selected_name]
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
            saved_pdf_col, portfolio_col, delete_col = st.columns(3)
            with saved_pdf_col:
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
                if st.button("Eliminar seleccionada", use_container_width=True):
                    del st.session_state.saved_estimations[selected_name]
                    st.rerun()

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

import math

import pandas as pd

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
    de = 1 if sf >= 11 else 0
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
        mensajes_hora_sf = nodos_sf * mensajes_por_nodo_por_hora

        toa_uplink = calcular_toa(sf_ul, payload_uplink_bytes, bw=125_000)
        toa_ul_ms = toa_uplink * 1000
        if toa_ul_ms > AU915_DWELL_TIME_MS and nodos_sf > 0:
            advertencias.append(
                f"⚠ DWELL TIME UPLINK: SF{sf_ul} con {payload_uplink_bytes} bytes = "
                f"{toa_ul_ms:.0f} ms > 400 ms. Revisar payload, SF o dwell-time regional."
            )

        capacidad_teorica_uplink_canal = 3600 / toa_uplink
        capacidad_aloha_uplink_gw = capacidad_teorica_uplink_canal * eficiencia_aloha_uplink * canales_uplink_por_gateway
        carga_uplink = mensajes_hora_sf / capacidad_aloha_uplink_gw if capacidad_aloha_uplink_gw else 0

        sf_dl_rx1 = AU915_UL_TO_DL_SF[sf_ul]
        toa_ack_rx1 = calcular_toa(sf_dl_rx1, ack_payload_bytes, bw=AU915_DOWNLINK_BW)
        toa_ack_rx2 = calcular_toa(AU915_RX2_SF, ack_payload_bytes, bw=AU915_DOWNLINK_BW)
        toa_ack_ponderado = (toa_ack_rx1 * (1 - rx2_fallback_ratio)) + (toa_ack_rx2 * rx2_fallback_ratio)

        ack_attempts_hora_sf = mensajes_hora_sf * confirmed_ratio * retransmission_factor
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
                "Uplinks/hora": round(mensajes_hora_sf, 1),
                "Payload UL bytes": payload_uplink_bytes,
                "ToA UL (s)": toa_uplink,
                "ToA UL (ms)": round(toa_ul_ms, 0),
                "Dwell OK UL": "✓" if toa_ul_ms <= AU915_DWELL_TIME_MS else "⚠ revisar",
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
            "Uplinks/hora": round(nodos_totales * mensajes_por_nodo_por_hora, 1),
            "Payload UL bytes": payload_uplink_bytes,
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
    }

    return pd.DataFrame(filas), resumen, advertencias


def app_streamlit():
    st.set_page_config(page_title="Estimador LoRaWAN AU915", layout="wide")
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
            "Payload uplink (bytes)",
            min_value=1,
            max_value=250,
            value=11,
            help="Tamaño del mensaje uplink. Payloads mayores aumentan el Time on Air, reducen capacidad y elevan colisiones.",
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
            "Payload ACK downlink (bytes)",
            min_value=0,
            max_value=20,
            value=0,
            help="Payload adicional enviado en el downlink ACK. ACK vacíos normalmente usan 0 bytes. Payloads mayores aumentan el airtime downlink.",
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
        )

        if advertencias:
            st.subheader("⚠️ Advertencias")
            for adv in advertencias:
                st.warning(adv)

        st.subheader("Resultado de dimensionamiento")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Gateways recomendados", resumen["gateways_recomendados"])
        c2.metric("Gateways estimados = MAX(cuellos) × seguridad", resumen["gateways_estimados"], help="Se toma el mayor cuello entre uplink, airtime ACK y bloqueo RX, y luego se aplica seguridad.")
        c3.metric("Cuello de botella", resumen["cuello_botella"])
        c4.metric("ACK/downlink attempts hora", resumen["ack_attempts_hora_total"])

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Gateways por uplink (cuello uplink)", resumen["gateways_por_uplink"])
        c6.metric("Gateways por ACK airtime", resumen["gateways_por_airtime_ack"], help="No se suma con bloqueo RX; se usa el máximo.")
        c7.metric("Gateways por ACK blocking", resumen["gateways_por_blocking"], help="No se suma con airtime ACK; se usa el máximo.")
        c8.metric("Airtime DL disponible (s/h)", resumen["airtime_dl_disponible_s_hora"])

        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Descargar CSV", csv, "estimacion_gateways_au915.csv", "text/csv")

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

        with st.expander("📖 Interpretación"):
            st.markdown(
                f"""
**ADR OFF:** se usa 100% de nodos en el DR/SF fijo seleccionado.

**ADR ON:** se usan perfiles de distribución SF porque ADR puede mover nodos entre data rates.

**Resultado final:** se toma el mayor cuello de botella entre uplink y ACK/downlink, y luego se aplica seguridad.

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

import unittest

from gateway_estimation import (
    antenna_preset_input_state,
    calcular_toa,
    capture_estimation_input_state,
    distribucion_por_dr_fijo,
    estimar_gateways,
    environment_preset_input_state,
    payload_fisico_downlink,
    payload_fisico_uplink,
    restorable_input_state,
)


class GatewayEstimationTests(unittest.TestCase):
    def _estimate(self, confirmed_ratio=1.0, retransmission_factor=1.0):
        _, summary, _ = estimar_gateways(
            nodos_totales=1000,
            mensajes_por_nodo_por_hora=1,
            eficiencia_aloha_uplink=0.10,
            canales_uplink_por_gateway=8,
            factor_seguridad=1.0,
            distribucion_sf=distribucion_por_dr_fijo("SF10"),
            payload_uplink_bytes=4,
            confirmed_ratio=confirmed_ratio,
            ack_payload_bytes=0,
            rx2_fallback_ratio=0.0,
            eficiencia_downlink_ack=0.10,
            max_blocking_rx=0.10,
            retransmission_factor=retransmission_factor,
        )
        return summary

    def test_observed_packet_matches_tts_airtime(self):
        physical_bytes = payload_fisico_uplink(4)
        self.assertEqual(physical_bytes, 17)
        self.assertAlmostEqual(calcular_toa(10, physical_bytes), 0.3297, places=4)

    def test_ack_only_has_twelve_physical_bytes(self):
        self.assertEqual(payload_fisico_downlink(0), 12)
        self.assertAlmostEqual(calcular_toa(12, 12, 500_000), 0.2478, places=4)

    def test_confirmed_retries_increase_uplink_load(self):
        base = self._estimate(confirmed_ratio=1.0, retransmission_factor=1.0)
        retried = self._estimate(confirmed_ratio=1.0, retransmission_factor=2.0)
        self.assertGreaterEqual(
            retried["gateways_por_uplink"], base["gateways_por_uplink"] * 1.9
        )

    def test_unconfirmed_traffic_ignores_ack_retry_factor(self):
        base = self._estimate(confirmed_ratio=0.0, retransmission_factor=1.0)
        retried = self._estimate(confirmed_ratio=0.0, retransmission_factor=3.0)
        self.assertEqual(retried["gateways_por_uplink"], base["gateways_por_uplink"])
        self.assertEqual(retried["ack_attempts_hora_total"], 0)

    def test_saved_estimation_restores_complete_widget_state(self):
        state = {
            "input_nodes": 4200,
            "input_payload_ul": 4,
            "input_coverage_enabled": True,
            "input_antenna": "Sectorial 60° × 35°",
            "input_require_hpbw_redundancy": True,
            "unrelated_key": "ignore",
        }
        captured = capture_estimation_input_state(state)
        self.assertNotIn("unrelated_key", captured)
        restored = restorable_input_state({"input_state": captured})
        self.assertEqual(restored, captured)

    def test_legacy_saved_estimation_is_partially_migrated(self):
        snapshot = {
            "parameters": {
                "Perfil operativo": "Terminal Contenedores",
                "Nodos totales": 4000,
                "Uplinks confirmados": "25.0%",
                "ADR": "Deshabilitado",
            },
            "coverage": {"Ambiente RF": "Terminal de contenedores"},
        }
        restored = restorable_input_state(snapshot)
        self.assertEqual(restored["input_nodes"], 4000)
        self.assertEqual(restored["input_confirmed_ratio"], 0.25)
        self.assertTrue(restored["input_coverage_enabled"])
        self.assertTrue(restored["input_require_hpbw_redundancy"])

    def test_antenna_preset_updates_all_owned_rf_controls(self):
        omni = antenna_preset_input_state("Omnidireccional")
        sector = antenna_preset_input_state("Sectorial 60° × 35°")
        directional = antenna_preset_input_state("Direccional 30° × 30°")
        self.assertEqual(omni["input_horizontal_beamwidth"], 360.0)
        self.assertEqual(sector["input_horizontal_beamwidth"], 60.0)
        self.assertEqual(directional["input_horizontal_beamwidth"], 30.0)
        self.assertGreater(
            directional["input_gateway_gain"], omni["input_gateway_gain"]
        )

    def test_environment_preset_updates_propagation_controls(self):
        container = environment_preset_input_state("Terminal de contenedores")
        open_area = environment_preset_input_state("Área abierta")
        self.assertGreater(
            container["input_additional_loss"], open_area["input_additional_loss"]
        )


if __name__ == "__main__":
    unittest.main()

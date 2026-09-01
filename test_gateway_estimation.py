import unittest

from gateway_estimation import (
    calcular_toa,
    distribucion_por_dr_fijo,
    estimar_gateways,
    payload_fisico_downlink,
    payload_fisico_uplink,
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


if __name__ == "__main__":
    unittest.main()

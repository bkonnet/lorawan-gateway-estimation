import unittest

from reporting import build_pdf_report, load_scenarios_json, safe_filename, scenarios_json


class ReportingTests(unittest.TestCase):
    def sample_snapshot(self):
        return {
            "name": "Terminal Norte - Alternativa A",
            "saved_at": "2026-08-31T10:00:00-04:00",
            "parameters": {
                "Nodos totales": 4000,
                "Mensajes por nodo por hora": 12,
                "Payload aplicación uplink (bytes)": 4,
            },
            "summary": {
                "gateways_finales": 8,
                "gateways_por_capacidad": 6,
                "gateways_por_cobertura": 8,
                "condicion_dominante": "cobertura",
                "gateways_por_uplink": 5.2,
                "gateways_por_airtime_ack": 0.0,
                "gateways_por_blocking": 0.0,
                "airtime_dl_disponible_s_hora": 360.0,
            },
            "coverage": {
                "Superficie": "1.250 km2",
                "Ambiente RF": "Terminal de contenedores",
                "Redundancia requerida": 2,
                "Tipo de antena": "Sectorial 60° x 35°",
            },
            "warnings": ["Validar el diseño mediante site survey."],
            "details": [
                {
                    "SF UL": "SF10",
                    "Nodos": 4000,
                    "Uplinks/hora": 48000,
                    "Payload UL bytes": 17,
                    "ToA UL (ms)": 330,
                    "Carga uplink": 5.2,
                    "ToA ACK pond. (s)": 0.09,
                    "Airtime ACK s/h": 0,
                }
            ],
            "sites": [
                {
                    "gateway": "GW-01",
                    "longitude": -70.65,
                    "latitude": -33.45,
                    "antena": "Sectorial 60° x 35°",
                    "ganancia_dbi": 12,
                    "azimuth_deg": 90,
                    "downtilt_deg": 5,
                }
            ],
            "figures": {
                "coverage_map_png_base64": (
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ),
                "capacity_charts_png_base64": (
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ),
            },
        }

    def test_filename_is_portable(self):
        self.assertEqual(safe_filename("Terminal Norte / opción A"), "Terminal_Norte_opcion_A")

    def test_scenario_portfolio_round_trip(self):
        scenarios = {"Alternativa A": self.sample_snapshot()}
        self.assertEqual(load_scenarios_json(scenarios_json(scenarios)), scenarios)

    def test_pdf_is_generated(self):
        pdf = build_pdf_report(self.sample_snapshot())
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 2500)
        self.assertGreaterEqual(pdf.count(b"/Subtype /Image"), 2)


if __name__ == "__main__":
    unittest.main()

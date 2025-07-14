import argparse
import json
import pandas as pd
import os

try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None
    print("Advertencia: matplotlib no está instalado. Los gráficos no estarán disponibles.")

def calcular_toa(sf, payload_bytes):
    bw = 125000
    cr = 1
    ih = 0
    de = 1 if sf in [11, 12] else 0
    h = 0 if ih else 1
    n_preamble = 8
    t_sym = (2 ** sf) / bw
    t_preamble = (n_preamble + 4.25) * t_sym
    payload_symb_nb = 8 + max(
        int(
            (8 * payload_bytes - 4 * sf + 28 + 16 - 20 * h)
            / (4 * (sf - 2 * de))
        )
        * (cr + 4),
        0,
    )
    t_payload = payload_symb_nb * t_sym
    return round(t_preamble + t_payload, 3)

def estimar_gateways(nodos_totales, mensajes_por_nodo_por_hora, efficiency, canales_por_gateway, factor_seguridad, distribucion_sf, payload_bytes):
    sf_toa = {sf: calcular_toa(int(sf[2:]), payload_bytes) for sf in distribucion_sf}
    filas = []
    total_carga = 0
    for sf, proporcion in distribucion_sf.items():
        nodos = int(nodos_totales * proporcion)
        mensajes_totales = nodos * mensajes_por_nodo_por_hora
        toa = sf_toa[sf]
        capacidad_por_canal = (3600 / toa) * efficiency
        capacidad_gateway = capacidad_por_canal * canales_por_gateway
        carga = mensajes_totales / capacidad_gateway
        total_carga += carga
        filas.append({
            "SF": sf,
            "Nodos asignados": nodos,
            "Mensajes/hora": mensajes_totales,
            "ToA (s)": toa,
            "Capacidad por Gateway (msg/h)": int(capacidad_gateway),
            "Carga relativa": round(carga, 3)
        })
    gateways_necesarios = round(total_carga * factor_seguridad, 1)
    filas.append({"SF": "TOTAL", "Nodos asignados": "", "Mensajes/hora": "", "ToA (s)": "", "Capacidad por Gateway (msg/h)": "", "Carga relativa": f"Gateways requeridos: {gateways_necesarios}"})
    df = pd.DataFrame(filas)
    df.attrs['payload_bytes'] = payload_bytes
    return df

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Estimador de gateways LoRaWAN para bandas AU915/US915")
    parser.add_argument('--nodos', type=int, required=True, help='Cantidad total de nodos en la red')
    parser.add_argument('--mensajes_hora', type=int, required=True, help='Mensajes por nodo por hora')
    parser.add_argument('--eficiencia', type=float, default=0.18, help='Eficiencia de canal (default=0.18)')
    parser.add_argument('--canales', type=int, default=8, help='Cantidad de canales por gateway (default=8)')
    parser.add_argument('--seguridad', type=float, default=0.6, help='Factor de seguridad (default=0.6)')
    parser.add_argument('--payload_bytes', type=int, default=20, choices=range(1, 251), metavar='[1-250]', help='Tamaño del payload en bytes (1 a 250, default=20). Este valor afecta el cálculo del tiempo de aire (ToA) y, por lo tanto, la estimación de capacidad de la red.')
    parser.add_argument('--distribucion_sf', type=str, required=True, help='Archivo JSON con distribución de SF (debe contener SF7–SF12 y sumar 1.0)')
    parser.add_argument('--export_excel', type=str, help='Nombre del archivo Excel de salida, ej: resultado.xlsx')
    parser.add_argument('--grafico', type=str, help='Nombre del archivo de imagen para guardar el gráfico, ej: carga_sf.png')

    if len(os.sys.argv) == 1:
        print("\nFaltan argumentos. Ejecute con --help para ver las opciones disponibles.\n")
        parser.print_help()
        os.sys.exit(1)

    args = parser.parse_args()

    try:
        if args.distribucion_sf.endswith('.json'):
            with open(args.distribucion_sf, 'r') as f:
                distribucion_sf = json.load(f)
        else:
            distribucion_sf = json.loads(args.distribucion_sf)
    except Exception as e:
        print("Error al leer la distribución SF:", e)
        exit(1)

    print(f"Cargando archivo JSON: {args.distribucion_sf}")
    print("Contenido JSON cargado:", distribucion_sf)

    resultado = estimar_gateways(
        nodos_totales=args.nodos,
        mensajes_por_nodo_por_hora=args.mensajes_hora,
        efficiency=args.eficiencia,
        canales_por_gateway=args.canales,
        factor_seguridad=args.seguridad,
        distribucion_sf=distribucion_sf,
        payload_bytes=args.payload_bytes
    )

    print(f"\nTamaño del payload utilizado: {args.payload_bytes} bytes\n")
    print(resultado.to_string(index=False))

    if args.export_excel:
        try:
            resultado.to_excel(args.export_excel, index=False, sheet_name=f"Payload_{args.payload_bytes}B")
        except Exception as e:
            print("Error al exportar a Excel:", e)

    if args.grafico:
        if plt is not None:
            try:
                df_plot = resultado[resultado['SF'].str.startswith('SF')]
                plt.figure(figsize=(10, 6))
                bars = plt.bar(df_plot['SF'], df_plot['Carga relativa'])
                for bar, carga, toa in zip(bars, df_plot['Carga relativa'], df_plot['ToA (s)']):
                    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01, f"ToA: {toa}s", ha='center', fontsize=8)
                plt.xlabel('Spreading Factor')
                plt.ylabel('Carga relativa')
                plt.title('Carga por SF en gateway')
                plt.figtext(0.99, 0.01, f'Tamaño del payload: {args.payload_bytes} bytes', ha='right', fontsize=8, style='italic')
                plt.grid(True, axis='y', linestyle='--', alpha=0.5)
                plt.savefig(args.grafico)
                print(f"Gráfico guardado en {args.grafico}")
            except Exception as e:
                print("Error al generar gráfico:", e)
        else:
            print("matplotlib no está instalado. Puede instalarlo con: pip install matplotlib")

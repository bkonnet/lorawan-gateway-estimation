# Estimador de Gateways LoRaWAN

Este script permite estimar la cantidad de gateways necesarios para soportar una red LoRaWAN operando en las bandas AU915 o US915, considerando el volumen de tráfico esperado y la distribución de Spreading Factors (SF).

## Características

- Permite parametrizar:
  - Número de nodos
  - Mensajes por hora por nodo
  - Eficiencia del canal
  - Número de canales por gateway
  - Tamaño del payload
  - Distribución de SF (debe sumar 1.0)
  - Factor de seguridad
- Exporta los resultados a Excel
- Genera gráficos de carga relativa por SF

## Requisitos

- Python 3.8+
- pandas
- matplotlib (opcional, para los gráficos)

Instalación de dependencias:

```bash
pip install pandas matplotlib
```

## Argumentos

--nodos: Número total de nodos en la red.
--mensajes_hora: Mensajes enviados por cada nodo por hora.
--eficiencia: Eficiencia del canal (valor entre 0 y 1, default: 0.18).
--canales: Número de canales por gateway (default: 8).
--seguridad: Factor de seguridad adicional sobre la carga estimada (default: 0.6).
--payload_bytes: Tamaño del mensaje en bytes (de 1 a 250, default: 20).
--distribucion_sf: Ruta a un archivo .json con la distribución de Spreading Factors.
--export_excel: Nombre del archivo Excel para guardar los resultados (opcional).
--grafico: Nombre del archivo de imagen (PNG) para guardar el gráfico (opcional).

## Uso

Ejemplo de ejecución:

```bash
python gateway_estimation.py \
  --nodos 5000 \
  --mensajes_hora 4 \
  --canales 8 \
  --payload_bytes 20 \
  --distribucion_sf sf.json \
  --export_excel resultados.xlsx \
  --grafico carga_sf.png
```

### Estructura del archivo `sf.json`

```json
{
  "SF7": 0.25,
  "SF8": 0.25,
  "SF9": 0.2,
  "SF10": 0.15,
  "SF11": 0.1,
  "SF12": 0.05
}
```

**Nota:** La suma de los valores debe ser exactamente 1.0

## Salida

- Tabla con carga relativa por SF y el total de gateways necesarios
- Archivo Excel (opcional)
- Gráfico de barras de carga por SF (opcional)

## Autor

Proyecto desarrollado para optimizar el despliegue de redes LoRaWAN en aplicaciones logísticas portuarias.


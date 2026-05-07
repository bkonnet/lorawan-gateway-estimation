# Estimador Interactivo de Gateways LoRaWAN para AU915

Herramienta interactiva desarrollada en Python + Streamlit para estimar la cantidad de gateways LoRaWAN requeridos en redes **AU915-928**, considerando tráfico uplink, ACK/downlink, bloqueo half-duplex del gateway, distribución de Spreading Factors y escenarios con o sin ADR.

Está orientada a despliegues industriales, logísticos y portuarios, especialmente en ambientes con contenedores metálicos, multipath, ruido RF y dispositivos que usan **confirmed uplink con ACK**.

---

## Características principales

- Interfaz web local con Streamlit.
- Modelo específico para **AU915-928**.
- Soporte para escenarios con **ADR ON** y **ADR OFF**.
- En modo **ADR OFF**, permite seleccionar el **DR/SF fijo** configurado en los equipos.
- En modo **ADR ON**, permite usar perfiles de distribución SF.
- Modelo separado de:
  - cuello de botella uplink,
  - cuello de botella por airtime ACK,
  - cuello de botella por bloqueo RX half-duplex.
- Cálculo de Time on Air para uplink y ACK/downlink.
- Advertencia por dwell time uplink de 400 ms.
- Perfil operativo **Terminal Contenedores**.
- Exportación de resultados a CSV.
- Gráficos de carga uplink, airtime ACK y comparación RX1/RX2.

---

## Requisitos

### Software

- Python 3.10 o superior recomendado.
- pip instalado.
- Navegador web moderno.

### Dependencias Python

Instalar con:

```bash
pip install streamlit pandas matplotlib openpyxl
```

En Windows, si usas el lanzador `py`:

```powershell
py -m pip install streamlit pandas matplotlib openpyxl
```

Dependencias utilizadas:

| Paquete | Uso |
|---|---|
| `streamlit` | Interfaz web interactiva |
| `pandas` | Tablas y procesamiento de resultados |
| `matplotlib` | Gráficos |
| `openpyxl` | Soporte Excel si se desea extender exportación |

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/TU_USUARIO/lorawan-gateway-estimation.git
cd lorawan-gateway-estimation
```

Reemplazar `TU_USUARIO` por el usuario real de GitHub.

### 2. Instalar dependencias

```bash
pip install streamlit pandas matplotlib openpyxl
```

O en Windows:

```powershell
py -m pip install streamlit pandas matplotlib openpyxl
```

### 3. Ejecutar la aplicación

```bash
streamlit run gateway_estimation.py
```

Si `streamlit` no está en el PATH de Windows:

```powershell
py -m streamlit run gateway_estimation.py
```

Al ejecutar, Streamlit abrirá una interfaz web local. Si no se abre automáticamente, copiar en el navegador la URL indicada en consola, normalmente:

```text
http://localhost:8501
```

---

## Manual de uso

### 1. Perfil operativo

La herramienta incluye un selector de perfil operativo.

#### Personalizado

Permite ajustar manualmente todos los parámetros.

#### Terminal Contenedores

Perfil recomendado para patios de contenedores, terminales portuarias o zonas con alta presencia metálica. Representa:

- multipath,
- reflexiones,
- ruido RF,
- pérdidas intermitentes,
- ACK perdidos,
- mayor probabilidad de retransmisiones.

Este perfil ajusta automáticamente:

| Parámetro | Valor preset |
|---|---:|
| Eficiencia ALOHA uplink | 0.10 |
| Fracción de uplinks confirmados | 1.0 |
| Fracción ACK en RX2 | 0.20 |
| Factor de retransmisiones | 2.0 |
| Uso máximo airtime ACK | 0.10 |
| Máximo bloqueo RX por ACK | 0.06 |
| Factor de seguridad final | 1.4 |

En modo **ADR OFF**, este perfil **no cambia el DR/SF fijo** del equipo. Solo modifica los parámetros de canal, ACK y margen.

---

### 2. Parámetros de tráfico uplink

#### Nodos totales

Cantidad total de dispositivos LoRaWAN activos en la red.

- Si aumenta, suben los uplinks/hora.
- También aumenta la cantidad de ACK/downlink si los mensajes son confirmados.

#### Mensajes por nodo por hora

Frecuencia de transmisión de cada nodo.

Ejemplos:

| Escenario | Valor |
|---|---:|
| 1 reporte por hora | 1 |
| 1 reporte cada 30 minutos | 2 |
| 1 reporte cada 10 minutos | 6 |
| 1 reporte cada 5 minutos | 12 |

A mayor valor, mayor carga uplink y mayor carga ACK.

#### Payload uplink bytes

Tamaño del mensaje uplink en bytes.

- Valores mayores aumentan el Time on Air.
- Mayor Time on Air reduce la capacidad.
- Mayor Time on Air aumenta la probabilidad de colisión.

Para sensores pequeños, valores típicos pueden ser:

| Tipo de mensaje | Payload aproximado |
|---|---:|
| Temperatura simple | 3 bytes |
| Reporte acumulado | 8 a 11 bytes |
| Mensaje más complejo | 20+ bytes |

#### Canales uplink por gateway

Cantidad de canales uplink de 125 kHz disponibles por gateway.

En AU915, un gateway normalmente opera con una sub-banda de **8 canales uplink**.

- Más canales aumentan capacidad uplink.
- Menos canales reducen capacidad y aumentan colisiones.

#### Eficiencia ALOHA uplink

Representa la eficiencia real del canal uplink considerando:

- colisiones,
- acceso aleatorio,
- interferencia,
- reutilización imperfecta del canal,
- pérdidas por captura imperfecta.

No debe incluir ACK ni margen de seguridad.

Valores orientativos:

| Escenario | Eficiencia sugerida |
|---|---:|
| Laboratorio / canal limpio | 0.20 a 0.30 |
| Red real moderada | 0.12 a 0.18 |
| Terminal de contenedores / multipath | 0.08 a 0.12 |
| Red congestionada | 0.05 a 0.10 |

---

### 3. Parámetros ACK / downlink AU915

#### Fracción de uplinks confirmados

Porcentaje de uplinks que requieren ACK.

| Valor | Interpretación |
|---:|---|
| 0.0 | Ningún mensaje requiere ACK |
| 0.5 | 50% de mensajes confirmados |
| 1.0 | Todos los mensajes requieren ACK |

Si los sensores usan confirmed uplink siempre, usar `1.0`.

#### Payload ACK downlink bytes

Payload adicional en el downlink ACK.

Normalmente un ACK simple puede modelarse como `0 bytes` de payload de aplicación. Si además se envían comandos MAC o payload de aplicación, se puede aumentar.

- Mayor payload ACK aumenta airtime downlink.
- Mayor airtime downlink aumenta bloqueo half-duplex del gateway.

#### Fracción de ACK que caen en RX2

Porcentaje de ACK que no se entregan en RX1 y terminan usando RX2.

En AU915, RX2 suele ser **DR8 / SF12 / 500 kHz**, por lo que puede tener más airtime que un RX1 en SF bajo.

Valores orientativos:

| Escenario | Valor sugerido |
|---|---:|
| Red estable | 0.00 a 0.05 |
| Red industrial normal | 0.05 a 0.15 |
| Patio de contenedores | 0.15 a 0.30 |
| Ambiente muy problemático | 0.30 a 0.50 |

#### Uso máximo de airtime downlink ACK por gateway

Fracción máxima del tiempo por hora que se permite usar al gateway transmitiendo ACK.

Ejemplo:

```text
0.10 = máximo 10% del tiempo transmitiendo ACK
```

Valores bajos son más conservadores y pueden aumentar la cantidad recomendada de gateways.

#### Máximo bloqueo RX tolerable por ACK

Como el gateway es half-duplex, cuando transmite un ACK no puede recibir uplinks. Este parámetro limita qué fracción del tiempo se acepta que el gateway esté transmitiendo y no escuchando.

Valores orientativos:

| Escenario | Valor sugerido |
|---|---:|
| Red muy crítica | 0.03 a 0.05 |
| Terminal contenedores | 0.05 a 0.08 |
| Red normal | 0.08 a 0.15 |
| Red poco exigente | 0.15 a 0.30 |

#### Factor de retransmisiones por ACK perdido

Representa intentos adicionales por pérdida de uplinks o ACK.

| Valor | Interpretación |
|---:|---|
| 1.0 | Sin retransmisiones adicionales |
| 1.5 | 50% más intentos equivalentes |
| 2.0 | El doble de intentos equivalentes |
| 3.0+ | Escenario muy degradado |

La métrica resultante se muestra como:

```text
ACK/downlink attempts hora
```

Esto incluye ACK originales más intentos adicionales por retransmisión.

---

### 4. Factor de seguridad final

Margen de ingeniería aplicado al resultado final.

Debe representar:

- crecimiento futuro,
- incertidumbre RF,
- variación operacional,
- interferencias externas,
- margen de diseño.

No debe duplicar el impacto de ACK, porque el ACK ya se modela de forma separada.

Valores orientativos:

| Escenario | Valor sugerido |
|---|---:|
| Laboratorio | 1.0 |
| Red comercial básica | 1.2 |
| Industrial / puerto | 1.3 a 1.5 |
| Crítico / alta disponibilidad | 1.5 a 2.0 |

---

## ADR y configuración DR/SF

La herramienta soporta dos modos principales.

### ADR OFF

Usar cuando los dispositivos tienen DR/SF fijo y no cambian dinámicamente.

En este modo se selecciona el DR/SF fijo configurado en los equipos:

| AU915 DR | SF | BW |
|---|---|---|
| DR0 | SF12 | 125 kHz |
| DR1 | SF11 | 125 kHz |
| DR2 | SF10 | 125 kHz |
| DR3 | SF9 | 125 kHz |
| DR4 | SF8 | 125 kHz |
| DR5 | SF7 | 125 kHz |

La distribución SF queda automáticamente en:

```text
100% de nodos en el SF seleccionado
```

Esto es importante porque, sin ADR, no se debe asumir que los nodos migrarán a otro SF.

### ADR ON

Usar cuando la red puede ajustar dinámicamente el data rate de los nodos.

En este modo se habilitan perfiles de distribución SF, por ejemplo:

- ADR conservador puerto
- ADR optimizado buena cobertura
- ADR cobertura difícil / indoor

También es posible editar manualmente la distribución.

---

## Modelo AU915 usado

### Uplink

AU915 uplink usa canales de 125 kHz.

### RX1 downlink ACK

Con `RX1DROffset = 0`, el modelo usa el siguiente mapeo:

| Uplink | RX1 ACK |
|---|---|
| DR0 / SF12 | DR8 / SF12 / 500 kHz |
| DR1 / SF11 | DR9 / SF11 / 500 kHz |
| DR2 / SF10 | DR10 / SF10 / 500 kHz |
| DR3 / SF9 | DR11 / SF9 / 500 kHz |
| DR4 / SF8 | DR12 / SF8 / 500 kHz |
| DR5 / SF7 | DR13 / SF7 / 500 kHz |

### RX2 fallback

RX2 se modela como:

```text
DR8 / SF12 / 500 kHz
```

### Downlink TX chains

Aunque AU915 define 8 frecuencias downlink, el modelo asume conservadoramente:

```text
1 transmisión downlink simultánea por gateway
```

Esto evita sobreestimar la capacidad real de ACK.

---

## Interpretación de resultados

La herramienta muestra:

### Gateways por uplink

Cantidad de gateways requerida considerando solo uplink, eficiencia ALOHA y canales.

### Gateways por ACK airtime

Cantidad de gateways requerida por airtime consumido por ACK/downlink.

### Gateways por ACK blocking

Cantidad de gateways requerida para mantener bajo control el bloqueo RX half-duplex.

### Gateways estimados

La herramienta **no suma** los tres valores anteriores. Usa el máximo cuello de botella:

```text
Gateways estimados = MAX(uplink, ACK airtime, ACK blocking) × factor de seguridad
```

### Gateways recomendados

Es el valor estimado redondeado hacia arriba.

Ejemplo:

```text
Gateways estimados = 2.28
Gateways recomendados = 3
```

---

## Ejemplo recomendado para Terminal Contenedores

Para un patio de contenedores con sensores confirmed uplink:

```text
Perfil operativo: Terminal Contenedores
ADR: OFF si los equipos tienen DR fijo
DR/SF fijo: según configuración real del equipo
Payload uplink: 5 a 11 bytes
Fracción confirmed: 1.0
RX2 fallback: 0.20
Retransmission factor: 2.0
Eficiencia ALOHA: 0.10
Máximo bloqueo RX: 0.06
Factor seguridad: 1.4
```

---

## Exportación

La herramienta permite descargar los resultados como CSV desde la interfaz.

---

## Limitaciones

Este programa es una herramienta de dimensionamiento de capacidad. No reemplaza:

- simulación RF con terreno,
- mediciones de campo,
- drive test,
- análisis de interferencia espectral,
- modelamiento detallado de colisiones entre gateways,
- simulación LoRaWAN completa a nivel paquete.

Se recomienda usarlo junto con herramientas de cobertura como Radio Mobile, mediciones RSSI/SNR y pruebas piloto en terreno.

---

## Autor

Herramienta desarrollada para apoyar el dimensionamiento de redes LoRaWAN en entornos industriales, portuarios y logísticos.


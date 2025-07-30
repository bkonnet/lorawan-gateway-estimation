
# Dual‑Laser Deflection Tracker  
Python + OpenCV + ThingsBoard (MQTT)

## Descripción
Este script captura video desde una cámara USB/webcam, detecta de forma simultánea dos puntos láser —**verde (proa)** y **rojo (popa)**—, y envía por MQTT sus coordenadas (`x`, `y`) a un dispositivo en ThingsBoard.  
Está pensado como prueba de concepto para el sistema de medición de deflexión en diques flotantes.

---

## Diagrama rápido

```
Láser (proa, verde)   Láser (popa, rojo)
       \                   /
        \                 /
         > Pantalla difusora <
                ↑ cámara USB
                ↑ Raspberry Pi / PC
                       |
                    MQTT
                       |
                 ThingsBoard CE
```

---

## Requisitos

| Componente | Versión mínima |
|------------|----------------|
| Python     | 3.7+ |
| Pip        | 21+ |
| Librerías  | `opencv-python`, `numpy`, `paho‑mqtt` |
| Hardware   | Cámara USB/Webcam (640×480 o superior) |

### Instalación de dependencias

```bash
pip install opencv-python paho-mqtt numpy
```

---

## Configuración

1. **Crear dispositivo** en ThingsBoard → copiar el *Access Token*.  
2. Editar las siguientes líneas del script:

```python
THINGSBOARD_HOST = "demo.thingsboard.io"   # O tu servidor local
ACCESS_TOKEN     = "TU_ACCESS_TOKEN_AQUI"  # Token del dispositivo
cap = cv2.VideoCapture(0)                  # Índice de la cámara (0, 1…)
```

3. Ajustar rangos HSV si usas otros colores/láseres:

```python
# Rojo (popa)
lower_red  = np.array([0, 70, 70])
upper_red  = np.array([15, 255, 255])

# Verde (proa)
lower_green = np.array([40, 50, 50])
upper_green = np.array([90, 255, 255])
```

---

## Ejecución

```bash
python dual_laser_thingsboard.py
```

- Se abre una ventana de video con los puntos detectados.
- Presiona **`q`** para cerrar.

---

## Datos enviados

| Clave         | Descripción                       |
|---------------|-----------------------------------|
| `proa_x_px`   | Coordenada **X** del láser verde  |
| `proa_y_px`   | Coordenada **Y** del láser verde  |
| `popa_x_px`   | Coordenada **X** del láser rojo   |
| `popa_y_px`   | Coordenada **Y** del láser rojo   |

> Puedes crear widgets en ThingsBoard para visualizar la diferencia de `Y` entre proa y popa y calcular deflexión.

---

## Calibración rápida

1. Coloca los láseres apuntando a una **pantalla blanca** a ~2–3 m.  
2. Toma una foto de referencia con el dique “en reposo” y anota las coordenadas.  
3. Usa esas coordenadas como `base_proa`, `base_popa` para convertir píxeles a mm/cm.  

```python
deflexion_mm = (cy_red - cy_green) * mm_per_px
```

---

## Seguridad

- Usa láseres **Clase 2 (< 1 mW)** o **Clase 3R (≤ 5 mW)**.
- No mires directamente al haz.  
- Protege la cámara y la pantalla de luz solar directa.

---

## Licencia

MIT — libre para uso, modificación y distribución.

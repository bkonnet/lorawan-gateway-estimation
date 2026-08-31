# Notas del modelo geográfico

## Resultado combinado

La aplicación calcula dos requisitos independientes:

1. **Capacidad:** uplink, ACK/downlink y bloqueo half-duplex.
2. **Cobertura:** cantidad de sitios necesaria para cubrir el polígono con la redundancia solicitada.

El resultado final es el máximo de ambos. No se suman, porque cada gateway propuesto aporta simultáneamente cobertura y capacidad.

## Redundancia

Redundancia 2 significa que cada punto muestreado del polígono debe estar dentro del radio calculado de dos candidatos distintos. No significa simplemente multiplicar por dos la cantidad de gateways.

## Ambiente de contenedores

El preset inicial usa:

- Exponente de pérdida: 3.6
- Pérdida adicional: 12 dB
- Margen de desvanecimiento: 20 dB

Son supuestos conservadores editables, no valores universales. Deben calibrarse con mediciones del sitio, especialmente cuando los dispositivos operan dentro de contenedores cerrados.

## Ubicaciones propuestas

El algoritmo selecciona puntos matemáticos dentro del polígono. Antes de adoptar una ubicación se debe verificar:

- estructura y altura disponibles;
- alimentación y backhaul;
- permisos de montaje;
- pérdidas de cables y filtros;
- obstrucciones y apilamiento operativo;
- cobertura uplink y downlink medida.

Si existe una lista real de postes o edificios candidatos, el siguiente paso recomendado es reemplazar la cuadrícula automática por esa lista y ejecutar el mismo algoritmo multi-cover sobre sitios instalables.

## Calibración recomendada

Recolectar para cada prueba:

- latitud y longitud del dispositivo;
- latitud, longitud y altura del gateway;
- RSSI, SNR, SF, potencia TX y frecuencia;
- condición exterior/interior de contenedor;
- altura y estado de apilamiento.

Con esos datos se pueden ajustar el exponente de pérdida, la pérdida adicional y el margen para cada zona del terminal.

# Xiegu G1M – Radio Control CAT

**Versión 1.0.0** · Desarrollado por **EA4HCJ**

Panel de control para PC del transceptor QRP **Xiegu G1M** por el puerto COMM (CAT, protocolo
CI-V de Icom): frecuencia grande sintonizable con el ratón, dial, bandas, modos, VFO A/B,
A = B, SPLIT, PTT con tiempo máximo, potencia, AGC y memorias.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![Windows](https://img.shields.io/badge/Windows-10%2F11-informational)

## Uso rápido

1. Instala Python 3 (marca "Add Python to PATH") y luego `pip install pyserial`.
2. Copia `g1m_control.py`, `g1m_cat.py` y `abrir_g1m.bat` en una misma carpeta.
3. Doble clic en `abrir_g1m.bat`, elige el puerto del cable en la lista y pulsa **Conectar**.

## Datos técnicos del G1M (firmware V1.07)

| Parámetro | Valor |
|---|---|
| Protocolo | CI-V (Icom), 19200 baudios 8N1, dirección 0x70 |
| Funciona | frecuencia (03/05), modo con filtro (04/06 xx yy), VFO A/B (07 00/01), PTT (1C 00), potencia (14 0A), AGC, preamp, NB, NR, break-in |
| Acepta pero no hace nada | volumen, ganancia RF, squelch, A=B (07 A0), A↔B (07 B0) |
| No soporta | split (0F), atenuador (11), CW automático (17), espectro (27), medidores Po/ROE reales |

## Detalles

El firmware oficial (`G1M_ZIF_FW_V1.07_build001.xgf`) está **cifrado** con un cifrado por
bloques de 16 bytes en modo ECB (probablemente AES) y la clave vive en el bootloader del
radio, así que el protocolo no se puede leer del archivo. Este programa lo descubre
preguntándole al radio directamente por CI-V (protocolo Icom).

## Instalación (Windows)

```
pip install -r requirements.txt
```

## Pasos

1. Ver qué puerto COM tiene el cable:
   `python g1m_cat.py puertos`
2. Encontrar velocidad y dirección CI-V:
   `python g1m_cat.py detectar --puerto COM3`
3. Probar qué comandos acepta (no transmite; las escrituras ponen el mismo valor que ya tenía):
   `python g1m_cat.py sondear --puerto COM3 --baudios 19200 --dir 0x70 --amplio`
   Genera `g1m_sondeo_<fecha>.json`.
4. Probar la portadora en varias frecuencias (**transmite**; usa carga ficticia de 50 Ω y
   frecuencias para las que tengas licencia):
   `python g1m_cat.py portadora --puerto COM3 --baudios 19200 --freqs 3.560,7.030,14.060 --modo CW`
   Genera `g1m_portadora_<fecha>.json` con la potencia medida (Po) y la ROE en cada frecuencia.

Otros comandos: `estado`, `freq 7.074`, `modo USB`, `potencia 255`.
Con `-v` se ven las tramas en hexadecimal; con `--puerto SIM` se usa un radio simulado.

Si el radio no responde, prueba `--rts` o `--dtr` (algunos cables los usan para alimentarse).
Por defecto quedan apagados porque otros cables los usan para PTT.

## Panel de control con ventana (`g1m_control.py`)

Doble clic en `abrir_g1m.bat` (o `python g1m_control.py`). Solo hay que **elegir el puerto** en la
lista y pulsar **Conectar**: la velocidad y la dirección CI-V se detectan solas y se recuerdan en
`g1m_config.json` para la próxima vez.

- Frecuencia grande: **rueda del ratón sobre un dígito** cambia ese dígito; **arrastrar** sobre la
  frecuencia sintoniza rápido; **dial** circular para girar con el ratón; flechas ↑↓ y RePág/AvPág.
- Bandas, modos (LSB/USB/CW/CW-R/AM) con filtro 1-3, VFO A/B, A = B, B = A, A ↔ B
  (el G1M acepta `07 A0`/`07 B0` pero no los hace: el programa copia los VFO uno a uno).
- **SPLIT con los dos VFO**: se recibe en el VFO activo y se transmite en el otro; al soltar
  el PTT vuelve al de recepción (el G1M rechaza el split por CAT, `0F`).
- PTT pulsado o en modo interruptor, con tiempo máximo de TX.
- Ajustes: potencia, AGC, preamplificador, NB, NR y break-in. Volumen, ganancia RF, squelch y
  atenuador no están: probado en el G1M, el radio los acepta (o rechaza) pero no hacen nada.
- Memorias guardadas en `g1m_memorias.json`.

Datos confirmados con el radio real: COM6, 19200 baudios, dirección 0x70. El modo solo se acepta
con byte de filtro (`06 <modo> <filtro>`); los medidores de Po/ROE devuelven valores fijos; los
comandos `17` (CW automático) y `27` (espectro) no responden.

## Versiones

- **1.0.0** – Primera versión: panel con frecuencia sintonizable con el ratón y dial, bandas,
  modos con filtro, VFO A/B, A = B, B = A, A ↔ B, SPLIT con los dos VFO, PTT con tiempo máximo,
  potencia, AGC, preamplificador, NB, NR, break-in, memorias con ejemplos QRP, detección
  automática del puerto y de la dirección CI-V, y herramientas de sondeo (`g1m_cat.py`).

73 de EA4HCJ

# Xiegu G1M – control CI-V por el puerto COMM

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

Doble clic en `abrir_g1m.bat` (conecta a COM6), o:
`python g1m_control.py --puerto COM6`

- Frecuencia grande: **rueda del ratón sobre un dígito** cambia ese dígito; **arrastrar** sobre la
  frecuencia sintoniza rápido; **dial** circular para girar con el ratón; flechas ↑↓ y RePág/AvPág.
- Bandas, modos (LSB/USB/CW/CW-R/AM) con filtro 1-3, VFO A/B, A=B, A↔B.
- **Split por software**: al transmitir salta a la frecuencia de TX y vuelve al soltar
  (el G1M rechaza el split por CAT).
- PTT pulsado o en modo interruptor, con tiempo máximo de TX.
- Ajustes: volumen, ganancia RF, squelch, potencia, micro, velocidad CW, AGC, preamplificador,
  atenuador, NB, NR y break-in. El registro de abajo indica si el radio acepta cada orden.
- Memorias guardadas en `g1m_memorias.json`.

Datos confirmados con el radio real: COM6, 19200 baudios, dirección 0x70. El modo solo se acepta
con byte de filtro (`06 <modo> <filtro>`); los medidores de Po/ROE devuelven valores fijos; los
comandos `17` (CW automático) y `27` (espectro) no responden.

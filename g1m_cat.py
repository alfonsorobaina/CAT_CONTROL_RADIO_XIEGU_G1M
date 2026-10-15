#!/usr/bin/env python3
"""
Control y sondeo CI-V para el transceptor Xiegu G1M por el puerto COMM.

El firmware del G1M viene cifrado (AES por bloques de 16 bytes), así que el
protocolo no se puede leer del archivo .xgf: se descubre preguntándole al radio.
El G1M habla CI-V de Icom: cada comando es

    FE FE <dir_radio> <dir_PC=E0> <comando> [subcomando] [datos] FD

y el radio responde con los datos pedidos, con FB (aceptado) o FA (rechazado).

Uso rápido (Windows, el radio en COM3):

    python g1m_cat.py puertos
    python g1m_cat.py detectar  --puerto COM3
    python g1m_cat.py sondear   --puerto COM3 --baudios 19200
    python g1m_cat.py estado    --puerto COM3 --baudios 19200
    python g1m_cat.py portadora --puerto COM3 --baudios 19200 --freqs 7.030,14.060

Con --puerto SIM se usa un radio simulado para probar el programa sin equipo.
"""

import argparse
import datetime
import json
import sys
import time

DIR_PC = 0xE0
FIN = 0xFD
OK = 0xFB
NG = 0xFA

MODOS = {
    0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW",
    0x04: "RTTY", 0x05: "FM", 0x07: "CW-R", 0x08: "RTTY-R",
}
MODOS_POR_NOMBRE = {v: k for k, v in MODOS.items()}

BAUDIOS = [19200, 9600, 115200, 38400, 57600, 4800]
# 0x70 = IC-7000 (lo que sugiere el manual), 0x88 = IC-7100 (perfil G90 en flrig),
# 0x94 = IC-7300, 0xA4 = IC-705, 0x00 = difusión.
DIRECCIONES = [0x70, 0x88, 0x94, 0xA4, 0x90, 0x00]

# Comandos de solo lectura que se prueban en el sondeo: (nombre, bytes del comando).
LECTURAS = [
    ("leer_frecuencia", b"\x03"),
    ("leer_modo", b"\x04"),
    ("limites_de_banda", b"\x02"),
    ("id_transceptor", b"\x19\x00"),
    ("nivel_AF", b"\x14\x01"),
    ("nivel_RF_ganancia", b"\x14\x02"),
    ("nivel_squelch", b"\x14\x03"),
    ("nivel_potencia_RF", b"\x14\x0A"),
    ("velocidad_keyer", b"\x14\x0C"),
    ("ganancia_micro", b"\x14\x0B"),
    ("medidor_S", b"\x15\x02"),
    ("medidor_potencia_Po", b"\x15\x11"),
    ("medidor_ROE_SWR", b"\x15\x12"),
    ("medidor_ALC", b"\x15\x13"),
    ("medidor_voltaje", b"\x15\x15"),
    ("medidor_corriente", b"\x15\x16"),
    ("preamplificador", b"\x16\x02"),
    ("AGC", b"\x16\x12"),
    ("noise_blanker", b"\x16\x22"),
    ("reduccion_ruido", b"\x16\x40"),
    ("break_in_CW", b"\x16\x47"),
    ("atenuador", b"\x11"),
    ("split", b"\x0F"),
    ("estado_TX_PTT", b"\x1C\x00"),
    ("sintonizador_antena", b"\x1C\x01"),
    ("modo_datos", b"\x1A\x06"),
    ("frecuencia_VFO_sel", b"\x25\x00"),
    ("modo_VFO_sel", b"\x26\x00"),
]


# ----------------------------------------------------------------------------
# Conversión BCD (CI-V manda la frecuencia en BCD con el byte menos
# significativo primero: 7.074.000 Hz -> 00 40 07 07 00)
# ----------------------------------------------------------------------------

def freq_a_bcd(hz):
    s = f"{int(round(hz)):010d}"
    return bytes(int(s[i:i + 2], 16) for i in range(8, -1, -2))


def bcd_a_freq(datos):
    return int("".join(f"{b:02x}" for b in reversed(datos)))


def nivel_a_bcd(n):
    return bytes.fromhex(f"{int(n):04d}")


def bcd_a_nivel(datos):
    return int(datos.hex())


def hexa(b):
    return " ".join(f"{x:02X}" for x in b) if b is not None else None


# ----------------------------------------------------------------------------
# Puerto serie
# ----------------------------------------------------------------------------

def abrir_puerto(nombre, baudios, rts=False, dtr=False):
    if nombre.upper() == "SIM":
        return RadioSimulado(baudios)
    try:
        import serial
    except ImportError:
        sys.exit("Falta pyserial. Instálalo con:  pip install pyserial")
    ser = serial.Serial()
    ser.port = nombre
    ser.baudrate = baudios
    ser.bytesize = 8
    ser.parity = "N"
    ser.stopbits = 1
    ser.timeout = 0.02
    # Muchos cables usan RTS o DTR para PTT o CW: se dejan apagados por defecto
    # para no transmitir al abrir el puerto.
    ser.rts = rts
    ser.dtr = dtr
    ser.open()
    return ser


class CIV:
    def __init__(self, puerto, dir_radio, espera=0.4, verbose=False):
        self.puerto = puerto
        self.dir_radio = dir_radio
        self.espera = espera
        self.verbose = verbose

    def trama(self, cuerpo):
        return bytes([0xFE, 0xFE, self.dir_radio, DIR_PC]) + bytes(cuerpo) + bytes([FIN])

    def enviar(self, cuerpo):
        """Envía un comando y devuelve el cuerpo de la respuesta del radio
        (sin preámbulo, direcciones ni FD), o None si no contestó."""
        salida = self.trama(cuerpo)
        self.puerto.reset_input_buffer()
        self.puerto.write(salida)
        if self.verbose:
            print(f"  -> {hexa(salida)}")
        buf = bytearray()
        limite = time.monotonic() + self.espera
        while time.monotonic() < limite:
            buf += self.puerto.read(self.puerto.in_waiting or 1)
            for t in self._tramas(buf):
                # El bus CI-V devuelve un eco de lo que enviamos: se ignora.
                if t == salida:
                    continue
                if len(t) >= 6 and t[2] in (DIR_PC, 0x00):
                    if self.verbose:
                        print(f"  <- {hexa(t)}")
                    return bytes(t[4:-1])
        if self.verbose:
            print(f"  <- (sin respuesta) {hexa(buf) if buf else ''}")
        return None

    @staticmethod
    def _tramas(buf):
        tramas = []
        i = 0
        while True:
            ini = buf.find(b"\xFE\xFE", i)
            if ini < 0:
                break
            while ini + 2 < len(buf) and buf[ini + 2] == 0xFE:
                ini += 1  # preámbulo con más de dos FE
            fin = buf.find(bytes([FIN]), ini)
            if fin < 0:
                break
            tramas.append(bytes(buf[ini:fin + 1]))
            i = fin + 1
        return tramas

    def _dato(self, cmd):
        r = self.enviar(cmd)
        if r is None or r[:1] == bytes([NG]) or not r.startswith(cmd):
            return None
        return r[len(cmd):]

    def _ordena(self, cuerpo):
        r = self.enviar(cuerpo)
        if r is None:
            return None
        return r[:1] == bytes([OK])

    # --- lecturas ---
    def leer_freq(self):
        d = self._dato(b"\x03")
        return bcd_a_freq(d[:5]) if d and len(d) >= 5 else None

    def leer_modo(self):
        d = self._dato(b"\x04")
        if not d:
            return None, None
        return MODOS.get(d[0], f"0x{d[0]:02X}"), (d[1] if len(d) > 1 else None)

    def leer_nivel(self, sub):
        d = self._dato(bytes([0x14, sub]))
        return bcd_a_nivel(d[:2]) if d and len(d) >= 2 else None

    def leer_medidor(self, sub):
        d = self._dato(bytes([0x15, sub]))
        return bcd_a_nivel(d[:2]) if d and len(d) >= 2 else None

    def leer_ptt(self):
        d = self._dato(b"\x1C\x00")
        return bool(d[0]) if d else None

    # --- órdenes ---
    def poner_freq(self, hz):
        return self._ordena(b"\x05" + freq_a_bcd(hz))

    def poner_modo(self, nombre, filtro=1):
        # El G1M rechaza (FA) el comando 06 sin el byte de filtro: siempre se manda.
        cuerpo = bytes([0x06, MODOS_POR_NOMBRE[nombre]])
        if filtro is not None:
            cuerpo += bytes([filtro])
        return self._ordena(cuerpo)

    def poner_nivel(self, sub, valor):
        return self._ordena(bytes([0x14, sub]) + nivel_a_bcd(valor))

    def ptt(self, encendido):
        return self._ordena(bytes([0x1C, 0x00, 0x01 if encendido else 0x00]))


# ----------------------------------------------------------------------------
# Radio simulado (para probar el programa sin el equipo)
# ----------------------------------------------------------------------------

class RadioSimulado:
    """Emula un radio CI-V sencillo que solo contesta a 19200 baudios."""

    def __init__(self, baudios):
        self.baudios = baudios
        self.salida = bytearray()
        self.freq = 7_074_000
        self.modo = 0x01
        self.potencia = 255
        self.tx = False

    @property
    def in_waiting(self):
        return len(self.salida)

    def reset_input_buffer(self):
        self.salida.clear()

    def read(self, n=1):
        d = bytes(self.salida[:n])
        del self.salida[:n]
        return d

    def close(self):
        pass

    def write(self, trama):
        self.salida += trama  # eco del bus
        if self.baudios != 19200:
            return
        radio, pc, cuerpo = trama[2], trama[3], trama[4:-1]
        resp = self._procesar(cuerpo)
        self.salida += bytes([0xFE, 0xFE, pc, radio or 0x70]) + resp + bytes([FIN])

    def _procesar(self, c):
        if c == b"\x03":
            return c + freq_a_bcd(self.freq)
        if c == b"\x04":
            return c + bytes([self.modo, 0x01])
        if c[:1] == b"\x05" and len(c) == 6:
            self.freq = bcd_a_freq(c[1:])
            return bytes([OK])
        if c[:1] == b"\x06" and len(c) == 3:
            self.modo = c[1]
            return bytes([OK])
        if c == b"\x14\x0A":
            return c + nivel_a_bcd(self.potencia)
        if c[:2] == b"\x14\x0A" and len(c) == 4:
            self.potencia = bcd_a_nivel(c[2:])
            return bytes([OK])
        if c == b"\x15\x02":
            return c + nivel_a_bcd(120)
        if c == b"\x15\x11":
            return c + nivel_a_bcd(self.potencia * 0.6 if self.tx and self.modo in (0x02, 0x03) else 0)
        if c == b"\x15\x12":
            return c + nivel_a_bcd(30 if self.tx else 0)
        if c == b"\x1C\x00":
            return c + bytes([int(self.tx)])
        if c[:2] == b"\x1C\x00" and len(c) == 3:
            self.tx = bool(c[2])
            return bytes([OK])
        if c == b"\x19\x00":
            return c + bytes([0x70])
        return bytes([NG])


# ----------------------------------------------------------------------------
# Comandos del programa
# ----------------------------------------------------------------------------

def conectar(args):
    puerto = abrir_puerto(args.puerto, args.baudios, args.rts, args.dtr)
    return puerto, CIV(puerto, args.dir, verbose=args.verbose)


def cmd_puertos(args):
    try:
        from serial.tools import list_ports
    except ImportError:
        sys.exit("Falta pyserial. Instálalo con:  pip install pyserial")
    puertos = list(list_ports.comports())
    if not puertos:
        print("No se encontraron puertos serie.")
    for p in puertos:
        print(f"{p.device:10s} {p.description}")


def cmd_detectar(args):
    print(f"Buscando el radio en {args.puerto}...")
    for b in BAUDIOS:
        try:
            puerto = abrir_puerto(args.puerto, b, args.rts, args.dtr)
        except Exception as e:
            sys.exit(f"No se pudo abrir {args.puerto}: {e}")
        try:
            for d in DIRECCIONES:
                civ = CIV(puerto, d, verbose=args.verbose)
                f = civ.leer_freq()
                print(f"  {b:6d} baudios, dirección 0x{d:02X}: "
                      + (f"RESPONDE, frecuencia {f / 1e6:.6f} MHz" if f else "sin respuesta"))
                if f:
                    print(f"\nUsa:  --baudios {b} --dir 0x{d:02X}")
                    return
        finally:
            puerto.close()
    print("\nEl radio no respondió. Revisa el cable al puerto COMM, que el radio esté "
          "encendido y la velocidad CI-V/CAT en el menú del G1M. Prueba también con "
          "--rts o --dtr si tu cable los necesita para alimentarse.")


def cmd_estado(args):
    puerto, civ = conectar(args)
    try:
        f = civ.leer_freq()
        modo, filtro = civ.leer_modo()
        print(f"Frecuencia : {f / 1e6:.6f} MHz" if f else "Frecuencia : (sin respuesta)")
        print(f"Modo       : {modo} (filtro {filtro})")
        print(f"Potencia RF: {civ.leer_nivel(0x0A)}  (0-255)")
        print(f"Medidor S  : {civ.leer_medidor(0x02)}")
        print(f"PTT        : {civ.leer_ptt()}")
    finally:
        puerto.close()


def cmd_freq(args):
    puerto, civ = conectar(args)
    try:
        ok = civ.poner_freq(args.mhz * 1e6)
        print(f"Poner {args.mhz} MHz: {resultado(ok)}; el radio dice {civ.leer_freq()} Hz")
    finally:
        puerto.close()


def cmd_modo(args):
    puerto, civ = conectar(args)
    try:
        print(f"Poner modo {args.modo}: {resultado(civ.poner_modo(args.modo))}; "
              f"el radio dice {civ.leer_modo()[0]}")
    finally:
        puerto.close()


def cmd_potencia(args):
    puerto, civ = conectar(args)
    try:
        print(f"Poner potencia {args.valor}: {resultado(civ.poner_nivel(0x0A, args.valor))}; "
              f"el radio dice {civ.leer_nivel(0x0A)}")
    finally:
        puerto.close()


def resultado(ok):
    return {True: "aceptado (FB)", False: "rechazado (FA)", None: "sin respuesta"}[ok]


def cmd_sondear(args):
    puerto, civ = conectar(args)
    informe = {
        "fecha": datetime.datetime.now().isoformat(timespec="seconds"),
        "puerto": args.puerto, "baudios": args.baudios, "direccion": f"0x{args.dir:02X}",
        "lecturas": {}, "escrituras": {}, "barrido": {},
    }
    try:
        print("== Lecturas ==")
        for nombre, cmd in LECTURAS:
            r = civ.enviar(cmd)
            estado = ("sin respuesta" if r is None else
                      "rechazado (FA)" if r[:1] == bytes([NG]) else
                      "aceptado (FB)" if r[:1] == bytes([OK]) else "datos")
            informe["lecturas"][nombre] = {"comando": hexa(cmd), "estado": estado, "respuesta": hexa(r)}
            print(f"  {nombre:24s} {hexa(cmd):8s} {estado:16s} {hexa(r) or ''}")

        print("== Escrituras (se escribe el mismo valor que ya tiene el radio) ==")
        f = civ.leer_freq()
        if f:
            informe["escrituras"]["poner_frecuencia_05"] = resultado(civ.poner_freq(f))
        modo, filtro = civ.leer_modo()
        if modo in MODOS_POR_NOMBRE:
            informe["escrituras"]["poner_modo_06"] = resultado(civ.poner_modo(modo, None))
            informe["escrituras"]["poner_modo_con_filtro_06"] = resultado(civ.poner_modo(modo, filtro or 1))
        p = civ.leer_nivel(0x0A)
        if p is not None:
            informe["escrituras"]["poner_potencia_14_0A"] = resultado(civ.poner_nivel(0x0A, p))
        for k, v in informe["escrituras"].items():
            print(f"  {k:28s} {v}")

        if args.amplio:
            print("== Barrido de subcomandos de lectura 14 xx, 15 xx, 16 xx ==")
            for cmd in (0x14, 0x15, 0x16):
                for sub in range(0x100):
                    r = civ.enviar(bytes([cmd, sub]))
                    if r is not None and r[:1] != bytes([NG]):
                        informe["barrido"][f"{cmd:02X} {sub:02X}"] = hexa(r)
                        print(f"  {cmd:02X} {sub:02X} -> {hexa(r)}")
    finally:
        puerto.close()

    nombre = args.salida or f"g1m_sondeo_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(nombre, "w", encoding="utf-8") as fh:
        json.dump(informe, fh, indent=2, ensure_ascii=False)
    print(f"\nInforme guardado en {nombre}")


def cmd_portadora(args):
    freqs = [float(x) * 1e6 for x in args.freqs.split(",") if x.strip()]
    segundos = min(args.segundos, 30)
    print(f"Se va a TRANSMITIR en modo {args.modo}, {segundos} s en cada frecuencia: "
          + ", ".join(f"{f / 1e6:.4f} MHz" for f in freqs))
    print("Conecta una carga ficticia de 50 ohm o una antena ajustada, y usa solo "
          "frecuencias para las que tengas licencia.")
    if not args.confirmo and input("Escribe TX para continuar: ").strip().upper() != "TX":
        print("Cancelado.")
        return

    puerto, civ = conectar(args)
    modo_orig, filtro_orig = civ.leer_modo()
    original = (civ.leer_freq(), modo_orig, civ.leer_nivel(0x0A))
    # En el G1M V1.07 los medidores 15 11 (Po) y 15 12 (ROE) devuelven un valor
    # fijo aunque no transmita: si en recepción Po no es 0, no son medidas reales.
    po_rx = civ.leer_medidor(0x11)
    if po_rx:
        print(f"Aviso: el medidor Po marca {po_rx} sin transmitir; el radio no da medidas "
              "reales por CAT. Mide la potencia con un wattímetro externo.")
    resultados = []
    try:
        print(f"Modo {args.modo}: {resultado(civ.poner_modo(args.modo))}")
        if args.potencia is not None:
            print(f"Potencia {args.potencia}: {resultado(civ.poner_nivel(0x0A, args.potencia))}")
        for f in freqs:
            r_f = civ.poner_freq(f)
            time.sleep(args.espera_ms / 1000)  # que se asienten el oscilador y los filtros de banda
            leida = civ.leer_freq()
            r_ptt = civ.ptt(True)
            muestras = []
            fin = time.monotonic() + segundos
            while time.monotonic() < fin:
                muestras.append({"TX": civ.leer_ptt(), "Po": civ.leer_medidor(0x11),
                                 "SWR": civ.leer_medidor(0x12)})
                time.sleep(0.2)
            civ.ptt(False)
            pos = [m["Po"] for m in muestras if m["Po"] is not None]
            tx = [m["TX"] for m in muestras if m["TX"] is not None]
            en_tx = (all(tx) and "sí") if tx else "sin dato"
            en_tx = en_tx or "NO"
            fila = {"pedida_hz": int(f), "leida_hz": leida, "poner_freq": resultado(r_f),
                    "ptt": resultado(r_ptt), "radio_en_TX": en_tx,
                    "Po_max": max(pos) if pos else None, "muestras": muestras}
            resultados.append(fila)
            po = "medir con wattímetro" if po_rx else fila["Po_max"]
            print(f"  {f / 1e6:9.4f} MHz  leída:{(leida or 0) / 1e6:9.4f}  ptt:{resultado(r_ptt):14s} "
                  f"radio en TX: {en_tx:8s} Po: {po}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nInterrumpido.")
    finally:
        civ.ptt(False)
        if original[0]:
            civ.poner_freq(original[0])
        if original[1] in MODOS_POR_NOMBRE:
            civ.poner_modo(original[1], filtro_orig or 1)
        if original[2] is not None and args.potencia is not None:
            civ.poner_nivel(0x0A, original[2])
        print(f"PTT apagado; estado original restaurado. PTT ahora: {civ.leer_ptt()}")
        puerto.close()

    nombre = f"g1m_portadora_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(nombre, "w", encoding="utf-8") as fh:
        json.dump({"modo": args.modo, "potencia": args.potencia, "resultados": resultados},
                  fh, indent=2, ensure_ascii=False)
    print(f"Resultados guardados en {nombre}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Control y sondeo CI-V del Xiegu G1M")
    sub = p.add_subparsers(dest="orden", required=True)

    def comunes(sp, con_baudios=True):
        sp.add_argument("--puerto", required=True, help="COM3, /dev/ttyUSB0 o SIM")
        if con_baudios:
            sp.add_argument("--baudios", type=int, default=19200)
            sp.add_argument("--dir", type=lambda s: int(s, 0), default=0x70,
                            help="dirección CI-V del radio (por defecto 0x70)")
        sp.add_argument("--rts", action="store_true", help="activar RTS al abrir el puerto")
        sp.add_argument("--dtr", action="store_true", help="activar DTR al abrir el puerto")
        sp.add_argument("-v", "--verbose", action="store_true", help="mostrar las tramas en hexadecimal")

    sp = sub.add_parser("puertos", help="listar puertos serie")
    sp.set_defaults(fn=cmd_puertos)

    sp = sub.add_parser("detectar", help="encontrar velocidad y dirección CI-V")
    comunes(sp, con_baudios=False)
    sp.set_defaults(fn=cmd_detectar)

    sp = sub.add_parser("sondear", help="probar qué comandos CI-V acepta el radio")
    comunes(sp)
    sp.add_argument("--amplio", action="store_true", help="barrer también 14/15/16 xx (unos minutos)")
    sp.add_argument("--salida", help="nombre del archivo JSON del informe")
    sp.set_defaults(fn=cmd_sondear)

    sp = sub.add_parser("estado", help="leer frecuencia, modo, potencia y PTT")
    comunes(sp)
    sp.set_defaults(fn=cmd_estado)

    sp = sub.add_parser("freq", help="cambiar frecuencia")
    comunes(sp)
    sp.add_argument("mhz", type=float)
    sp.set_defaults(fn=cmd_freq)

    sp = sub.add_parser("modo", help="cambiar modo")
    comunes(sp)
    sp.add_argument("modo", choices=list(MODOS_POR_NOMBRE))
    sp.set_defaults(fn=cmd_modo)

    sp = sub.add_parser("potencia", help="cambiar potencia RF (0-255)")
    comunes(sp)
    sp.add_argument("valor", type=int)
    sp.set_defaults(fn=cmd_potencia)

    sp = sub.add_parser("portadora", help="transmitir portadora y medir Po en cada frecuencia")
    comunes(sp)
    sp.add_argument("--freqs", required=True, help="lista en MHz separada por comas, ej. 7.030,14.060")
    sp.add_argument("--modo", default="CW", choices=list(MODOS_POR_NOMBRE))
    sp.add_argument("--potencia", type=int, help="potencia RF 0-255 (si el radio la acepta por CAT)")
    sp.add_argument("--segundos", type=float, default=2.0, help="tiempo en TX por frecuencia (máx. 30)")
    sp.add_argument("--espera-ms", type=int, default=150, help="pausa tras cambiar de frecuencia")
    sp.add_argument("--confirmo", action="store_true", help="no pedir confirmación antes de transmitir")
    sp.set_defaults(fn=cmd_portadora)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

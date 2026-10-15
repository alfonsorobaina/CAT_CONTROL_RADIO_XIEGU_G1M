#!/usr/bin/env python3
"""
Panel de control del Xiegu G1M por CAT (CI-V) con ventana.

Necesita g1m_cat.py en la misma carpeta y pyserial.

    python g1m_control.py

Solo hay que elegir el puerto en la lista y pulsar Conectar: la velocidad y la
dirección CI-V se detectan solas y se recuerdan para la próxima vez.

Sintonía:
  - Rueda del ratón sobre un dígito de la frecuencia: cambia ese dígito.
  - Arrastrar con el botón izquierdo sobre la frecuencia: sintonía rápida.
  - Dial: arrastrar en círculo o usar la rueda encima.
  - Teclado: flechas arriba/abajo (paso), RePág/AvPág (paso x10),
    escribir la frecuencia en MHz en la casilla y pulsar Enter.
"""

import argparse
import json
import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import g1m_cat as cat

FREQ_MIN = 100_000
FREQ_MAX = 30_000_000

BANDAS = [
    ("80 m", 3_560_000), ("60 m", 5_354_000), ("40 m", 7_030_000), ("30 m", 10_116_000),
    ("20 m", 14_060_000), ("17 m", 18_086_000), ("15 m", 21_060_000),
    ("12 m", 24_906_000), ("10 m", 28_060_000),
]
PASOS = [("10 Hz", 10), ("50 Hz", 50), ("100 Hz", 100), ("500 Hz", 500),
         ("1 kHz", 1_000), ("5 kHz", 5_000), ("10 kHz", 10_000), ("100 kHz", 100_000)]
MODOS_PANEL = ["LSB", "USB", "CW", "CW-R", "AM"]
AGC = {"Rápido": 0x01, "Medio": 0x02, "Lento": 0x03}

# Niveles del comando 14 xx (0-255): (nombre, subcomando).
# Probado en el G1M V1.07: volumen (14 01), ganancia RF (14 02) y squelch (14 03)
# se aceptan con FB pero no hacen nada; micro y velocidad CW devuelven el mismo
# valor de relleno. Por eso solo queda la potencia.
NIVELES = [
    ("Potencia TX", 0x0A),
]
# Interruptores: (nombre, comando sin el valor, valor encendido)
INTERRUPTORES = [
    ("Preamplificador", b"\x16\x02", 0x01),
    ("Noise blanker", b"\x16\x22", 0x01),
    ("Reducción ruido", b"\x16\x40", 0x01),
    ("Break-in CW", b"\x16\x47", 0x01),
]

CARPETA = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_MEMORIAS = os.path.join(CARPETA, "g1m_memorias.json")
ARCHIVO_CONFIG = os.path.join(CARPETA, "g1m_config.json")

# Tema claro
C_FONDO = "#e9edf2"
C_PANEL = "#f7f9fb"
C_TEXTO = "#1f2a37"
C_SUAVE = "#5f6b7a"
C_CAMPO = "#ffffff"
C_BOTON = "#e1e7ef"
C_BOTON_ACT = "#cfd8e4"
C_SEL = "#3f72c4"
C_LCD = "#e4edf7"
C_DIGITO = "#12407a"
C_DIGITO_SEL = "#c0392b"
C_VFO = "#3f72c4"
C_MODO = "#1e7e34"
C_SPLIT = "#b7791f"
C_TX = "#d63031"
C_RX = "#2e9e4f"
C_PTT = "#f2c9c9"
C_ERROR = "#c0392b"
C_OK = "#1e7e34"
C_AVISO = "#b7791f"


# ----------------------------------------------------------------------------
# Hilo de comunicación: todo el puerto serie pasa por aquí
# ----------------------------------------------------------------------------

class Enlace(threading.Thread):
    def __init__(self, civ, puerto, salida):
        super().__init__(daemon=True)
        self.civ = civ
        self.puerto = puerto
        self.salida = salida          # cola hacia la ventana
        self.trabajos = queue.Queue()
        self.cerrojo = threading.Lock()
        self.freq_pendiente = None    # se agrupan los cambios de frecuencia rápidos
        self.ultima_sintonia = 0.0
        self.sondeo_pausado = False
        self.activo = True

    def pedir_freq(self, hz):
        with self.cerrojo:
            self.freq_pendiente = int(hz)
        self.ultima_sintonia = time.monotonic()

    def trabajo(self, descripcion, funcion, *args):
        self.trabajos.put((descripcion, funcion, args))

    def detener(self):
        self.activo = False

    def run(self):
        ultimo_sondeo = 0.0
        while self.activo:
            with self.cerrojo:
                f, self.freq_pendiente = self.freq_pendiente, None
            if f is not None:
                ok = self.civ.poner_freq(f)
                if ok is not True:
                    self.salida.put(("log", f"Frecuencia {f / 1e6:.6f} MHz: {cat.resultado(ok)}"))
                continue
            try:
                descripcion, funcion, args = self.trabajos.get(timeout=0.04)
                try:
                    r = funcion(*args)
                except Exception as e:  # el puerto se cayó, cable suelto, etc.
                    r = f"error: {e}"
                if descripcion:
                    texto = cat.resultado(r) if r in (True, False, None) else r
                    self.salida.put(("log", f"{descripcion}: {texto}"))
                continue
            except queue.Empty:
                pass
            ahora = time.monotonic()
            if not self.sondeo_pausado and ahora - ultimo_sondeo > 0.35:
                ultimo_sondeo = ahora
                try:
                    estado = {
                        "freq": self.civ.leer_freq(),
                        "modo": self.civ.leer_modo(),
                        "tx": self.civ.leer_ptt(),
                        "s": self.civ.leer_medidor(0x02),
                        "reciente": ahora - self.ultima_sintonia < 1.0,
                    }
                    self.salida.put(("estado", estado))
                except Exception as e:
                    self.salida.put(("log", f"Error leyendo el radio: {e}"))
                    time.sleep(1)
        try:
            self.civ.ptt(False)
        except Exception:
            pass
        self.puerto.close()


# ----------------------------------------------------------------------------
# Ventana
# ----------------------------------------------------------------------------

class Panel:
    def __init__(self, raiz, args):
        self.raiz = raiz
        self.args = args
        self.enlace = None
        self.cola = queue.Queue()
        self.freq = 7_030_000
        self.modo = "USB"
        self.filtro = 1
        self.en_tx = False
        self.tx_desde = 0.0
        self.freq_rx_split = None
        self.ptt_local = 0.0
        self.ultimo_nivel = {}
        self.memorias = self._cargar_memorias()
        self.ultima_por_banda = {}
        self.retardos = {}

        raiz.title("Xiegu G1M – Control CAT")
        raiz.configure(bg=C_FONDO)
        raiz.minsize(940, 640)
        self._estilos()
        self._construir()
        self._pintar_freq()
        raiz.protocol("WM_DELETE_WINDOW", self.cerrar)
        raiz.after(50, self._atender_cola)
        raiz.after(500, self._vigilar_tx)
        if args.puerto:
            self.var_puerto.set(args.puerto)
            raiz.after(200, self.conectar)

    # --- aspecto ---
    def _estilos(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=C_PANEL, foreground=C_TEXTO, fieldbackground=C_CAMPO)
        st.configure("TFrame", background=C_PANEL)
        st.configure("Fondo.TFrame", background=C_FONDO)
        st.configure("TLabelframe", background=C_PANEL, foreground=C_TEXTO)
        st.configure("TLabelframe.Label", background=C_PANEL, foreground=C_SEL)
        st.configure("TLabel", background=C_PANEL, foreground=C_TEXTO)
        st.configure("TButton", background=C_BOTON, foreground=C_TEXTO, padding=4)
        st.map("TButton", background=[("active", C_BOTON_ACT)])
        st.configure("Sel.TButton", background=C_SEL, foreground="white")
        st.map("Sel.TButton", background=[("active", C_SEL)])
        st.configure("TCheckbutton", background=C_PANEL, foreground=C_TEXTO)
        st.configure("Horizontal.TScale", background=C_PANEL)
        st.configure("TNotebook", background=C_PANEL)
        st.configure("TNotebook.Tab", background=C_BOTON, foreground=C_SUAVE, padding=(12, 4))
        st.map("TNotebook.Tab", background=[("selected", C_SEL)], foreground=[("selected", "white")])
        st.map("TCombobox", fieldbackground=[("readonly", C_CAMPO)], foreground=[("readonly", C_TEXTO)])
        st.configure("TRadiobutton", background=C_PANEL, foreground=C_TEXTO)
        st.configure("TSpinbox", foreground=C_TEXTO)
        st.configure("TEntry", foreground=C_TEXTO)

    def _construir(self):
        r = self.raiz
        r.columnconfigure(0, weight=1)
        r.rowconfigure(2, weight=1)

        # Barra de conexión
        barra = ttk.Frame(r, padding=6)
        barra.grid(row=0, column=0, sticky="ew")
        ttk.Label(barra, text="Puerto").pack(side="left")
        self.var_puerto = tk.StringVar()
        self.cb_puerto = ttk.Combobox(barra, textvariable=self.var_puerto, width=38, state="readonly")
        self.cb_puerto.pack(side="left", padx=4)
        ttk.Button(barra, text="↻", width=3, command=self._refrescar_puertos).pack(side="left")
        self._refrescar_puertos()
        self.bt_conectar = ttk.Button(barra, text="Conectar", command=self.conectar)
        self.bt_conectar.pack(side="left", padx=6)
        self.lb_estado = ttk.Label(barra, text="Desconectado", foreground=C_ERROR)
        self.lb_estado.pack(side="left", padx=6)
        self.lb_tx = tk.Label(barra, text="  RX  ", bg=C_RX, fg="white",
                              font=("Segoe UI", 12, "bold"))
        self.lb_tx.pack(side="right", padx=6)

        # Zona superior: frecuencia + dial
        arriba = ttk.Frame(r, padding=6)
        arriba.grid(row=1, column=0, sticky="ew")
        arriba.columnconfigure(0, weight=1)
        self._construir_frecuencia(arriba)
        self._construir_dial(arriba)

        # Zona media: pestañas de controles
        medio = ttk.Frame(r, padding=(6, 0))
        medio.grid(row=2, column=0, sticky="nsew")
        medio.columnconfigure(0, weight=3)
        medio.columnconfigure(1, weight=2)
        medio.rowconfigure(0, weight=1)
        self._construir_controles(medio)
        self._construir_memorias(medio)

        # Registro
        self.txt_log = tk.Text(r, height=6, bg=C_CAMPO, fg=C_SUAVE, relief="flat",
                               font=("Consolas", 9))
        self.txt_log.grid(row=3, column=0, sticky="ew", padx=6, pady=6)

    def _construir_frecuencia(self, padre):
        marco = tk.Frame(padre, bg=C_LCD, bd=1, relief="solid", highlightbackground="#b8c6d8")
        marco.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        fila = tk.Frame(marco, bg=C_LCD)
        fila.pack(padx=10, pady=(8, 0))
        self.etq_vfo = tk.Label(fila, text="VFO A", bg=C_LCD, fg=C_VFO,
                                font=("Segoe UI", 11, "bold"))
        self.etq_vfo.pack(side="left", padx=(0, 10), anchor="n")
        # 8 dígitos: decenas de MHz ... unidades de Hz -> XX.XXX.XXX
        self.digitos = []
        formato = [7, 6, ".", 5, 4, 3, ".", 2, 1, 0]
        for pos in formato:
            if pos == ".":
                tk.Label(fila, text=".", bg=C_LCD, fg=C_DIGITO,
                         font=("Consolas", 46, "bold")).pack(side="left")
                continue
            lb = tk.Label(fila, text="0", bg=C_LCD, fg=C_DIGITO, font=("Consolas", 46, "bold"),
                          cursor="sb_v_double_arrow")
            lb.pack(side="left")
            lb.potencia = 10 ** pos
            lb.bind("<MouseWheel>", lambda e, l=lb: self._rueda_digito(e, l))
            lb.bind("<Button-4>", lambda e, l=lb: self._mover(l.potencia))
            lb.bind("<Button-5>", lambda e, l=lb: self._mover(-l.potencia))
            lb.bind("<Enter>", lambda e, l=lb: l.configure(fg=C_DIGITO_SEL))
            lb.bind("<Leave>", lambda e, l=lb: l.configure(fg=C_DIGITO))
            lb.bind("<ButtonPress-1>", self._arrastre_inicio)
            lb.bind("<B1-Motion>", self._arrastre)
            self.digitos.append(lb)
        tk.Label(fila, text="MHz", bg=C_LCD, fg=C_SUAVE, font=("Segoe UI", 12)).pack(
            side="left", padx=6, anchor="s", pady=12)

        info = tk.Frame(marco, bg=C_LCD)
        info.pack(fill="x", padx=12, pady=(0, 4))
        self.etq_modo = tk.Label(info, text="USB", bg=C_LCD, fg=C_MODO,
                                 font=("Segoe UI", 14, "bold"))
        self.etq_modo.pack(side="left")
        self.etq_split = tk.Label(info, text="", bg=C_LCD, fg=C_SPLIT, font=("Segoe UI", 11))
        self.etq_split.pack(side="left", padx=12)
        # Medidor S
        self.cv_s = tk.Canvas(info, width=260, height=18, bg="#d5dde7", highlightthickness=0)
        self.cv_s.pack(side="right")
        tk.Label(info, text="S", bg=C_LCD, fg=C_SUAVE).pack(side="right", padx=4)

        ent = tk.Frame(marco, bg=C_LCD)
        ent.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(ent, text="Ir a (MHz o kHz):", bg=C_LCD, fg=C_SUAVE).pack(side="left")
        self.var_entrada = tk.StringVar()
        e = ttk.Entry(ent, textvariable=self.var_entrada, width=12)
        e.pack(side="left", padx=4)
        e.bind("<Return>", self._ir_a)
        tk.Label(ent, text="Paso:", bg=C_LCD, fg=C_SUAVE).pack(side="left", padx=(12, 2))
        self.var_paso = tk.StringVar(value="100 Hz")
        ttk.Combobox(ent, textvariable=self.var_paso, width=8, state="readonly",
                     values=[p[0] for p in PASOS]).pack(side="left")
        tk.Label(ent, text="  Rueda sobre un dígito o arrastra la frecuencia para sintonizar",
                 bg=C_LCD, fg="#8a96a6").pack(side="left")

        self.raiz.bind("<Up>", lambda e: self._mover(self._paso()))
        self.raiz.bind("<Down>", lambda e: self._mover(-self._paso()))
        self.raiz.bind("<Prior>", lambda e: self._mover(10 * self._paso()))
        self.raiz.bind("<Next>", lambda e: self._mover(-10 * self._paso()))

    def _construir_dial(self, padre):
        self.cv_dial = tk.Canvas(padre, width=170, height=170, bg=C_PANEL, highlightthickness=0,
                                 cursor="exchange")
        self.cv_dial.grid(row=0, column=1)
        self.dial_angulo = 0.0
        self._dibujar_dial()
        self.cv_dial.bind("<ButtonPress-1>", self._dial_inicio)
        self.cv_dial.bind("<B1-Motion>", self._dial_mover)
        self.cv_dial.bind("<MouseWheel>", lambda e: self._mover(self._paso() * (1 if e.delta > 0 else -1)))
        self.cv_dial.bind("<Button-4>", lambda e: self._mover(self._paso()))
        self.cv_dial.bind("<Button-5>", lambda e: self._mover(-self._paso()))

    def _construir_controles(self, padre):
        nb = ttk.Notebook(padre)
        nb.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # --- Pestaña Operación ---
        op = ttk.Frame(nb, padding=8)
        nb.add(op, text="Operación")

        fb = ttk.LabelFrame(op, text="Bandas", padding=6)
        fb.pack(fill="x")
        for i, (nombre, f) in enumerate(BANDAS):
            ttk.Button(fb, text=nombre, width=6, command=lambda f=f, n=nombre: self._banda(n, f)).grid(
                row=i // 5, column=i % 5, padx=2, pady=2)

        fm = ttk.LabelFrame(op, text="Modo y filtro", padding=6)
        fm.pack(fill="x", pady=6)
        self.bt_modos = {}
        for i, m in enumerate(MODOS_PANEL):
            b = ttk.Button(fm, text=m, width=6, command=lambda m=m: self.poner_modo(m))
            b.grid(row=0, column=i, padx=2)
            self.bt_modos[m] = b
        ttk.Label(fm, text="Filtro").grid(row=1, column=0, pady=(6, 0))
        self.var_filtro = tk.IntVar(value=1)
        for i in (1, 2, 3):
            ttk.Radiobutton(fm, text=str(i), value=i, variable=self.var_filtro,
                            command=lambda: self.poner_modo(self.modo)).grid(row=1, column=i, pady=(6, 0))

        fv = ttk.LabelFrame(op, text="VFO y split", padding=6)
        fv.pack(fill="x")
        ttk.Button(fv, text="VFO A", command=lambda: self._vfo(0x00, "VFO A")).grid(row=0, column=0, padx=2)
        ttk.Button(fv, text="VFO B", command=lambda: self._vfo(0x01, "VFO B")).grid(row=0, column=1, padx=2)
        ttk.Button(fv, text="A = B", command=lambda: self._orden("A = B", b"\x07\xA0")).grid(row=0, column=2, padx=2)
        ttk.Button(fv, text="A ↔ B", command=lambda: self._orden("A ↔ B", b"\x07\xB0")).grid(row=0, column=3, padx=2)
        self.var_split = tk.BooleanVar(value=False)
        ttk.Checkbutton(fv, text="Split (por software)", variable=self.var_split,
                        command=self._split).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(fv, text="TX en MHz").grid(row=1, column=2, pady=(6, 0))
        self.var_freq_tx = tk.StringVar()
        ttk.Entry(fv, textvariable=self.var_freq_tx, width=11).grid(row=1, column=3, pady=(6, 0))
        ttk.Button(fv, text="TX = RX", command=lambda: self.var_freq_tx.set(f"{self.freq / 1e6:.6f}")).grid(
            row=1, column=4, padx=4, pady=(6, 0))

        ftx = ttk.LabelFrame(op, text="Transmisión", padding=6)
        ftx.pack(fill="x", pady=6)
        self.bt_ptt = tk.Button(ftx, text="PTT", width=10, bg=C_PTT, fg=C_TX,
                                font=("Segoe UI", 12, "bold"), relief="raised")
        self.bt_ptt.grid(row=0, column=0, rowspan=2, padx=4)
        self.bt_ptt.bind("<ButtonPress-1>", lambda e: self.ptt(True) if not self.var_ptt_fijo.get() else None)
        self.bt_ptt.bind("<ButtonRelease-1>", self._ptt_soltar)
        self.var_ptt_fijo = tk.BooleanVar(value=False)
        ttk.Checkbutton(ftx, text="Modo interruptor (clic = TX, otro clic = RX)",
                        variable=self.var_ptt_fijo).grid(row=0, column=1, sticky="w")
        ttk.Label(ftx, text="Tiempo máx. TX (s)").grid(row=1, column=1, sticky="w")
        self.var_tx_max = tk.IntVar(value=120)
        ttk.Spinbox(ftx, from_=5, to=600, textvariable=self.var_tx_max, width=5).grid(
            row=1, column=1, sticky="e")
        ttk.Label(ftx, foreground=C_SUAVE, wraplength=430,
                  text="El G1M transmite señal solo con audio en SSB/AM (micrófono) o con la llave en CW. "
                       "El PTT por CAT conmuta a transmisión pero no genera portadora por sí mismo.").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- Pestaña Ajustes (menú) ---
        aj = ttk.Frame(nb, padding=8)
        nb.add(aj, text="Ajustes")
        self.escalas = {}
        for i, (nombre, sub) in enumerate(NIVELES):
            ttk.Label(aj, text=nombre, width=16).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.IntVar(value=0)
            esc = ttk.Scale(aj, from_=0, to=255, orient="horizontal", length=260, variable=var,
                            command=lambda v, s=sub, n=nombre: self._nivel_cambiado(n, s))
            esc.grid(row=i, column=1, padx=6)
            lb = ttk.Label(aj, text="0", width=4)
            lb.grid(row=i, column=2)
            self.escalas[sub] = (var, lb)

        fila = len(NIVELES)
        ttk.Label(aj, text="AGC", width=16).grid(row=fila, column=0, sticky="w", pady=6)
        self.var_agc = tk.StringVar(value="Lento")
        cb = ttk.Combobox(aj, textvariable=self.var_agc, values=list(AGC), state="readonly", width=10)
        cb.grid(row=fila, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>",
                lambda e: self._orden(f"AGC {self.var_agc.get()}", b"\x16\x12" + bytes([AGC[self.var_agc.get()]])))

        self.vars_interruptor = {}
        fi = ttk.Frame(aj)
        fi.grid(row=fila + 1, column=0, columnspan=3, sticky="w", pady=6)
        for i, (nombre, cmd, val) in enumerate(INTERRUPTORES):
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(fi, text=nombre, variable=var,
                            command=lambda n=nombre, c=cmd, v=val, x=var:
                            self._orden(f"{n} {'ON' if x.get() else 'OFF'}", c + bytes([v if x.get() else 0]))
                            ).grid(row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)
            self.vars_interruptor[cmd] = (var, val)

        ttk.Button(aj, text="Leer ajustes del radio", command=self.leer_ajustes).grid(
            row=fila + 2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(aj, foreground=C_SUAVE, wraplength=430,
                  text="Volumen, ganancia RF, squelch y atenuador no se pueden controlar por CAT en el "
                       "G1M: se ajustan con los mandos del radio.").grid(
            row=fila + 3, column=0, columnspan=3, sticky="w")

    def _construir_memorias(self, padre):
        fm = ttk.LabelFrame(padre, text="Memorias", padding=6)
        fm.grid(row=0, column=1, sticky="nsew")
        fm.rowconfigure(0, weight=1)
        fm.columnconfigure(0, weight=1)
        self.lst_mem = tk.Listbox(fm, bg=C_CAMPO, fg=C_TEXTO, selectbackground=C_SEL, selectforeground="white",
                                  font=("Consolas", 10), activestyle="none")
        self.lst_mem.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.lst_mem.bind("<Double-Button-1>", lambda e: self._mem_recuperar())
        self.var_mem_nombre = tk.StringVar()
        ttk.Entry(fm, textvariable=self.var_mem_nombre).grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(fm, text="Guardar", command=self._mem_guardar).grid(row=2, column=0, sticky="ew")
        ttk.Button(fm, text="Ir", command=self._mem_recuperar).grid(row=2, column=1, sticky="ew", padx=4)
        ttk.Button(fm, text="Borrar", command=self._mem_borrar).grid(row=2, column=2, sticky="ew")
        self._mem_refrescar()

    # --- utilidades ---
    def _refrescar_puertos(self):
        """Lista los puertos con su descripción; el último usado queda elegido."""
        self.puertos = {}
        try:
            from serial.tools import list_ports
            for p in sorted(list_ports.comports(), key=lambda p: p.device):
                self.puertos[f"{p.device}  –  {p.description}"] = p.device
        except Exception:
            pass
        self.puertos["SIM  –  radio simulado (sin equipo)"] = "SIM"
        nombres = list(self.puertos)
        self.cb_puerto.configure(values=nombres)
        ultimo = self._config().get("puerto")
        elegido = next((n for n in nombres if self.puertos[n] == ultimo), None)
        if not elegido:  # el primero que no sea Bluetooth
            elegido = next((n for n in nombres if "bluetooth" not in n.lower()), nombres[0])
        self.var_puerto.set(elegido)

    def _puerto_elegido(self):
        texto = self.var_puerto.get()
        return self.puertos.get(texto, texto.split()[0] if texto else "")

    def _config(self):
        try:
            with open(ARCHIVO_CONFIG, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _paso(self):
        return dict(PASOS).get(self.var_paso.get(), 100)

    def log(self, texto):
        self.txt_log.insert("end", time.strftime("%H:%M:%S ") + texto + "\n")
        self.txt_log.see("end")
        if int(self.txt_log.index("end-1c").split(".")[0]) > 500:
            self.txt_log.delete("1.0", "100.0")

    def _necesita_enlace(self):
        if not self.enlace:
            self.log("No hay conexión con el radio.")
            return False
        return True

    def _orden(self, descripcion, cuerpo):
        if self._necesita_enlace():
            self.enlace.trabajo(descripcion, self.enlace.civ.orden, cuerpo)

    def _retrasar(self, clave, ms, funcion):
        """Agrupa cambios seguidos (por ejemplo al mover un deslizador)."""
        if clave in self.retardos:
            self.raiz.after_cancel(self.retardos[clave])
        self.retardos[clave] = self.raiz.after(ms, funcion)

    # --- conexión ---
    def conectar(self):
        if self.enlace:
            self.desconectar()
            return
        nombre = self._puerto_elegido()
        if not nombre:
            return
        cfg = self._config()
        preferido = (cfg.get("baudios"), cfg.get("direccion")) if cfg.get("puerto") == nombre else None
        self.bt_conectar.configure(state="disabled")
        self.lb_estado.configure(text=f"Buscando el radio en {nombre}...", foreground=C_AVISO)

        def buscar():
            try:
                r = cat.buscar_radio(nombre, preferido)
                self.cola.put(("conexion", (nombre, r)))
            except Exception as e:
                self.cola.put(("conexion", (nombre, e)))
        threading.Thread(target=buscar, daemon=True).start()

    def _conexion_lista(self, nombre, r):
        self.bt_conectar.configure(state="normal")
        if isinstance(r, Exception) or r is None:
            self.lb_estado.configure(text="Desconectado", foreground=C_ERROR)
            if r is None:
                messagebox.showwarning("G1M", f"No se encontró el radio en {nombre}.\n\n"
                                       "Comprueba que esté encendido, que el cable vaya al puerto COMM "
                                       "y que sea el puerto correcto.")
            else:
                messagebox.showerror("G1M", f"No se pudo abrir {nombre}:\n{r}")
            return
        puerto, civ, f, baudios, direccion = r
        try:
            with open(ARCHIVO_CONFIG, "w", encoding="utf-8") as fh:
                json.dump({"puerto": nombre, "baudios": baudios, "direccion": direccion}, fh)
        except OSError:
            pass
        self.enlace = Enlace(civ, puerto, self.cola)
        self.enlace.start()
        self.freq = f
        self._pintar_freq()
        self.lb_estado.configure(text=f"Conectado a {nombre}", foreground=C_OK)
        self.bt_conectar.configure(text="Desconectar")
        self.log(f"Conectado en {nombre} ({baudios} baudios, dirección 0x{direccion:02X}). "
                 f"Frecuencia {f / 1e6:.6f} MHz")
        self.leer_ajustes()

    def desconectar(self):
        if self.en_tx:
            self.ptt(False)
        self.enlace.detener()
        self.enlace = None
        self.lb_estado.configure(text="Desconectado", foreground=C_ERROR)
        self.bt_conectar.configure(text="Conectar")
        self.log("Desconectado.")

    def leer_ajustes(self):
        if not self._necesita_enlace():
            return
        civ = self.enlace.civ

        def leer():
            datos = {"niveles": {}, "interruptores": {}}
            for _, sub in NIVELES:
                datos["niveles"][sub] = civ.leer_nivel(sub)
            for _, cmd, _ in INTERRUPTORES:
                datos["interruptores"][cmd] = civ.leer_byte(cmd)
            datos["agc"] = civ.leer_byte(b"\x16\x12")
            self.cola.put(("ajustes", datos))
            return "leídos"
        self.enlace.trabajo("Ajustes del radio", leer)

    # --- frecuencia ---
    def _pintar_freq(self):
        texto = f"{self.freq:08d}"
        for lb, c in zip(self.digitos, texto):
            lb.configure(text=c)

    def poner_freq(self, hz, enviar=True):
        hz = max(FREQ_MIN, min(FREQ_MAX, int(round(hz))))
        self.freq = hz
        self._pintar_freq()
        if enviar and self.enlace and not (self.en_tx and self.var_split.get()):
            self.enlace.pedir_freq(hz)

    def _mover(self, delta):
        self.poner_freq(self.freq + delta)

    def _rueda_digito(self, evento, lb):
        self._mover(lb.potencia * (1 if evento.delta > 0 else -1))

    def _arrastre_inicio(self, evento):
        self._arr_x = evento.x_root
        self._arr_y = evento.y_root

    def _arrastre(self, evento):
        dx = evento.x_root - self._arr_x
        dy = self._arr_y - evento.y_root
        pasos = int((dx + dy) / 4)
        if pasos:
            self._arr_x, self._arr_y = evento.x_root, evento.y_root
            self._mover(pasos * self._paso())

    def _ir_a(self, _evento=None):
        try:
            v = float(self.var_entrada.get().strip().replace(",", "."))
        except ValueError:
            self.log("Frecuencia no válida. Escríbela en MHz (7.074) o en kHz (7074)")
            return
        # 7.074 -> MHz; 7074 -> kHz; 7074000 -> Hz
        hz = v * 1e6 if v < 1000 else v * 1e3 if v < 100_000 else v
        self.poner_freq(hz)
        self.var_entrada.set("")

    def _banda(self, nombre, f_defecto):
        actual = next((n for n, f in BANDAS if abs(f - self.freq) < 600_000), None)
        if actual:
            self.ultima_por_banda[actual] = self.freq
        self.poner_freq(self.ultima_por_banda.get(nombre, f_defecto))
        self.log(f"Banda {nombre}")

    # --- dial ---
    def _dibujar_dial(self):
        cv = self.cv_dial
        cv.delete("all")
        cx = cy = 85
        cv.create_oval(8, 8, 162, 162, fill="#dfe5ec", outline="#aab6c5", width=3)
        cv.create_oval(28, 28, 142, 142, fill="#f4f6f9", outline="#c3cdd9", width=2)
        for i in range(36):
            a = math.radians(i * 10)
            r1, r2 = (66, 76) if i % 3 == 0 else (70, 76)
            cv.create_line(cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                           cx + r2 * math.cos(a), cy + r2 * math.sin(a), fill="#8a96a6")
        a = self.dial_angulo
        x, y = cx + 45 * math.cos(a), cy + 45 * math.sin(a)
        cv.create_oval(x - 9, y - 9, x + 9, y + 9, fill=C_DIGITO, outline="")
        cv.create_text(cx, cy, text="DIAL", fill="#8a96a6", font=("Segoe UI", 9, "bold"))

    def _dial_inicio(self, evento):
        self._dial_prev = math.atan2(evento.y - 85, evento.x - 85)
        self._dial_resto = 0.0

    def _dial_mover(self, evento):
        a = math.atan2(evento.y - 85, evento.x - 85)
        d = a - self._dial_prev
        if d > math.pi:
            d -= 2 * math.pi
        elif d < -math.pi:
            d += 2 * math.pi
        self._dial_prev = a
        self.dial_angulo += d
        # 50 pasos por vuelta
        self._dial_resto += d / (2 * math.pi) * 50
        pasos = int(self._dial_resto)
        if pasos:
            self._dial_resto -= pasos
            self._mover(pasos * self._paso())
        self._dibujar_dial()

    # --- modo, VFO, niveles ---
    def poner_modo(self, modo):
        if not self._necesita_enlace():
            return
        self.modo = modo
        filtro = self.var_filtro.get()
        self.enlace.trabajo(f"Modo {modo} filtro {filtro}", self.enlace.civ.poner_modo, modo, filtro)
        self._pintar_modo()

    def _pintar_modo(self):
        self.etq_modo.configure(text=f"{self.modo}  F{self.var_filtro.get()}")
        for m, b in self.bt_modos.items():
            b.configure(style="Sel.TButton" if m == self.modo else "TButton")

    def _vfo(self, valor, nombre):
        self._orden(nombre, bytes([0x07, valor]))
        self.etq_vfo.configure(text=nombre)

    def _nivel_cambiado(self, nombre, sub):
        var, lb = self.escalas[sub]
        valor = int(float(var.get()))
        lb.configure(text=str(valor))
        if self.ultimo_nivel.get(sub) == valor:
            return
        self.ultimo_nivel[sub] = valor
        if self.enlace:
            self._retrasar(("nivel", sub), 150, lambda: self.enlace and self.enlace.trabajo(
                f"{nombre} {valor}", self.enlace.civ.poner_nivel, sub, valor))

    # --- split por software y PTT ---
    def _split(self):
        if self.var_split.get():
            if not self.var_freq_tx.get():
                self.var_freq_tx.set(f"{(self.freq + 1000) / 1e6:.6f}")
            self.etq_split.configure(text=f"SPLIT  TX {self.var_freq_tx.get()} MHz")
            self.log("Split activado: al transmitir el programa salta a la frecuencia de TX y vuelve al soltar.")
        else:
            self.etq_split.configure(text="")

    def ptt(self, encender):
        # Apagar siempre se envía, aunque la ventana crea que ya está en RX.
        if not self._necesita_enlace() or (encender and self.en_tx):
            return
        self.ptt_local = time.monotonic()
        civ = self.enlace.civ
        split = self.var_split.get()
        try:
            f_tx = float(self.var_freq_tx.get().replace(",", ".")) * 1e6 if split else None
        except ValueError:
            self.log("Frecuencia de TX no válida; split desactivado.")
            self.var_split.set(False)
            f_tx = None
        if encender:
            self.en_tx = True
            self.tx_desde = time.monotonic()
            if f_tx:
                self.freq_rx_split = self.freq

                def tx_split():
                    civ.poner_freq(f_tx)
                    time.sleep(0.08)
                    return civ.ptt(True)
                self.enlace.trabajo(f"PTT ON (split, TX {f_tx / 1e6:.6f})", tx_split)
            else:
                self.enlace.trabajo("PTT ON", civ.ptt, True)
        else:
            self.en_tx = False
            rx = self.freq_rx_split
            self.freq_rx_split = None

            def rx_vuelta():
                r = civ.ptt(False)
                if rx:
                    time.sleep(0.05)
                    civ.poner_freq(rx)
                return r
            self.enlace.trabajo("PTT OFF", rx_vuelta)
        self._pintar_tx(self.en_tx)

    def _ptt_soltar(self, _evento):
        if self.var_ptt_fijo.get():
            self.ptt(not self.en_tx)
        else:
            self.ptt(False)

    def _pintar_tx(self, tx):
        self.lb_tx.configure(text="  TX  " if tx else "  RX  ", bg=C_TX if tx else C_RX)
        self.bt_ptt.configure(bg=C_TX if tx else C_PTT, fg="white" if tx else C_TX, relief="sunken" if tx else "raised")

    def _vigilar_tx(self):
        if self.en_tx and time.monotonic() - self.tx_desde > max(5, self.var_tx_max.get()):
            self.log("Tiempo máximo de TX alcanzado: PTT apagado por seguridad.")
            self.ptt(False)
        self.raiz.after(500, self._vigilar_tx)

    # --- memorias ---
    def _cargar_memorias(self):
        try:
            with open(ARCHIVO_MEMORIAS, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return []

    def _guardar_memorias(self):
        try:
            with open(ARCHIVO_MEMORIAS, "w", encoding="utf-8") as fh:
                json.dump(self.memorias, fh, indent=2, ensure_ascii=False)
        except OSError as e:
            self.log(f"No se pudieron guardar las memorias: {e}")

    def _mem_refrescar(self):
        self.lst_mem.delete(0, "end")
        for m in self.memorias:
            self.lst_mem.insert("end", f"{m['freq'] / 1e6:10.6f}  {m['modo']:4s}  {m.get('nombre', '')}")

    def _mem_guardar(self):
        self.memorias.append({"freq": self.freq, "modo": self.modo, "filtro": self.var_filtro.get(),
                              "nombre": self.var_mem_nombre.get().strip()})
        self.memorias.sort(key=lambda m: m["freq"])
        self.var_mem_nombre.set("")
        self._guardar_memorias()
        self._mem_refrescar()

    def _mem_seleccion(self):
        sel = self.lst_mem.curselection()
        return sel[0] if sel else None

    def _mem_recuperar(self):
        i = self._mem_seleccion()
        if i is None:
            return
        m = self.memorias[i]
        self.poner_freq(m["freq"])
        self.var_filtro.set(m.get("filtro", 1))
        if m.get("modo") in cat.MODOS_POR_NOMBRE:
            self.poner_modo(m["modo"])

    def _mem_borrar(self):
        i = self._mem_seleccion()
        if i is not None:
            del self.memorias[i]
            self._guardar_memorias()
            self._mem_refrescar()

    # --- datos que llegan del hilo ---
    def _atender_cola(self):
        try:
            while True:
                tipo, dato = self.cola.get_nowait()
                if tipo == "log":
                    self.log(dato)
                elif tipo == "estado":
                    self._aplicar_estado(dato)
                elif tipo == "ajustes":
                    self._aplicar_ajustes(dato)
                elif tipo == "conexion":
                    self._conexion_lista(*dato)
        except queue.Empty:
            pass
        self.raiz.after(50, self._atender_cola)

    def _aplicar_estado(self, e):
        # No pisar la frecuencia mientras el usuario sintoniza ni durante TX en split.
        if e["freq"] and not e["reciente"] and not (self.en_tx and self.var_split.get()):
            if e["freq"] != self.freq:
                self.freq = e["freq"]
                self._pintar_freq()
        modo, filtro = e["modo"]
        if modo and modo != self.modo:
            self.modo = modo
            if filtro in (1, 2, 3):
                self.var_filtro.set(filtro)
            self._pintar_modo()
        if (e["tx"] is not None and e["tx"] != self.en_tx
                and time.monotonic() - self.ptt_local > 1.5):
            self.en_tx = e["tx"]
            if self.en_tx:
                self.tx_desde = time.monotonic()
            self._pintar_tx(self.en_tx)
        self._pintar_s(e["s"])

    def _pintar_s(self, valor):
        cv = self.cv_s
        cv.delete("all")
        if valor is None:
            return
        valor = max(0, min(241, valor))
        ancho = int(260 * valor / 241)
        cv.create_rectangle(0, 0, ancho, 18, fill="#7cc48f" if valor <= 120 else "#f08a8a", outline="")
        if valor <= 120:
            texto = f"S{round(valor / 120 * 9)}"
        else:
            texto = f"S9+{round((valor - 120) / 121 * 60)}"
        cv.create_text(130, 9, text=texto, fill=C_TEXTO, font=("Segoe UI", 9, "bold"))

    def _aplicar_ajustes(self, d):
        for sub, valor in d["niveles"].items():
            if valor is not None and sub in self.escalas:
                var, lb = self.escalas[sub]
                var.set(min(255, valor))
                lb.configure(text=str(min(255, valor)))
                self.ultimo_nivel[sub] = min(255, valor)
        for cmd, valor in d["interruptores"].items():
            if valor is not None and cmd in self.vars_interruptor:
                self.vars_interruptor[cmd][0].set(valor != 0)
        nombre_agc = next((n for n, v in AGC.items() if v == d.get("agc")), None)
        if nombre_agc:
            self.var_agc.set(nombre_agc)
        # Los deslizadores disparan su orden al moverse: se cancelan las que provoca esta lectura.
        for clave in [k for k in self.retardos if k[0] == "nivel"]:
            self.raiz.after_cancel(self.retardos.pop(clave))

    def cerrar(self):
        if self.enlace:
            self.enlace.detener()  # el hilo apaga el PTT antes de cerrar el puerto
            self.enlace.join(timeout=2)
        self.raiz.destroy()


def main():
    p = argparse.ArgumentParser(description="Panel de control del Xiegu G1M")
    p.add_argument("--puerto", help="opcional: COM6, /dev/ttyUSB0 o SIM para conectar al abrir")
    args = p.parse_args()
    raiz = tk.Tk()
    Panel(raiz, args)
    raiz.mainloop()


if __name__ == "__main__":
    main()

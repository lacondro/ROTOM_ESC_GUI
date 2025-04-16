import tkinter
import tkinter.messagebox
import customtkinter
import glob
import threading
import time
import queue
import serial
import sys
from collections import deque
import copy
import traceback  # 오류 추적용

# --- 사용자 정의 모듈 및 pyvesc 컴포넌트 Import ---
try:
    import read  # VESC 통신 함수 모음
    from pyvesc.VESC.messages import SetCurrent, SetDutyCycle, SetRPM  # 기본 제어
    from pyvesc.VESC.messages.setters import SetMcConf, SetAppConf
    from pyvesc.VESC.messages.vesc_protocol_utils import (
        encode_set_mcconf,
        encode_set_appconf,
    )
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)
except AttributeError as e:
    print(f"Attribute Error: {e}")
    sys.exit(1)

# Matplotlib imports
import matplotlib

matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

customtkinter.set_appearance_mode("Dark")
customtkinter.set_default_color_theme("blue")


# --- DataReader Thread ---
class DataReader(threading.Thread):
    def __init__(self, data_q, error_q, app_ref, pause_event):  # pause_event 추가
        threading.Thread.__init__(self, daemon=True)
        self.data_queue = data_q
        self.error_queue = error_q
        self.app = app_ref
        # --- 추가: Event 저장 ---
        self.pause_event = pause_event
        # --- 추가 끝 ---
        self.serial_connection = None
        self.running = True
        self.lock = threading.Lock()

    def run(self):
        while self.running:
            if not self.running:
                break

            # --- 수정: Event 기반 일시정지 ---
            if self.app.pause_datareader:
                self.pause_event.set()  # "나 멈췄음" 신호 보내기
                # print("DataReader: Paused event set.")
                while (
                    self.app.pause_datareader and self.running
                ):  # pause_datareader가 False가 되거나 running이 False가 될 때까지 대기
                    time.sleep(0.05)  # CPU 사용 방지하며 대기
                self.pause_event.clear()  # "다시 시작함" 신호 해제
                # print("DataReader: Resuming, pause event cleared.")
                continue  # 루프 시작으로 돌아가서 상태 다시 확인
            connection = None
            with self.lock:
                connection = self.serial_connection
            if not self.running:
                break
            if connection and connection.is_open:
                try:
                    values = read.get_realtime_data(connection)
                    if values:
                        values.timestamp = time.time()
                        self.data_queue.put(values)
                except serial.SerialException as se:
                    msg = f"Serial Error(R):{se}"
                    # print(msg) # Avoid flooding
                    with self.lock:
                        if self.serial_connection:
                            self.error_queue.put(msg)
                            self.serial_connection = None
                except Exception as e:
                    msg = f"DataReader Error:{e}"
                    print(msg)
                    traceback.print_exc()
                    with self.lock:
                        if self.serial_connection:
                            self.error_queue.put(msg)
                            self.serial_connection = None
            if not self.running:
                break
            # --- Use try-except for hasattr/read.TIMEOUT in case read module changes ---
            try:
                sleep_time = read.TIMEOUT if hasattr(read, "TIMEOUT") else 0.05
            except AttributeError:
                sleep_time = 0.05
            time.sleep(sleep_time)
            # --- End try-except ---
        print("DataReader thread terminated.")

    def stop(self):
        print("Signaling DataReader stop...")
        self.running = False

    def set_serial_connection(self, ser):
        with self.lock:
            self.serial_connection = ser


# --- Main Application Class ---
class App(customtkinter.CTk):
    def __init__(self):
        super().__init__()
        self.update_idletasks()
        self.title("VESC Config & Monitor")
        # --- Fullscreen/Maximize logic with correct indentation ---
        try:
            self.state("zoomed")
            print("Info: Window maximized using state('zoomed').")
        except tkinter.TclError:
            try:
                self.attributes("-zoomed", True)  # macOS
                print("Info: Window maximized using attributes('-zoomed', True).")
            except tkinter.TclError:
                try:
                    self.attributes("-fullscreen", True)
                    print("Info: Using fullscreen mode. Press ESC to exit.")
                except tkinter.TclError:
                    self.geometry(f"{1200}x750")
                    print(
                        "Warn: Could not set window to maximized/fullscreen automatically."
                    )
        # --- End Fullscreen/Maximize logic ---
        self.datareader_pause_event = threading.Event()

        # State Variables
        self.serial_connection = None
        self.loaded_mc_config = None
        self.loaded_app_config = None
        self.config_read_in_progress = False
        self.config_write_in_progress = False
        self.pause_datareader = False
        self.is_plotting = False
        self.plot_start_time = None

        # Data Handling
        self.data_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.data_reader = DataReader(
            self.data_queue, self.error_queue, self, self.datareader_pause_event
        )

        # Plotting Data
        self.plot_update_interval = 100
        self.plot_time_window = 15
        # --- Use try-except for hasattr/read.TIMEOUT ---
        try:
            rt = read.TIMEOUT if hasattr(read, "TIMEOUT") else 0.05
        except AttributeError:
            rt = 0.05
        # --- End try-except ---
        self.plot_max_points = int(self.plot_time_window / rt) + 5
        self.time_data = deque(maxlen=self.plot_max_points)
        self.duty_data = deque(maxlen=self.plot_max_points)
        self.current_data = deque(maxlen=self.plot_max_points)
        self.plot_line_duty = None
        self.plot_line_current = None

        # GUI Setup
        self._setup_layout()
        self._create_sidebar()
        self._create_main_tabs_and_plot()
        self._create_realtime_panel()
        self._create_control_panel()
        self._set_initial_states()

        # Start Background Tasks
        self.data_reader.start()
        self.process_queue()
        self.after(self.plot_update_interval, self._trigger_plot_update)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self._refresh_com_ports_action()

    def _setup_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=10)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=6)
        self.grid_rowconfigure(1, weight=3)

    def _create_sidebar(self):
        self.sidebar_frame = customtkinter.CTkFrame(self, width=140, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, rowspan=2, sticky="nsew")

        # --- 수정: Sidebar 프레임의 0번 컬럼이 모든 가로 공간을 차지하도록 설정 ---
        self.sidebar_frame.grid_columnconfigure(0, weight=1)
        # --- 수정 끝 ---

        # 행 간 간격을 위한 설정 (기존 spacer 행 대신 사용 가능)
        # self.sidebar_frame.grid_rowconfigure((0,1,2,3,4,5,6,7,8), pad=5) # 예시: 각 행 위아래 5픽셀 패딩
        # self.sidebar_frame.grid_rowconfigure(9, weight=1) # 마지막 행은 여전히 확장용

        self.logo_label = customtkinter.CTkLabel(
            self.sidebar_frame,
            text="VESC\nControl",
            font=customtkinter.CTkFont(size=20, weight="bold"),
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))  # sticky 제거

        # Connection Section
        self.selected_com_port = customtkinter.StringVar(value="")
        self.com_port_optionmenu = customtkinter.CTkOptionMenu(
            self.sidebar_frame,
            values=["Select Port"],
            variable=self.selected_com_port,
            command=self._on_com_port_selected,
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.com_port_optionmenu.grid(
            row=1, column=0, padx=20, pady=(10, 5)
        )  # sticky 제거

        self.sidebar_button_refresh = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self._refresh_com_ports_action,
            text="Refresh Ports",
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.sidebar_button_refresh.grid(
            row=2, column=0, padx=20, pady=5
        )  # sticky 제거

        self.sidebar_button_connect = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self._sidebar_button_connect_event,
            text="Connect",
            state="disabled",
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.sidebar_button_connect.grid(
            row=3, column=0, padx=20, pady=5
        )  # sticky 제거

        self.sidebar_button_disconnect = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self.sidebar_button_disconnect,
            text="Disconnect",
            state="disabled",
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.sidebar_button_disconnect.grid(
            row=4, column=0, padx=20, pady=5
        )  # sticky 제거

        self.sidebar_is_connected = customtkinter.CTkLabel(
            self.sidebar_frame,
            text="Disconnected",
            text_color=("gray60", "gray50"),
            font=customtkinter.CTkFont(weight="bold"),
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.sidebar_is_connected.grid(row=5, column=0, padx=20, pady=10)  # sticky 제거

        # Config Read/Write Section
        self.sidebar_button_read_all = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self.read_all_configurations_event,
            text="Read All Config",
            state="disabled",
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.sidebar_button_read_all.grid(
            row=6, column=0, padx=20, pady=10
        )  # sticky 제거

        self.sidebar_button_write_all = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self.write_all_configurations_event,
            text="Write All Config",
            state="disabled",
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.sidebar_button_write_all.grid(
            row=7, column=0, padx=20, pady=10
        )  # sticky 제거

        # --- 빈 행 추가 (선택 사항: 간격 조절) ---
        # self.sidebar_frame.grid_rowconfigure(8, minsize=20) # 8번 행에 최소 높이 지정
        self.sidebar_frame.grid_rowconfigure(9, weight=1)  # 확장용 빈 행 (기존 유지)
        # --- 빈 행 추가 끝 ---

        # Appearance Section (Bottom)
        self.appearance_mode_label = customtkinter.CTkLabel(
            self.sidebar_frame,
            text="Appearance Mode:",
            anchor="w",  # 레이블 자체는 왼쪽 정렬 유지
        )
        # --- 수정: sticky='ew' 또는 제거 (레이블은 텍스트 때문에 가운데 정렬 효과 적음) ---
        self.appearance_mode_label.grid(
            row=10, column=0, padx=20, pady=(10, 0), sticky="w"
        )  # 왼쪽 정렬 유지 또는 sticky 제거

        self.appearance_mode_optionemenu = customtkinter.CTkOptionMenu(
            self.sidebar_frame,
            values=["Dark", "Light", "System"],
            command=self.change_appearance_mode_event,
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.appearance_mode_optionemenu.grid(
            row=11, column=0, padx=20, pady=(0, 10)
        )  # sticky 제거

        self.scaling_label = customtkinter.CTkLabel(
            self.sidebar_frame,
            text="UI Scaling:",
            anchor="w",  # 레이블 자체는 왼쪽 정렬 유지
        )
        # --- 수정: sticky='ew' 또는 제거 ---
        self.scaling_label.grid(
            row=12, column=0, padx=20, pady=(0, 0), sticky="w"
        )  # 왼쪽 정렬 유지 또는 sticky 제거

        self.scaling_optionemenu = customtkinter.CTkOptionMenu(
            self.sidebar_frame,
            values=["80%", "90%", "100%", "110%", "120%"],
            command=self.change_scaling_event,
        )
        # --- 수정: sticky 제거 (가운데 정렬) ---
        self.scaling_optionemenu.grid(
            row=13, column=0, padx=20, pady=(0, 20)
        )  # sticky 제거

    def _create_main_tabs_and_plot(self):
        mf = customtkinter.CTkFrame(self, fg_color="transparent")
        mf.grid(row=0, column=1, padx=(20, 0), pady=(20, 10), sticky="nsew")
        mf.grid_rowconfigure(0, weight=1)
        mf.grid_columnconfigure(0, weight=1)
        self.tabview = customtkinter.CTkTabview(mf)
        self.tabview.grid(row=0, column=0, sticky="nsew")
        for name in ["Settings", "Plot", "Console"]:
            self.tabview.add(name)
            tab = self.tabview.tab(name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
        st = self.tabview.tab("Settings")
        customtkinter.CTkLabel(
            st, text="Config UI (Placeholder)", font=("Arial", 16)
        ).grid(row=0, column=0, padx=20, pady=20)
        self.optionmenu_1 = customtkinter.CTkOptionMenu(
            st, values=["(Read First)"], state="disabled"
        )
        self.optionmenu_1.grid(row=1, column=0, padx=20, pady=10, sticky="w")
        pt = self.tabview.tab("Plot")
        pt.grid_rowconfigure(0, weight=1)
        pt.grid_rowconfigure(1, weight=0)
        pt.grid_rowconfigure(2, weight=0)
        self._update_plot_theme_params()
        self.plot_figure = Figure(figsize=(5, 3), dpi=100)
        self.plot_figure.set_facecolor(plt.rcParams["figure.facecolor"])
        self.ax_duty = self.plot_figure.add_subplot(111)
        self.ax_current = self.ax_duty.twinx()
        self._setup_plot_axes()
        self.plot_canvas = FigureCanvasTkAgg(self.plot_figure, master=pt)
        self.plot_canvas_widget = self.plot_canvas.get_tk_widget()
        self.plot_canvas_widget.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        tf = customtkinter.CTkFrame(pt, fg_color="transparent")
        tf.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        try:
            self.plot_toolbar = NavigationToolbar2Tk(self.plot_canvas, tf)
            self.plot_toolbar.update()
        except Exception as e:
            print(f"Toolbar Error:{e}")
            self.plot_toolbar = None
        bf = customtkinter.CTkFrame(pt, fg_color="transparent")
        bf.grid(row=2, column=0, pady=(5, 10))
        self.plot_start_button = customtkinter.CTkButton(
            bf,
            text="Start Plot",
            command=self._start_plotting_event,
            fg_color="green",
            hover_color="dark green",
            state="disabled",
        )
        self.plot_start_button.pack(side=tkinter.LEFT, padx=10)
        self.plot_stop_button = customtkinter.CTkButton(
            bf,
            text="Stop Plot",
            command=self._stop_plotting_event,
            fg_color="red",
            hover_color="dark red",
            state="disabled",
        )
        self.plot_stop_button.pack(side=tkinter.LEFT, padx=10)
        ct = self.tabview.tab("Console")
        ct.grid_columnconfigure(0, weight=1)
        ct.grid_rowconfigure(0, weight=1)
        self.textbox = customtkinter.CTkTextbox(
            ct, corner_radius=5, wrap="word", state="disabled"
        )
        self.textbox.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self._insert_log("Console Output:\n")

    def _create_realtime_panel(self):
        rt_frame = customtkinter.CTkFrame(self)
        rt_frame.grid(
            row=0, column=2, rowspan=2, padx=(10, 20), pady=(20, 10), sticky="nsew"
        )
        rt_frame.grid_columnconfigure(0, weight=1)  # 항목 레이블 열
        rt_frame.grid_columnconfigure(1, weight=0)  # 값 레이블 열

        rt_title = customtkinter.CTkLabel(
            rt_frame,
            text="Realtime Data",
            font=customtkinter.CTkFont(size=25, weight="bold"),
        )
        # Title row - no weight needed unless you want it to take up specific space
        rt_frame.grid_rowconfigure(0, weight=0)
        rt_title.grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 15))

        def create_rt_label_pair(parent, text, row):
            # --- 각 데이터 행에 weight=1 부여 ---
            parent.grid_rowconfigure(row, weight=1)

            # 항목 레이블 (왼쪽 정렬)
            lbl_text = customtkinter.CTkLabel(
                parent,
                text=text,
                anchor="center",
                font=customtkinter.CTkFont(size=25, weight="bold"),
            )
            # sticky="nsew" 대신 "w" 사용하고 pady 증가시켜 세로 중앙 정렬 효과
            lbl_text.grid(
                row=row, column=0, padx=(15, 5), pady=5, sticky="w"
            )  # pady 증가

            # 값 레이블 (가운데 정렬)
            lbl_value = customtkinter.CTkLabel(
                parent,
                text="N/A",
                font=customtkinter.CTkFont(size=25, weight="bold"),
                anchor="center",
            )
            # sticky 제거하고 pady 증가
            lbl_value.grid(row=row, column=1, padx=(5, 15), pady=5)  # pady 증가
            return lbl_value

        # --- 각 항목 생성 및 배치 ---
        current_row = 1  # Start data rows from 1
        self.real_voltage_read = create_rt_label_pair(rt_frame, "V In", current_row)
        current_row += 1
        self.real_duty_read = create_rt_label_pair(rt_frame, "Duty", current_row)
        current_row += 1
        self.real_mot_curr_read = create_rt_label_pair(
            rt_frame, "Mot Curr", current_row
        )
        current_row += 1
        self.real_batt_curr_read = create_rt_label_pair(
            rt_frame, "Batt Curr", current_row
        )
        current_row += 1
        self.real_erpm_read = create_rt_label_pair(rt_frame, "ERPM", current_row)
        current_row += 1
        self.real_temp_mos_read = create_rt_label_pair(
            rt_frame, "MOS Temp", current_row
        )
        current_row += 1
        self.real_power_read = create_rt_label_pair(rt_frame, "Power", current_row)
        current_row += 1
        self.real_fault_read = create_rt_label_pair(rt_frame, "Fault", current_row)
        current_row += 1

        # --- 마지막 Spacer 행 제거 또는 weight=1로 설정 ---
        # rt_frame.grid_rowconfigure(current_row, weight=1) # 이 줄은 이제 필요 없음 (모든 데이터 행이 weight=1을 가짐)
        # 만약 데이터 항목 아래에 추가 공간이 필요하면 유지하되, 필요 없으면 제거합니다.
        # 여기서는 제거하겠습니다.

    def _create_control_panel(self):
        cpf = customtkinter.CTkFrame(self)
        cpf.grid(row=1, column=1, padx=(20, 0), pady=(10, 20), sticky="nsew")
        cpf.grid_columnconfigure((0, 1, 2, 3), weight=1)
        cpf.grid_rowconfigure(0, weight=0)
        cpf.grid_rowconfigure(1, weight=1)
        cpf.grid_rowconfigure(2, weight=0)  # Correct weight usage
        customtkinter.CTkLabel(
            cpf,
            text="Motor Control",
            font=customtkinter.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, columnspan=4, pady=(5, 10))
        self.control_mode = tkinter.StringVar(value="None")
        modes = [
            ("Duty", "Duty"),
            ("Current", "Current"),
            ("RPM", "RPM"),
            ("None", "None"),
        ]
        rf = customtkinter.CTkFrame(cpf, fg_color="transparent")
        rf.grid(row=1, column=0, padx=5, pady=5, sticky="nw")
        [
            customtkinter.CTkRadioButton(
                rf,
                text=t,
                variable=self.control_mode,
                value=m,
                command=self._on_control_mode_change,
            ).pack(anchor="w", pady=3)
            for t, m in modes
        ]
        self.duty_var = tkinter.DoubleVar(value=0.0)
        self.slider_duty = customtkinter.CTkSlider(
            cpf,
            from_=0,
            to=100,
            variable=self.duty_var,
            command=self._slider_duty_event,
            orientation="vertical",
            state="disabled",
        )
        self.slider_duty.grid(row=1, column=1, padx=10, pady=5, sticky="ns")
        self.text_duty = customtkinter.CTkLabel(
            cpf, textvariable=self.duty_var, width=40
        )
        self.text_duty.grid(row=2, column=1, pady=(0, 5))
        self.current_var = tkinter.DoubleVar(value=0.0)
        max_c = 20
        self.slider_current = customtkinter.CTkSlider(
            cpf,
            from_=-max_c,
            to=max_c,
            variable=self.current_var,
            command=self._slider_current_event,
            orientation="vertical",
            state="disabled",
        )
        self.slider_current.grid(row=1, column=2, padx=10, pady=5, sticky="ns")
        self.text_current = customtkinter.CTkLabel(
            cpf, textvariable=self.current_var, width=40
        )
        self.text_current.grid(row=2, column=2, pady=(0, 5))
        rpmf = customtkinter.CTkFrame(cpf, fg_color="transparent")
        rpmf.grid(row=1, column=3, padx=10, pady=5, sticky="nw")
        self.rpm_var = tkinter.IntVar(value=0)
        self.entry_rpm = customtkinter.CTkEntry(
            rpmf, textvariable=self.rpm_var, width=80, state="disabled"
        )
        self.entry_rpm.pack(pady=(5, 5))
        self.button_set_rpm = customtkinter.CTkButton(
            rpmf,
            text="Set RPM",
            command=self._set_rpm_event,
            width=80,
            state="disabled",
        )
        self.button_set_rpm.pack(pady=5)
        self.stop_button = customtkinter.CTkButton(
            rpmf,
            command=self.stop_button_event,
            text="STOP",
            fg_color="#D9262C",
            hover_color="#B7181E",
            width=80,
            height=35,
            font=customtkinter.CTkFont(weight="bold"),
            state="disabled",
        )
        self.stop_button.pack(pady=(15, 5))

    def _set_initial_states(self):
        self.com_port_optionmenu.set("Select Port")
        self.appearance_mode_optionemenu.set("Dark")
        self.scaling_optionemenu.set("100%")
        self.control_mode.set("None")
        self._update_ui_connection_state(connected=False)

    def _on_com_port_selected(self, port):
        valid = port and "found" not in port.lower() and "select" not in port.lower()
        btn = getattr(self, "sidebar_button_connect", None)
        if btn:
            conn = self.serial_connection and self.serial_connection.is_open
            btn.configure(state="normal" if valid and not conn else "disabled")

    # --- get_COM_ports 수정됨: 모든 try/except 블록 수정 ---
    def get_COM_ports(self):
        ports = []
        plat = sys.platform
        if plat.startswith("win"):
            try:
                from serial.tools.list_ports import comports

                ports = [p.device for p in comports()]
            except ImportError:
                ports = glob.glob("COM*")  # Fallback
        elif plat.startswith("linux") or plat.startswith("darwin"):
            patterns = [
                "/dev/ttyACM*",
                "/dev/ttyUSB*",
                "/dev/cu.usb*",
                "/dev/cu.usbmodem*",
            ]
            for p in patterns:
                try:  # glob can sometimes fail with permission errors
                    ports.extend(glob.glob(p))
                except Exception as e:
                    print(f"Warn: Error globbing {p}: {e}")
        # Filter and sort
        try:
            ports = sorted(
                list(
                    set(
                        [
                            p
                            for p in ports
                            if all(
                                kw not in p.lower() for kw in ["bluetooth", "wireless"]
                            )
                        ]
                    )
                )
            )
        except Exception as e:
            print(
                f"Warn: Error filtering/sorting ports: {e}"
            )  # Handle potential errors if filtering fails

        return ports if ports else ["No ports found"]

    # --- get_COM_ports 수정 끝 ---

    def _refresh_com_ports_action(self):
        ports = self.get_COM_ports()
        cur = self.selected_com_port.get()
        menu = getattr(self, "com_port_optionmenu", None)
        if menu:
            menu.configure(values=ports)
            p = ports[0] if ports and ports[0] != "No ports found" else ""
            sel = cur if cur in ports else p
            menu.set(sel if sel else "No ports found")
            self.selected_com_port.set(menu.get())
            self._on_com_port_selected(sel)
            self._insert_log("COM Ports Refreshed.")

    def _sidebar_button_connect_event(self):
        port = self.selected_com_port.get()
        if not port or "select" in port.lower() or "found" in port.lower():
            return tkinter.messagebox.showwarning("Connect Error", "Select valid port.")
        self._insert_log(f"Connecting to {port}...")
        lbl = getattr(self, "sidebar_is_connected", None)
        lbl.configure(text="Connecting...", text_color="orange") if lbl else None
        self._update_ui_connection_state(connecting=True)
        self.update_idletasks()
        threading.Thread(
            target=self._attempt_connection, args=(port,), daemon=True
        ).start()

    def _attempt_connection(self, port):
        ser = None
        try:
            ser = serial.Serial(port, baudrate=115200, timeout=0.5)
            if not ser.is_open:
                raise serial.SerialException(f"Port {port} not open.")
            self.after(0, self._connection_success, ser, port)
        except (serial.SerialException, Exception) as e:
            err = f"Conn fail {port}:{e}"
            print(err)
            if ser and ser.is_open:
                try:
                    ser.close()
                    print(f"Debug: Closed port {port}")
                except Exception as close_err:
                    print(f"Debug: Error closing port {port}: {close_err}")
            self.after(0, self._connection_failure, port, err)

    def _connection_success(self, ser_obj, port_name):
        self._insert_log(f"Connected to {port_name}.")
        self.serial_connection = ser_obj
        self.data_reader.set_serial_connection(ser_obj)
        self._update_ui_connection_state(connected=True)

    def _connection_failure(self, port_name, err_msg):
        self._insert_log(err_msg, error=True)
        tkinter.messagebox.showerror("Connection Error", err_msg)
        self.serial_connection = None
        self.data_reader.set_serial_connection(None)
        self._update_ui_connection_state(connected=False)

    def sidebar_button_disconnect(self):
        self._insert_log("Disconnecting...")
        self._handle_disconnection(log=True)

    def _handle_disconnection(self, log=True):
        msg = (
            f"Closing connection to {self.serial_connection.port}."
            if self.serial_connection and self.serial_connection.is_open
            else "No active connection."
        )
        self._insert_log(msg) if log else None
        self.is_plotting = False
        self.pause_datareader = True
        if self.serial_connection and self.serial_connection.is_open:
            self.stop_button_event(log=False)
        ser_close = self.serial_connection
        self.serial_connection = None
        self.data_reader.set_serial_connection(None)
        read.close_serial_port(ser_close) if ser_close else None
        self.pause_datareader = False
        self._update_ui_connection_state(connected=False)
        self.after(50, self.update_labels, None)
        self._reset_plot()
        self._insert_log("Disconnected.") if log else None

    def read_all_configurations_event(self):
        if not self.serial_connection or not self.serial_connection.is_open:
            return tkinter.messagebox.showerror("Error", "Connect first.")
        if self.config_read_in_progress or self.config_write_in_progress:
            return self._insert_log("Warn: Config busy.", error=True)
        self._insert_log("Reading configs...")
        self.config_read_in_progress = True
        self.pause_datareader = True
        self._update_config_button_states()
        threading.Thread(target=self._read_configs_worker, daemon=True).start()

    def _read_configs_worker(self):
        mc, app, err = None, None, None
        ser = self.serial_connection

        # --- 추가: DataReader가 멈출 때까지 대기 ---
        # pause_datareader는 이미 True로 설정됨
        # print("Worker: Waiting for DataReader pause...")
        paused = self.datareader_pause_event.wait(timeout=0.5)  # 최대 0.5초 대기
        if not paused:
            err = "Timeout waiting for DataReader to pause."
            print(f"Warning: {err}")  # 로그 남기고 계속 진행하거나 에러 처리 가능
        else:
            # print("Worker: DataReader confirmed paused.")
            pass
        # --- 추가 끝 ---

        # --- 나머지 로직 (이전과 거의 동일, 완료 시 pause 해제) ---
        if err is None:  # Pause 대기 성공 시에만 진행 (선택적)
            if not ser or not ser.is_open:
                err = "Connection lost before read."
            else:
                try:
                    mc = read.get_mc_configuration(ser)
                    if mc:
                        time.sleep(0.1)
                        app = read.get_app_configuration(ser)
                    if not mc:
                        err = "Failed MC read."
                    elif not app:
                        err = "Failed APP read (after MC OK)."
                except serial.SerialException as se:
                    err = f"Serial Error reading configs: {se}"
                    self.error_queue.put(err)
                    print(err)
                except Exception as e:
                    err = f"Unexpected error reading configs: {e}"
                    traceback.print_exc()

        # 작업 완료 또는 실패 시 메인 스레드 콜백 호출 및 Pause 해제
        self.config_read_in_progress = False  # 상태 플래그 리셋
        self.pause_datareader = (
            False  # DataReader 다시 활성화 (이후 루프에서 Event 자동 해제됨)
        )
        self.after(0, self._read_configs_finished, mc, app, err)

    def _read_configs_finished(self, mc, app, err):
        """Callback after config read attempt."""
        # self.config_read_in_progress = False # Worker thread now handles this
        # self.pause_datareader = False      # Worker thread now handles this
        self._update_config_button_states()  # Update button states based on config_read_in_progress
        menu = getattr(self, "optionmenu_1", None)
        if err:
            self._insert_log(f"Read Config Error: {err}", error=True)
            tkinter.messagebox.showerror("Read Error", err)
            self.loaded_mc_config = self.loaded_app_config = None
            if menu:
                menu.set("(Read Fail)")
        elif mc and app:
            self.loaded_mc_config = mc
            self.loaded_app_config = app
            self._insert_log("Configs read successfully.")
            self._update_gui_with_config()
            tkinter.messagebox.showinfo("Read Success", "Configs read!")
        else:  # Should not happen if worker logic is correct, but handle defensively
            unknown_err = (
                "Read Error: Unknown failure (mc or app is None without error)."
            )
            self._insert_log(unknown_err, error=True)
            tkinter.messagebox.showerror("Read Error", unknown_err)
            self.loaded_mc_config = self.loaded_app_config = None
            if menu:
                menu.set("(Read Fail)")
        self.update_idletasks()  # Ensure UI reflects changes

    def write_all_configurations_event(self):
        if not self.serial_connection or not self.serial_connection.is_open:
            return tkinter.messagebox.showerror("Error", "Connect first.")
        if self.config_write_in_progress or self.config_read_in_progress:
            return self._insert_log("Warn: Config busy.", error=True)
        if not self.loaded_mc_config or not self.loaded_app_config:
            return tkinter.messagebox.showerror("Error", "Read configs first.")
        if not all([SetMcConf, SetAppConf, encode_set_mcconf, encode_set_appconf]):
            return tkinter.messagebox.showerror("Error", "pyvesc write missing.")
        if not tkinter.messagebox.askyesno(
            "Confirm Write", "Overwrite VESC configs?", icon="warning"
        ):
            return self._insert_log("Write cancelled.")
        self._insert_log("Writing configs...")
        self.config_write_in_progress = True
        self.pause_datareader = True
        self._update_config_button_states()
        mc_w = self._get_mc_config_from_gui()
        app_w = self._get_app_config_from_gui()
        if not mc_w or not app_w:
            self._insert_log("Write Error: Bad GUI config.", error=True)
            tkinter.messagebox.showerror("Write Error", "Cannot get GUI settings.")
            self.config_write_in_progress = False
            self.pause_datareader = False
            self._update_config_button_states()
            return
        threading.Thread(
            target=self._write_configs_worker, args=(mc_w, app_w), daemon=True
        ).start()

    def _write_configs_worker(self, mc_conf, app_conf):
        ok, err = False, None
        ser = self.serial_connection

        # --- 추가: DataReader가 멈출 때까지 대기 ---
        # print("Worker: Waiting for DataReader pause...")
        paused = self.datareader_pause_event.wait(timeout=0.5)
        if not paused:
            err = "Timeout waiting for DataReader to pause before write."
            print(f"Warning: {err}")
        # --- 추가 끝 ---

        if err is None:  # Pause 대기 성공 시에만 진행 (선택적)
            if not ser or not ser.is_open:
                err = "Connection lost before write."
            else:
                try:
                    mc_msg = SetMcConf()
                    mc_msg.mc_configuration = mc_conf
                    (
                        mc_msg.mc_configuration.setdefault(
                            "MCCONF_SIGNATURE",
                            self.loaded_mc_config.get("MCCONF_SIGNATURE", 0),
                        )
                        if self.loaded_mc_config
                        else None
                    )
                    packet = encode_set_mcconf(mc_msg)
                    read.clear_input_buffer(ser, 0.1)
                    ser.write(packet)
                    time.sleep(1.5)
                    app_msg = SetAppConf()
                    app_msg.app_configuration = app_conf
                    (
                        app_msg.app_configuration.setdefault(
                            "APPCONF_SIGNATURE",
                            self.loaded_app_config.get("APPCONF_SIGNATURE", 0),
                        )
                        if self.loaded_app_config
                        else None
                    )
                    packet = encode_set_appconf(app_msg)
                    read.clear_input_buffer(ser, 0.1)
                    ser.write(packet)
                    time.sleep(1.5)
                    ok = True
                except serial.SerialException as se:
                    err = f"Serial Error writing:{se}"
                    self.error_queue.put(err)
                    print(err)
                except Exception as e:
                    err = f"Error writing:{e}"
                    traceback.print_exc()

        # 작업 완료 또는 실패 시 메인 스레드 콜백 호출 및 Pause 해제
        self.config_write_in_progress = False
        self.pause_datareader = False
        self.after(0, self._write_configs_finished, ok, err)

    # --- Config Read/Write Workers 수정 끝 ---

    def _write_configs_finished(self, success, error_msg):
        self.config_write_in_progress = False
        self.pause_datareader = False
        self._update_config_button_states()
        if success:
            self._insert_log("Configs written.")
            tkinter.messagebox.showinfo("Write Success", "Configs written!")
        else:
            err = f"Write Error: {error_msg}" if error_msg else "Write Failed."
            self._insert_log(err, error=True)
            tkinter.messagebox.showerror("Write Error", err)
        self.update_idletasks()

    def _update_gui_with_config(self):
        if not self.loaded_mc_config or not self.loaded_app_config:
            self._insert_log("Cannot update GUI: Configs not loaded.", error=True)
            # Ensure option menu is updated even on failure to load
            menu = getattr(self, "optionmenu_1", None)
            if menu:
                menu.set("(Read First)")
                menu.configure(state="disabled")
            return

        self._insert_log("Updating GUI with loaded config values...")
        map_mt = {0: "BLDC", 2: "FOC", 3: "DC"}

        # --- 수정된 try-except 블록 ---
        try:
            # Get the widget reference safely
            menu = getattr(self, "optionmenu_1", None)
            if menu:  # Check if the widget exists
                if "motor_type" in self.loaded_mc_config:
                    val = self.loaded_mc_config["motor_type"]
                    s = map_mt.get(val, f"Unk({val})")
                    # Ensure the list of options includes the value to be set
                    current_options = menu.cget("values")
                    if s not in current_options:
                        # If the value isn't an option, add it or handle appropriately
                        # For now, let's just ensure the known types are options
                        menu.configure(
                            values=["BLDC", "FOC", "DC", "(Read First)", f"Unk({val})"]
                        )  # Add unknown if needed
                    menu.set(s)
                    menu.configure(state="normal")  # Enable after setting
                else:
                    # 'motor_type' key missing in config
                    menu.set("(N/A)")
                    menu.configure(state="disabled")
        except Exception as e:
            self._insert_log(f"GUI Update Error (Motor Type): {e}", error=True)
            # Attempt to set a safe default state on error
            menu = getattr(self, "optionmenu_1", None)
            if menu:
                menu.set("(Error)")
                menu.configure(state="disabled")
        # --- 수정 끝 ---

        # --- TODO: Add updates for other GUI elements here ---
        # Example (assuming you have an entry widget named self.entry_max_current):
        # try:
        #     if hasattr(self, 'entry_max_current') and 'l_current_max' in self.loaded_mc_config:
        #         max_curr = self.loaded_mc_config['l_current_max']
        #         self.entry_max_current.delete(0, tkinter.END)
        #         self.entry_max_current.insert(0, f"{max_curr:.2f}")
        #         self.entry_max_current.configure(state="normal")
        # except Exception as e:
        #     self._insert_log(f"GUI Update Error (Max Current): {e}", error=True)
        #     if hasattr(self, 'entry_max_current'): self.entry_max_current.configure(state="disabled")
        # --- End Example ---

        self._insert_log("GUI update with config finished.")

    def _get_mc_config_from_gui(self):
        """Retrieves MC configuration values from GUI elements."""
        # Ensure original config is loaded as a base
        if not self.loaded_mc_config:
            self._insert_log(
                "Cannot get MC config from GUI: Original config not loaded.", error=True
            )
            return None

        # Start with a deep copy to avoid modifying the original loaded config directly
        conf = copy.deepcopy(self.loaded_mc_config)
        self._insert_log("Getting MC config from GUI (Placeholder)...")

        # --- 수정된 try-except 블록 ---
        try:
            # Example: Get Motor Type from Option Menu
            menu = getattr(self, "optionmenu_1", None)
            if menu:  # Check if the widget exists
                selected_type_str = menu.get()
                motor_type_map_inv = {"BLDC": 0, "FOC": 2, "DC": 3}  # Inverse mapping
                # Update the config dict only if a valid type string is found in the map
                if selected_type_str in motor_type_map_inv:
                    conf["motor_type"] = motor_type_map_inv[selected_type_str]
                else:
                    # If the selection is invalid (e.g., "(Read First)", "(Error)"),
                    # keep the original value from the deepcopy. Log a warning.
                    self._insert_log(
                        f"Warn: Invalid motor type '{selected_type_str}' in GUI. Keeping original value.",
                        error=True,
                    )

            # --- TODO: Add code here to get other values from GUI widgets ---
            # Example (assuming self.entry_max_current exists):
            # entry_widget = getattr(self, 'entry_max_current', None)
            # if entry_widget:
            #     try:
            #         conf['l_current_max'] = float(entry_widget.get())
            #     except ValueError:
            #         self._insert_log("Error: Invalid value for Max Motor Current.", error=True)
            #         # Decide how to handle invalid input: return None, raise error, keep original?
            #         # Returning None here to indicate failure to get valid config from GUI
            #         return None
            # --- End Example ---

            # Ensure signature is present (important for some VESC operations)
            # Use setdefault which adds the key only if it's missing
            conf.setdefault(
                "MCCONF_SIGNATURE", self.loaded_mc_config.get("MCCONF_SIGNATURE", 0)
            )

            self._insert_log("MC config retrieval from GUI finished.")
            return conf

        except Exception as e:
            # Catch any unexpected errors during GUI value retrieval or type conversion
            self._insert_log(f"Error getting MC config from GUI: {e}", error=True)
            traceback.print_exc()  # Log full traceback for debugging
            tkinter.messagebox.showerror(
                "GUI Error", f"Error reading settings from GUI:\n{e}"
            )
            return None  # Indicate failure

    def _get_app_config_from_gui(self):
        if not self.loaded_app_config:
            return None
        conf = copy.deepcopy(self.loaded_app_config)
        self._insert_log("Getting APP config (Placeholder)...")
        try:
            conf.setdefault(
                "APPCONF_SIGNATURE", self.loaded_app_config.get("APPCONF_SIGNATURE", 0)
            )
            return conf
        except Exception as e:
            self._insert_log(f"Error getting APP from GUI:{e}", error=True)
            return None

    def _update_config_button_states(self):
        conn = self.serial_connection and self.serial_connection.is_open
        busy = self.config_read_in_progress or self.config_write_in_progress
        read = "normal" if conn and not busy else "disabled"
        write = "normal" if conn and not busy and self.loaded_mc_config else "disabled"
        btn_r = getattr(self, "sidebar_button_read_all", None)
        btn_r.configure(state=read) if btn_r else None
        btn_w = getattr(self, "sidebar_button_write_all", None)
        btn_w.configure(state=write) if btn_w else None

    def _update_ui_connection_state(self, connected=False, connecting=False):
        txt, clr, con, dis, ref, com = (
            "",
            "",
            "disabled",
            "disabled",
            "disabled",
            "disabled",
        )
        if connecting:
            txt, clr = "Connecting...", "orange"
            self.is_plotting = False
        elif connected:
            txt, clr = "Connected", ("#4CAF50", "#66BB6A")
            dis, com, ref = "normal", "disabled", "disabled"
        else:
            txt = "Disconnected"
            dk, lt = "light coral", "#E57373"
            clr = dk if customtkinter.get_appearance_mode() == "Dark" else lt
            valid = (
                self.selected_com_port.get()
                and "found" not in self.selected_com_port.get().lower()
                and "select" not in self.selected_com_port.get().lower()
            )
            con = "normal" if valid else "disabled"
            ref, com = "normal", "normal"
            self.is_plotting = False
            self.loaded_mc_config = self.loaded_app_config = None
            menu = getattr(self, "optionmenu_1", None)
            menu.set("(Connect First)") if menu else None
        lbl = getattr(self, "sidebar_is_connected", None)
        lbl.configure(text=txt, text_color=clr) if lbl else None
        btn_con = getattr(self, "sidebar_button_connect", None)
        btn_con.configure(state=con) if btn_con else None
        btn_dis = getattr(self, "sidebar_button_disconnect", None)
        btn_dis.configure(state=dis) if btn_dis else None
        btn_ref = getattr(self, "sidebar_button_refresh", None)
        btn_ref.configure(state=ref) if btn_ref else None
        mnu_com = getattr(self, "com_port_optionmenu", None)
        mnu_com.configure(state=com) if mnu_com else None
        self._update_plot_button_states()
        self._update_control_panel_state()
        self._update_config_button_states()

    def change_appearance_mode_event(self, mode):
        customtkinter.set_appearance_mode(mode)
        self._update_plot_theme()

    def change_scaling_event(self, scale):
        try:
            customtkinter.set_widget_scaling(int(scale.replace("%", "")) / 100)
            self._insert_log(f"UI Scaling set to {scale}")
        except ValueError:
            self._insert_log(f"Error: Invalid scaling value - {scale}", error=True)
        except Exception as e:
            self._insert_log(f"Error changing UI scaling: {e}", error=True)

    def _on_control_mode_change(self):
        mode = self.control_mode.get()
        self._insert_log(f"Control mode:{mode}")
        self.stop_button_event(log=False)
        self._update_control_panel_state()

    def _update_control_panel_state(self):
        mode = self.control_mode.get()
        conn = self.serial_connection and self.serial_connection.is_open

        def set_s(a, c):
            w = getattr(self, a, None)
            w.configure(state="normal" if conn and c else "disabled") if w else None

        set_s("slider_duty", mode == "Duty")
        set_s("slider_current", mode == "Current")
        set_s("entry_rpm", mode == "RPM")
        set_s("button_set_rpm", mode == "RPM")
        set_s("stop_button", conn)
        if not conn or mode != "Duty":
            self.duty_var.set(0.0)
        if not conn or mode != "Current":
            self.current_var.set(0.0)
        if not conn or mode != "RPM":
            self.rpm_var.set(0)

    def _slider_duty_event(self, val):
        d = max(0.0, min(0.95, float(val) / 100.0))
        (
            self._send_if_connected(SetDutyCycle(d))
            if self.control_mode.get() == "Duty"
            else None
        )
        self.duty_var.set(round(float(val), 1))

    def _slider_current_event(self, val):
        c = round(float(val), 2)
        (
            self._send_if_connected(SetCurrent(c))
            if self.control_mode.get() == "Current"
            else None
        )
        self.current_var.set(round(float(val), 1))

    def _set_rpm_event(self):
        if self.control_mode.get() == "RPM":
            try:
                rpm = int(self.rpm_var.get())
                self._send_if_connected(SetRPM(rpm))
            except ValueError:
                tkinter.messagebox.showerror("Input Error", "Invalid RPM.")
            except Exception as e:
                tkinter.messagebox.showerror("RPM Error", f"{e}")

    def stop_button_event(self, log=True):
        if log:
            self._insert_log("STOP pressed.")
        sent = self._send_if_connected(SetCurrent(0))
        self._send_if_connected(SetDutyCycle(0))
        self._send_if_connected(SetRPM(0))
        if sent:
            self.duty_var.set(0.0)
            self.current_var.set(0.0)
            self.rpm_var.set(0)
        elif log:
            self._insert_log("Cannot STOP: Not connected.", error=True)

    def _send_if_connected(self, cmd):
        if self.serial_connection and self.serial_connection.is_open:
            ok = read.send_command(self.serial_connection, cmd)
            if not ok:
                msg = f"Send Fail:{type(cmd).__name__}"
                self._insert_log(msg, error=True)
                self.error_queue.put(msg)
                return False
            return True
        return False

    def print_fault_code(self, code):
        """Converts VESC fault code enum to a human-readable string."""
        # Simplified codes, expand as needed from VESC source (mc_interface.h)
        codes = {
            0: "NONE",
            1: "OV",
            2: "UV",
            3: "DRV",
            4: "ABS",
            5: "FET",
            6: "MOT",
            24: "WDG",  # Add more codes here if needed
        }
        # --- 수정된 try-except 블록 ---
        try:
            # Attempt to convert code to integer and lookup in dict
            code_num = int(code)
            return codes.get(code_num, f"UNK({code_num})")  # Return code string or UNK
        except (ValueError, TypeError):
            # Handle cases where code is not a valid integer or None
            return "INVALID"

    def update_labels(self, vals):
        """Updates realtime data labels."""
        if not hasattr(self, "real_voltage_read"):
            return  # Check if labels exist

        def upd(widget, text):  # Helper to update safely
            if widget and widget.winfo_exists():
                try:
                    widget.configure(text=text)
                except Exception:
                    pass  # Ignore if widget destroyed during update

        if vals is None:  # Handle disconnection/no data
            labels = [
                "voltage",
                "duty",
                "mot_curr",
                "batt_curr",
                "erpm",
                "temp_mos",
                "power",
                "fault",
            ]
            for name in labels:
                upd(getattr(self, f"real_{name}_read", None), "N/A")
            return

        # --- 수정된 try-except 블록 ---
        try:
            # Get values safely
            v = getattr(vals, "v_in", 0.0)
            d = getattr(vals, "duty_cycle_now", 0.0) * 100.0
            mc = getattr(vals, "avg_motor_current", 0.0)
            ic = getattr(vals, "avg_input_current", 0.0)
            erpm = getattr(vals, "rpm", 0.0)
            t = getattr(vals, "temp_fet", 0.0)
            f = getattr(vals, "mc_fault_code", 0)
            p = v * ic

            # Update each label on its own line
            upd(self.real_voltage_read, f"{v:.2f} V")
            upd(self.real_duty_read, f"{d:.1f} %")
            upd(self.real_mot_curr_read, f"{mc:.2f} A")
            upd(self.real_batt_curr_read, f"{ic:.2f} A")
            upd(self.real_erpm_read, f"{erpm:.0f} ERPM")
            upd(self.real_temp_mos_read, f"{t:.1f} °C")
            upd(self.real_power_read, f"{p:.1f} W")
            upd(self.real_fault_read, self.print_fault_code(f))

        except AttributeError as ae:
            # Handle cases where the VESC data object doesn't have an expected attribute
            print(f"Label update warning: Attribute missing - {ae}")
        except Exception as e:
            # Catch any other unexpected errors during label update
            print(f"Unexpected label update error: {e}")
            traceback.print_exc()

    def _insert_log(self, msg, error=False):
        txt = getattr(self, "textbox", None)
        if not txt or not txt.winfo_exists():
            return print(f"{'[ERR] ' if error else ''}{msg}")
        try:
            ts = time.strftime("%H:%M:%S")
            fmt = f"[{ts}] {msg}\n"
            tag = ("err",) if error else ()
            txt.configure(state="normal")
            txt.insert(tkinter.END, fmt, tag)
            (
                txt.tag_config("err", foreground="red")
                if error and "err" not in txt.tag_names()
                else None
            )
            txt.see(tkinter.END)
            txt.configure(state="disabled")
        except Exception as e:
            print(f"Log error:{e}")

    def process_queue(self):
        try:
            while not self.data_queue.empty():
                values = self.data_queue.get_nowait()
                (
                    self.update_labels(values)
                    if self.serial_connection and self.serial_connection.is_open
                    else None
                )
                self._process_plot_data(values) if self.is_plotting else None
            while not self.error_queue.empty():
                msg = self.error_queue.get_nowait()
                err = any(
                    kw in msg.lower()
                    for kw in ["error", "fail", "exception", "disconnect"]
                )
                self._insert_log(msg, error=err)
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Queue error:{e}")
            traceback.print_exc()
        self.after(50, self.process_queue)

    def _process_plot_data(self, vals):
        if not hasattr(vals, "timestamp"):
            return
        try:
            if self.plot_start_time is None:
                self.plot_start_time = vals.timestamp
            t = vals.timestamp - self.plot_start_time
            d = getattr(vals, "duty_cycle_now", 0) * 100
            c = getattr(vals, "avg_motor_current", 0)
            self.time_data.append(t)
            self.duty_data.append(d)
            self.current_data.append(c)
        except Exception as e:
            print(f"Plot data error:{e}")

    def _setup_plot_axes(self):
        """Configures plot axes appearance."""
        if not hasattr(self, "ax_duty") or not self.ax_duty:
            return
        fg = plt.rcParams["text.color"]
        grid = plt.rcParams["grid.color"]
        self.ax_duty.set_xlabel("Time (s)", color=fg)
        self.ax_duty.set_ylabel("Duty (%)", color="tab:blue")
        self.ax_duty.tick_params(axis="y", labelcolor="tab:blue")
        self.ax_duty.tick_params(axis="x", colors=fg)
        self.ax_duty.set_ylim(-5, 105)
        self.ax_duty.grid(True, color=grid, ls="--", alpha=0.6)
        self.ax_current.set_ylabel("Mot Curr (A)", color="tab:red")
        self.ax_current.tick_params(axis="y", labelcolor="tab:red")
        limit = getattr(self, "slider_current", None)
        limit = limit.cget("to") if limit else 20
        self.ax_current.set_ylim(-(limit + 5), limit + 5)
        # Set spine colors (using a loop for brevity)
        for sp, c in [
            ("left", "tab:blue"),
            ("right", "tab:red"),
            ("bottom", fg),
            ("top", fg),
        ]:
            # Check if ax_current exists for the 'right' spine adjustment
            target_ax = self.ax_current if sp == "right" else self.ax_duty
            if target_ax and hasattr(target_ax, "spines"):
                try:
                    target_ax.spines[sp].set_color(c)
                except KeyError:
                    pass  # Ignore if spine doesn't exist

        # Make some spines invisible for clarity
        self.ax_current.spines["left"].set_visible(False)
        self.ax_current.spines["top"].set_visible(False)
        self.ax_duty.spines["right"].set_visible(False)

        # --- 수정된 라인 생성/할당 로직 ---
        # Create or get the duty line
        if self.plot_line_duty is None:
            # plot returns a list containing the line, so take the first element
            self.plot_line_duty = self.ax_duty.plot(
                [], [], "b-", lw=1.5, label="Duty (%)"
            )[0]
        else:
            # If line exists, ensure it's associated with the correct axes (e.g., after theme change)
            if self.plot_line_duty not in self.ax_duty.lines:
                self.ax_duty.add_line(self.plot_line_duty)

        # Create or get the current line
        if self.plot_line_current is None:
            self.plot_line_current = self.ax_current.plot(
                [], [], "r-", lw=1.5, alpha=0.8, label="Motor Curr (A)"
            )[0]
        else:
            if self.plot_line_current not in self.ax_current.lines:
                self.ax_current.add_line(self.plot_line_current)
        # --- 수정 끝 ---

        # Add legend (optional, might overlap)
        # lines1, labels1 = self.ax_duty.get_legend_handles_labels()
        # lines2, labels2 = self.ax_current.get_legend_handles_labels()
        # self.ax_current.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize='small')

        try:
            self.plot_figure.tight_layout()
        except Exception:
            pass  # Ignore errors if axes not ready

    def _reset_plot(self):
        self.is_plotting = False
        self.plot_start_time = None
        self.time_data.clear()
        self.duty_data.clear()
        self.current_data.clear()
        if self.plot_line_duty:
            self.plot_line_duty.set_data([], [])
        if self.plot_line_current:
            self.plot_line_current.set_data([], [])
        for ax in [self.ax_duty, self.ax_current]:
            if ax:
                ax.relim()
                ax.autoscale_view()
        if self.ax_duty:
            self.ax_duty.set_ylim(-5, 105)
        if self.ax_current:
            limit = getattr(self, "slider_current", None)
            limit = limit.cget("to") if limit else 20
            self.ax_current.set_ylim(-(limit + 5), limit + 5)
        if hasattr(self, "plot_canvas"):
            self.plot_canvas.draw_idle()
        self._update_plot_button_states()

    def _trigger_plot_update(self):
        self._update_plot_visuals() if self.is_plotting else None
        self.after(self.plot_update_interval, self._trigger_plot_update)

    def _update_plot_visuals(self):
        if (
            not self.is_plotting
            or not hasattr(self, "plot_canvas")
            or not self.time_data
        ):
            return
        try:
            t, d, c = (
                list(self.time_data),
                list(self.duty_data),
                list(self.current_data),
            )
            self.plot_line_duty.set_data(t, d)
            self.plot_line_current.set_data(t, c)
            t_min = max(0, t[-1] - self.plot_time_window) if t else 0
            t_max = t[-1] if t else 1
            t_max = (
                t_min + self.plot_time_window
                if t_max - t_min < self.plot_time_window * 0.9
                else t_max
            )
            self.ax_duty.set_xlim(t_min, t_max + 1)
            self.plot_canvas.draw_idle()
        except Exception as e:
            print(f"Plot update error:{e}")

    def _update_plot_theme_params(self):
        mode = customtkinter.get_appearance_mode()
        style = "seaborn-v0_8-darkgrid" if mode == "Dark" else "seaborn-v0_8-whitegrid"
        plt.style.use(style)
        dk = {
            "figure.facecolor": "#2a2d2e",
            "axes.facecolor": "#2a2d2e",
            "axes.edgecolor": "#AAB0B5",
            "axes.labelcolor": "#FFFFFF",
            "text.color": "#FFFFFF",
            "xtick.color": "#AAB0B5",
            "ytick.color": "#AAB0B5",
            "grid.color": "#44474a",
        }
        lt = {
            "figure.facecolor": "#ebebeb",
            "axes.facecolor": "#ebebeb",
            "axes.edgecolor": "#444444",
            "axes.labelcolor": "#000000",
            "text.color": "#000000",
            "xtick.color": "#444444",
            "ytick.color": "#444444",
            "grid.color": "#cccccc",
        }
        plt.rcParams.update(dk if mode == "Dark" else lt)
        if hasattr(self, "plot_figure"):
            self.plot_figure.set_facecolor(plt.rcParams["figure.facecolor"])
        if hasattr(self, "ax_duty"):
            self.ax_duty.set_facecolor(plt.rcParams["axes.facecolor"])
            self.ax_duty.grid(
                True, color=plt.rcParams["grid.color"], ls="--", alpha=0.6
            )
            self.ax_duty.tick_params(axis="x", colors=plt.rcParams["xtick.color"])

    def _update_plot_theme(self):
        self._update_plot_theme_params()
        if hasattr(self, "plot_figure"):
            self._setup_plot_axes()
            self._update_plot_visuals()
        tb = getattr(self, "plot_toolbar", None)
        if tb:
            try:
                bg = plt.rcParams["figure.facecolor"]
                tb.configure(background=bg)
                [
                    w.configure(bg=bg)
                    for w in tb.winfo_children()
                    if hasattr(w, "configure")
                ]
                tb.update()
            except Exception as e:
                print(f"Toolbar theme update error: {e}")

    def _start_plotting_event(self):
        if not self.serial_connection or not self.serial_connection.is_open:
            return tkinter.messagebox.showinfo("Plot Info", "Connect first.")
        if self.is_plotting:
            return
        self._insert_log("Starting plot...")
        self._reset_plot()
        self.is_plotting = True
        self.plot_start_time = None
        self._update_plot_button_states()

    def _stop_plotting_event(self):
        if not self.is_plotting:
            return
        self._insert_log("Stopping plot.")
        self.is_plotting = False
        self._update_plot_button_states()

    def _update_plot_button_states(self):
        conn = self.serial_connection and self.serial_connection.is_open
        s = "normal" if conn and not self.is_plotting else "disabled"
        st = "normal" if conn and self.is_plotting else "disabled"
        btn_s = getattr(self, "plot_start_button", None)
        btn_s.configure(state=s) if btn_s else None
        btn_st = getattr(self, "plot_stop_button", None)
        btn_st.configure(state=st) if btn_st else None

    def on_closing(self):
        """Handles window closing."""
        self._insert_log("Closing application...")
        self._stop_plotting_event()

        if self.data_reader and self.data_reader.is_alive():
            self._insert_log("Stopping DataReader thread...")
            self.data_reader.stop()
            # --- 추가 대기 시간 증가 ---
            time.sleep(
                0.3
            )  # Give reader more time to see self.running flag and exit loop

        if self.serial_connection and self.serial_connection.is_open:
            self._handle_disconnection(log=True)
        else:
            if self.serial_connection:
                read.close_serial_port(self.serial_connection)

        if self.data_reader and self.data_reader.is_alive():
            self._insert_log("Waiting for DataReader thread to join...")
            self.data_reader.join(timeout=1.0)  # Increase timeout slightly
            if self.data_reader.is_alive():
                self._insert_log(
                    "Warning: DataReader thread did not stop gracefully.", error=True
                )

        if hasattr(self, "plot_figure"):
            try:
                plt.close(self.plot_figure)
                self._insert_log("Plot figure closed.")
            except Exception as e:
                self._insert_log(f"Error closing plot figure: {e}", error=True)

        self._insert_log("Destroying GUI...")
        self.destroy()
        print("Application closed.")


if __name__ == "__main__":
    try:
        app = App()
        app.mainloop()
    except Exception as e:
        print("\n--- Unhandled Exception ---")
        traceback.print_exc()
        print("------\n")
        tkinter.messagebox.showerror(
            "Fatal Error", f"Unhandled error:\n{e}\nSee console."
        )

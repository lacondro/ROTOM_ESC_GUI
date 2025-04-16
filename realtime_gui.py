# --- START OF FILE realtime_gui.py ---

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

# Import functions and classes from read.py
import read # read.py 모듈 import
from pyvesc.VESC.messages import SetCurrent, SetDutyCycle, SetRPM

# Matplotlib imports
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# !!!!! VESC 설정/메시지 클래스 (Write 기능 위해 필요) !!!!!
try:
    from pyvesc.VESC.messages.setters import SetMcConf, SetAppConf
    from vesc_protocol_utils import encode_set_mcconf, encode_set_appconf # 경로 확인!
except ImportError:
    print("Warning: SetMcConf/SetAppConf or encoding utils not found. Write function will fail.")
    SetMcConf = None # Define as None if import fails
    SetAppConf = None
    encode_set_mcconf = None
    encode_set_appconf = None


customtkinter.set_appearance_mode("Dark")
customtkinter.set_default_color_theme("blue")

# --- DataReader Thread (App 참조 추가, 일시 중지 기능 추가) ---
class DataReader(threading.Thread):
    def __init__(self, app_queue, error_queue, app_ref): # app_ref 추가
        threading.Thread.__init__(self)
        self.app_queue = app_queue
        self.error_queue = error_queue
        self.app = app_ref # App 인스턴스 참조 저장
        self.serial_connection = None
        self.running = True
        self.lock = threading.Lock()

    def run(self):
        while self.running:
            # !!!!! 일시 중지 플래그 확인 !!!!!
            if self.app.pause_datareader:
                time.sleep(0.1) # 잠시 대기
                continue # 데이터 요청 건너뛰기

            # --- 이하 기존 로직 ---
            connection = None
            with self.lock: connection = self.serial_connection
            if connection and connection.is_open:
                try:
                    values = read.get_realtime_data(connection)
                    if values:
                        values.timestamp = time.time()
                        self.app_queue.put(values)
                    # else: # 데이터를 못 읽은 경우 (타임아웃 등) - 로그 너무 많을 수 있음
                    #     # print("Reader: No realtime data")
                    #     pass
                except serial.SerialException as se:
                    # read.py 에서 close 호출 안 함, 여기서도 안 함
                    error_message = f"Serial Error in Reader: {se}"; print(error_message)
                    with self.lock:
                        if self.serial_connection: # 연결되어 있었다고 생각될 때만
                            self.error_queue.put(error_message)
                            self.serial_connection = None # 연결 참조 제거
                            # 포트 닫기는 메인 스레드가 담당
                            self.error_queue.put("Disconnected due to serial error in reader.")
                except Exception as e:
                    error_message = f"DataReader Error: {e}"; print(error_message)
                    with self.lock:
                        if self.serial_connection:
                            self.error_queue.put(error_message)
                            self.serial_connection = None
                            self.error_queue.put("Disconnected due to error in reader.")
            time.sleep(0.05) # CPU 사용량 줄이기 위해 유지
        print("DataReader thread stopping.")
        # 쓰레드 종료 시에도 포트는 메인 스레드가 관리

    def stop(self): print("Stopping DataReader thread..."); self.running = False
    def set_serial_connection(self, ser):
        with self.lock:
            # 이전 연결이 있다면 메인 스레드에 알리거나 여기서 직접 닫지 않음
            self.serial_connection = ser
    # def get_serial_connection(self): # 필요시 사용
    #     with self.lock: return self.serial_connection


# --- Main Application Class ---
class App(customtkinter.CTk):
    def __init__(self):
        super().__init__()

        # Data Handling
        self.data_queue = queue.Queue()
        self.error_queue = queue.Queue()
        # !!!!! DataReader 생성 시 self 전달 !!!!!
        self.data_reader = DataReader(self.data_queue, self.error_queue, self)
        self.serial_connection = None
        self.loaded_mc_config = None
        self.loaded_app_config = None
        self.config_read_in_progress = False
        self.config_write_in_progress = False
        self.pause_datareader = False # <<<--- DataReader 일시중지 플래그

        # ... (Plotting Attributes 동일) ...
        self.plot_update_interval = 100; self.plot_time_window = 15
        self.plot_max_points = int(self.plot_time_window / 0.05) + 2
        self.time_data = deque(maxlen=self.plot_max_points)
        self.duty_data = deque(maxlen=self.plot_max_points)
        self.current_data = deque(maxlen=self.plot_max_points)
        self.plot_start_time = None; self.is_plotting = False
        self.plot_figure = None; self.ax_duty = None; self.ax_current = None
        self.line_duty = None; self.line_current = None
        self.plot_canvas = None; self.plot_toolbar = None
        self.plot_start_button = None; self.plot_stop_button = None


        # ... (Window Configuration 동일) ...
        self.title("ROTOM CONTROL"); self.geometry(f"{1200}x{750}")
        self.grid_columnconfigure(0, weight=1); self.grid_columnconfigure(1, weight=6)
        self.grid_columnconfigure(4, weight=3)
        self.grid_rowconfigure((0, 1, 2), weight=1)

        # ... (UI Creation 메서드 호출 동일) ...
        self._create_sidebar()
        self._create_main_tabs_and_plot()
        self._create_realtime_panel()
        self._create_control_panel()

        # ... (Default Values & States 동일) ...
        self.com_port_optionmenu.set("Select Port")
        self.appearance_mode_optionemenu.set("Dark")
        self.scaling_optionemenu.set("100%")
        self.optionmenu_1.set("ESC Mode")
        self.optionmenu_2.set("Motor Direction")
        self.sensor_option.set("Sensor Type")
        self.sensor_ABI_option.set("ABI Counts")
        self._on_com_port_selected(self.selected_com_port.get())
        self._update_ui_connection_state(connected=False)


        # Start background tasks
        self.data_reader.start()
        self.process_queue()
        self.after(self.plot_update_interval, self._trigger_plot_update)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # --- UI Creation Methods ---
    # (_create_sidebar 는 Read/Write 버튼 수정 반영 - 이전 답변 코드 사용)
    def _create_sidebar(self):
        self.sidebar_frame = customtkinter.CTkFrame(self, width=140, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.logo_label = customtkinter.CTkLabel(self.sidebar_frame, text="ROTOM\nCONTROL", font=customtkinter.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))
        self.selected_com_port = customtkinter.StringVar(value="")
        self.com_port_optionmenu = customtkinter.CTkOptionMenu(self.sidebar_frame, values=self.get_COM_ports(), variable=self.selected_com_port, command=self._on_com_port_selected)
        self.com_port_optionmenu.grid(row=1, column=0, padx=20, pady=(10, 10))
        self.sidebar_button_refresh = customtkinter.CTkButton(self.sidebar_frame, command=self.sidebar_button_refresh, text="Refresh Ports")
        self.sidebar_button_refresh.grid(row=2, column=0, padx=20, pady=10)
        self.sidebar_button_connect = customtkinter.CTkButton(self.sidebar_frame, command=self._sidebar_button_connect_event, text="Connect")
        self.sidebar_button_connect.grid(row=3, column=0, padx=20, pady=10)
        self.sidebar_button_disconnect = customtkinter.CTkButton(self.sidebar_frame, command=self.sidebar_button_disconnect, text="Disconnect")
        self.sidebar_button_disconnect.grid(row=4, column=0, padx=20, pady=10)
        self.sidebar_is_connected = customtkinter.CTkLabel(self.sidebar_frame, text="Disconnected", font=customtkinter.CTkFont(weight="bold"))
        self.sidebar_is_connected.grid(row=5, column=0, padx=20, pady=10)
        # Read/Write 버튼
        self.sidebar_button_read_all = customtkinter.CTkButton(self.sidebar_frame, command=self.read_all_configurations_event, text="Read All Config")
        self.sidebar_button_read_all.grid(row=6, column=0, padx=20, pady=10)
        self.sidebar_button_write_all = customtkinter.CTkButton(self.sidebar_frame, command=self.write_all_configurations_event, text="Write All Config")
        self.sidebar_button_write_all.grid(row=7, column=0, padx=20, pady=10)
        # Appearance/Scaling
        self.appearance_mode_optionemenu = customtkinter.CTkOptionMenu(self.sidebar_frame, values=["Light", "Dark", "System"], command=self.change_appearance_mode_event)
        self.appearance_mode_optionemenu.grid(row=8, column=0, padx=20, pady=(20, 10))
        self.scaling_optionemenu = customtkinter.CTkOptionMenu(self.sidebar_frame, values=["80%", "90%", "100%", "110%", "120%"], command=self.change_scaling_event)
        self.scaling_optionemenu.grid(row=9, column=0, padx=20, pady=(10, 20))

    # ... (_create_main_tabs_and_plot, _create_realtime_panel, _create_control_panel 은 이전과 동일) ...
    def _create_main_tabs_and_plot(self): # 구현은 이전 답변 내용 복사
        self.tabview = customtkinter.CTkTabview(self); self.tabview.grid(row=0, rowspan=2, column=1, columnspan=3, padx=(20, 0), pady=(20, 20), sticky="nsew")
        tabs = ["Setting", "Detection", "Sensor", "Communication", "Console", "Plot"]; [self.tabview.add(tab) for tab in tabs]
        setting_tab = self.tabview.tab("Setting"); setting_tab.grid_columnconfigure((0, 1), weight=1)
        self.optionmenu_1 = customtkinter.CTkOptionMenu(setting_tab, dynamic_resizing=True, values=["BLDC", "FOC"]); self.optionmenu_1.grid(row=0, column=0, padx=20, pady=(20, 10))
        self.optionmenu_2 = customtkinter.CTkOptionMenu(setting_tab, dynamic_resizing=True, values=["True", "False"]); self.optionmenu_2.grid(row=1, column=0, padx=20, pady=(10, 10))
        self.string_input_button = customtkinter.CTkButton(setting_tab, text="Open Input Dialog", command=self.open_input_dialog_event); self.string_input_button.grid(row=2, column=0, padx=20, pady=(10, 10))
        sensor_tab = self.tabview.tab("Sensor"); sensor_tab.grid_columnconfigure((0, 1), weight=1)
        self.sensor_option = customtkinter.CTkOptionMenu(sensor_tab, dynamic_resizing=True, values=["None", "AS5047", "ABI", "Hall"]); self.sensor_option.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="w")
        self.sensor_ABI_option = customtkinter.CTkOptionMenu(sensor_tab, dynamic_resizing=True, values=["2048", "4000", "4096", "8192"]); self.sensor_ABI_option.grid(row=1, column=0, padx=20, pady=(10, 10), sticky="w")
        console_tab = self.tabview.tab("Console"); console_tab.grid_columnconfigure(0, weight=1); console_tab.grid_columnconfigure(1, weight=0); console_tab.grid_rowconfigure(0, weight=1); console_tab.grid_rowconfigure(1, weight=0)
        self.textbox = customtkinter.CTkTextbox(console_tab, corner_radius=5); self.textbox.grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 5), sticky="nsew"); self.textbox.insert("0.0", "Console Output:\n"); self.textbox.configure(state="disabled")
        self.entry = customtkinter.CTkEntry(console_tab, placeholder_text="Type command (e.g., help)"); self.entry.grid(row=1, column=0, padx=(10, 5), pady=(5, 10), sticky="ew"); self.entry.bind("<Return>", self.send_console_command_event)
        self.send_button = customtkinter.CTkButton(console_tab, text="Send", width=80, command=self.send_console_command_event); self.send_button.grid(row=1, column=1, padx=(0, 10), pady=(5, 10), sticky="w")
        plot_tab = self.tabview.tab("Plot"); plot_tab.grid_columnconfigure(0, weight=1); plot_tab.grid_rowconfigure(0, weight=1); plot_tab.grid_rowconfigure(1, weight=0); plot_tab.grid_rowconfigure(2, weight=0); plt.style.use("seaborn-v0_8-darkgrid" if customtkinter.get_appearance_mode() == "Dark" else "seaborn-v0_8-whitegrid"); self._update_plot_theme_params()
        self.plot_figure = Figure(figsize=(5, 4), dpi=100); self.plot_figure.set_facecolor(plt.rcParams["figure.facecolor"]); self.ax_duty = self.plot_figure.add_subplot(111); self.ax_current = self.ax_duty.twinx(); self._setup_plot_axes()
        self.plot_canvas = FigureCanvasTkAgg(self.plot_figure, master=plot_tab); self.plot_canvas_widget = self.plot_canvas.get_tk_widget(); self.plot_canvas_widget.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        toolbar_frame = customtkinter.CTkFrame(plot_tab, fg_color="transparent"); toolbar_frame.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        try: self.plot_toolbar = NavigationToolbar2Tk(self.plot_canvas, toolbar_frame); self.plot_toolbar.update()
        except Exception as e: print(f"Toolbar Error: {e}"); self.plot_toolbar = None
        button_frame = customtkinter.CTkFrame(plot_tab, fg_color="transparent"); button_frame.grid(row=2, column=0, pady=(5, 10))
        self.plot_start_button = customtkinter.CTkButton(button_frame, text="Start Plotting", command=self._start_plotting_event, fg_color="green", hover_color="dark green"); self.plot_start_button.pack(side=tkinter.LEFT, padx=10)
        self.plot_stop_button = customtkinter.CTkButton(button_frame, text="Stop Plotting", command=self._stop_plotting_event, fg_color="red", hover_color="dark red"); self.plot_stop_button.pack(side=tkinter.LEFT, padx=10)

    def _create_realtime_panel(self): # 구현은 이전 답변 내용 복사
        self.tabview2 = customtkinter.CTkTabview(self); self.tabview2.grid(row=0, rowspan=3, column=4, padx=(20, 20), pady=(20, 20), sticky="nsew"); self.tabview2.add("Realtime Data"); rt_tab = self.tabview2.tab("Realtime Data"); rt_tab.grid_columnconfigure((0, 1), weight=1); rt_tab.grid_rowconfigure(list(range(8)), weight=0)
        def create_rt_label_pair(parent, text, row, col): lbl_text = customtkinter.CTkLabel(parent, text=text, font=("Arial", 16)); lbl_text.grid(row=row, column=col, padx=10, pady=(10, 0), sticky="s"); lbl_value = customtkinter.CTkLabel(parent, text="N/A", font=("Arial", 20, "bold")); lbl_value.grid(row=row + 1, column=col, padx=10, pady=(0, 10), sticky="n"); return lbl_value
        self.real_voltage_read = create_rt_label_pair(rt_tab, "Voltage", 0, 0); self.real_duty_read = create_rt_label_pair(rt_tab, "Duty Cycle", 0, 1); self.real_mot_curr_read = create_rt_label_pair(rt_tab, "Motor Current", 2, 0); self.real_batt_curr_read = create_rt_label_pair(rt_tab, "Battery Current", 2, 1); self.real_erpm_read = create_rt_label_pair(rt_tab, "ERPM", 4, 0); self.real_temp_mos_read = create_rt_label_pair(rt_tab, "MOSFET Temp", 4, 1); self.real_power_read = create_rt_label_pair(rt_tab, "Input Power", 6, 0); self.real_fault_read = create_rt_label_pair(rt_tab, "Fault Code", 6, 1)

    def _create_control_panel(self): # 구현은 이전 답변 내용 복사
        self.tabview3 = customtkinter.CTkTabview(self); self.tabview3.grid(row=2, column=1, columnspan=3, padx=(20, 0), pady=(0, 20), sticky="nsew"); self.tabview3.add("Control Panel"); cp_tab = self.tabview3.tab("Control Panel"); cp_tab.grid_columnconfigure((0, 1, 2), weight=1); cp_tab.grid_columnconfigure(3, weight=0); cp_tab.grid_rowconfigure(0, weight=0); cp_tab.grid_rowconfigure(1, weight=1); cp_tab.grid_rowconfigure(2, weight=0)
        self.control_mode = tkinter.StringVar(value="None"); modes = [("Duty", "Duty"), ("Current", "Current"), ("RPM", "RPM")]; [customtkinter.CTkRadioButton(cp_tab, text=text, variable=self.control_mode, value=mode, command=self._on_control_mode_change).grid(row=0, column=i, pady=10, padx=10, sticky="n") for i, (text, mode) in enumerate(modes)]
        self.duty_var = tkinter.DoubleVar(); self.slider_duty = customtkinter.CTkSlider(cp_tab, from_=0, to=100, number_of_steps=100, variable=self.duty_var, command=self._slider_duty_event, orientation="horizontal"); self.slider_duty.grid(row=1, column=0, padx=20, pady=5, sticky="ew"); self.slider_duty.set(0); self.text_duty = customtkinter.CTkLabel(cp_tab, textvariable=self.duty_var, font=("", 16)); self.text_duty.grid(row=2, column=0, padx=20, pady=0, sticky="n")
        self.current_var = tkinter.DoubleVar(); max_abs_current = 20; current_steps = max_abs_current * 2 * 10; self.slider_current = customtkinter.CTkSlider(cp_tab, from_=-max_abs_current, to=max_abs_current, number_of_steps=current_steps, variable=self.current_var, command=self._slider_current_event, orientation="horizontal"); self.slider_current.grid(row=1, column=1, padx=20, pady=5, sticky="ew"); self.slider_current.set(0); self.text_current = customtkinter.CTkLabel(cp_tab, textvariable=self.current_var, font=("", 16)); self.text_current.grid(row=2, column=1, padx=20, pady=0, sticky="n")
        self.rpm_var = tkinter.IntVar(); self.entry_rpm = customtkinter.CTkEntry(cp_tab, textvariable=self.rpm_var, width=80); self.entry_rpm.grid(row=1, column=2, padx=20, pady=5, sticky="n"); self.button_set_rpm = customtkinter.CTkButton(cp_tab, text="Set RPM", command=self._set_rpm_event, width=80); self.button_set_rpm.grid(row=2, column=2, padx=20, pady=5, sticky="n")
        self.stop_button = customtkinter.CTkButton(cp_tab, command=self.stop_button_event, text="STOP", fg_color="#D9262C", hover_color="#B7181E", width=100, height=40, font=customtkinter.CTkFont(size=16, weight="bold")); self.stop_button.grid(row=0, column=3, rowspan=3, padx=20, pady=10, sticky="ns"); self._update_control_panel_state()

    # --- Event Handlers & Logic ---
    # (_on_com_port_selected ~ sidebar_button_disconnect 는 이전과 동일)
    def _on_com_port_selected(self, selected_port):
        if hasattr(self, "sidebar_button_connect"):
            valid_port = (selected_port and "found" not in selected_port and "Select" not in selected_port)
            self.sidebar_button_connect.configure(state="normal" if valid_port else "disabled")

    def get_COM_ports(self):
        ports = []; platform = sys.platform
        if "linux" in platform or "darwin" in platform: ports.extend(glob.glob("/dev/ttyACM*")); ports.extend(glob.glob("/dev/ttyUSB*")); ports.extend(glob.glob("/dev/cu.*"))
        elif "win" in platform:
            try: from serial.tools.list_ports import comports; ports = [port.device for port in comports()]
            except ImportError: print("Warning: list_ports not available."); ports.extend(glob.glob("COM*"))
        ports = [p for p in ports if all(f not in p for f in ["Bluetooth", "Wireless", "Dial-Up"])]
        return sorted(ports) if ports else ["No ports found"]

    def sidebar_button_refresh(self):
        ports = self.get_COM_ports(); current_selection = self.selected_com_port.get()
        self.com_port_optionmenu.configure(values=ports)
        if not ports or ports[0] == "No ports found": self.com_port_optionmenu.set("No ports found"); self.selected_com_port.set("")
        else:
            if current_selection in ports: self.com_port_optionmenu.set(current_selection)
            else: self.com_port_optionmenu.set(ports[0]); self.selected_com_port.set(ports[0])
        self._on_com_port_selected(self.selected_com_port.get())
        self.log_to_console("COM Ports Refreshed")

    def _sidebar_button_connect_event(self):
        port = self.selected_com_port.get()
        if not port or port in ["Select Port", "No ports found"]: tkinter.messagebox.showwarning("Connect Error", "Please select a valid COM port."); return
        self.log_to_console(f"Attempting to connect to {port}...")
        if hasattr(self, "sidebar_is_connected"): self.sidebar_is_connected.configure(text="Connecting...", text_color="orange")
        self._update_ui_connection_state(connecting=True); self.update()
        connect_thread = threading.Thread(target=self._attempt_connection, args=(port,), daemon=True); connect_thread.start()

    def _attempt_connection(self, port):
        try:
            if self.serial_connection and self.serial_connection.is_open:
                temp_ser = self.serial_connection; self.serial_connection = None; self.data_reader.set_serial_connection(None)
                read.close_serial_port(temp_ser); time.sleep(0.1)
            new_ser = serial.Serial(port, baudrate=115200, timeout=0.2)
            if new_ser.is_open: self.serial_connection = new_ser; self.data_reader.set_serial_connection(self.serial_connection); self.after(0, self._connection_success, port)
            else: self.serial_connection = None; raise serial.SerialException(f"Serial port {port} did not open.")
        except serial.SerialException as e: self.serial_connection = None; self.after(0, self._connection_failure, port, str(e))
        except Exception as e: self.serial_connection = None; self.after(0, self._connection_failure, port, f"Unexpected error: {e}")

    def _connection_success(self, port):
        self._update_ui_connection_state(connected=True)
        self.log_to_console(f"Connected to {port} successfully.")

    def _connection_failure(self, port, error_msg):
        self.log_to_console(f"Connection Failed to {port}: {error_msg}", error=True)
        tkinter.messagebox.showerror("Connection Error", f"Failed to connect to {port}:\n{error_msg}")
        self.serial_connection = None; self.data_reader.set_serial_connection(None)
        self._update_ui_connection_state(connected=False)

    def sidebar_button_disconnect(self):
        self.log_to_console("Disconnecting..."); self.is_plotting = False
        ser_to_close = self.serial_connection; self.serial_connection = None
        self.data_reader.set_serial_connection(None)
        if ser_to_close: read.close_serial_port(ser_to_close)
        self._update_ui_connection_state(connected=False)
        self.after(50, self.update_labels, None); self.log_to_console("Disconnected.")
        self._reset_plot()

    # !!!!! 통합 Read/Write 이벤트 핸들러 !!!!!
    def read_all_configurations_event(self):
        if not self.serial_connection or not self.serial_connection.is_open: tkinter.messagebox.showerror("Error", "Please connect to VESC first."); return
        if self.config_read_in_progress: self.log_to_console("Read Config Warning: Read already in progress."); return

        self.log_to_console("Reading MC & APP configurations...")
        self.config_read_in_progress = True
        self.pause_datareader = True # <<<--- 데이터 리더 일시 중지!
        self._update_config_button_states()

        read_thread = threading.Thread(target=self._read_configs_worker, daemon=True); read_thread.start()

    def _read_configs_worker(self):
        mc_conf = None; app_conf = None; error_msg = None
        ser = self.data_reader.serial_connection # Use reader's connection ref

        if ser and ser.is_open:
            try:
                mc_conf = read.get_mc_configuration(ser)
                time.sleep(0.1) # Give VESC a break
                if mc_conf: app_conf = read.get_app_configuration(ser)
                else: error_msg = "Failed to read MC Config."
                if not app_conf and mc_conf: error_msg = "Failed to read APP Config after successful MC Config read."
            except Exception as e: error_msg = f"Error reading configs: {e}"
        else: error_msg = "Serial connection lost before reading."

        self.after(0, self._read_configs_finished, mc_conf, app_conf, error_msg)

    def _read_configs_finished(self, mc_conf, app_conf, error_msg):
        self.config_read_in_progress = False
        self.pause_datareader = False # <<<--- 데이터 리더 재개!
        self._update_config_button_states()

        if error_msg:
            self.log_to_console(f"Read Config Error: {error_msg}", error=True)
            tkinter.messagebox.showerror("Read Error", error_msg)
            self.loaded_mc_config = None; self.loaded_app_config = None
        elif mc_conf and app_conf:
            self.loaded_mc_config = mc_conf; self.loaded_app_config = app_conf
            self.log_to_console("MC & APP configurations read successfully.")
            self._update_gui_with_config() # UI 업데이트
            tkinter.messagebox.showinfo("Read Success", "Configurations read successfully!")
        else: # Should not happen if error_msg is handled, but defensively
            self.log_to_console("Read Config Error: Failed to read one or both.", error=True)
            tkinter.messagebox.showerror("Read Error", "Failed to read one or both configurations.")
            self.loaded_mc_config = None; self.loaded_app_config = None

    def write_all_configurations_event(self):
        if not self.serial_connection or not self.serial_connection.is_open: tkinter.messagebox.showerror("Error", "Please connect to VESC first."); return
        if self.config_write_in_progress: self.log_to_console("Write Config Warning: Write already in progress."); return
        if not self.loaded_mc_config or not self.loaded_app_config: tkinter.messagebox.showerror("Error", "Read configurations first or modify values."); return

        if not tkinter.messagebox.askyesno("Confirm Write", "Write ALL configurations to VESC?"): self.log_to_console("Write Config cancelled."); return

        self.log_to_console("Writing MC & APP configurations...")
        self.config_write_in_progress = True
        self.pause_datareader = True # <<<--- 데이터 리더 일시 중지!
        self._update_config_button_states()

        mc_to_write = self._get_mc_config_from_gui()
        app_to_write = self._get_app_config_from_gui()
        if not mc_to_write or not app_to_write: # 생성 실패 시
             self.log_to_console("Write Error: Failed to get config from GUI.", error=True)
             self.config_write_in_progress = False; self.pause_datareader = False
             self._update_config_button_states(); return

        write_thread = threading.Thread(target=self._write_configs_worker, args=(mc_to_write, app_to_write), daemon=True); write_thread.start()

    def _write_configs_worker(self, mc_conf, app_conf):
        success = False; error_msg = None
        ser = self.data_reader.serial_connection # Use reader's connection

        if ser and ser.is_open and mc_conf and app_conf and SetMcConf and SetAppConf and encode_set_mcconf and encode_set_appconf:
            try:
                # MC Conf 쓰기
                print("  Writing MC Config...")
                set_msg_mc = SetMcConf(); set_msg_mc.mc_configuration = mc_conf
                packet_mc = encode_set_mcconf(set_msg_mc)
                read.clear_input_buffer(ser); ser.write(packet_mc); time.sleep(1.5)

                # APP Conf 쓰기
                print("  Writing APP Config...")
                set_msg_app = SetAppConf(); set_msg_app.app_configuration = app_conf
                packet_app = encode_set_appconf(set_msg_app)
                read.clear_input_buffer(ser); ser.write(packet_app); time.sleep(1.5)
                success = True
            except Exception as e: error_msg = f"Error writing configurations: {e}"; print(error_msg)
        elif not mc_conf or not app_conf: error_msg = "Invalid config data."
        elif not SetMcConf or not SetAppConf or not encode_set_mcconf or not encode_set_appconf: error_msg = "Required Set/Encode functions not available."
        else: error_msg = "Serial connection lost."

        self.after(0, self._write_configs_finished, success, error_msg)

    def _write_configs_finished(self, success, error_msg):
        self.config_write_in_progress = False
        self.pause_datareader = False # <<<--- 데이터 리더 재개!
        self._update_config_button_states()

        if success:
            self.log_to_console("MC & APP configurations written successfully.")
            tkinter.messagebox.showinfo("Write Success", "Configurations written successfully!")
        else:
            log_msg = f"Write Config Error: {error_msg}" if error_msg else "Write Failed."
            self.log_to_console(log_msg, error=True)
            tkinter.messagebox.showerror("Write Error", log_msg)

    # !!!!! Placeholder 함수 - 실제 구현 필요 !!!!!
    def _update_gui_with_config(self):
        if self.loaded_mc_config and self.loaded_app_config:
            self.log_to_console("Updating GUI (placeholder)...")
            # 예시: motor_type
            motor_type_val = self.loaded_mc_config.get('motor_type')
            if motor_type_val == 0: self.optionmenu_1.set("BLDC")
            elif motor_type_val == 2: self.optionmenu_1.set("FOC")
            else: self.optionmenu_1.set("Other")
            # ... 여기에 각 설정 위젯 업데이트 코드 추가 ...
            print("  (GUI Update Placeholder - Add widget updates here)")
        else:
            self.log_to_console("Cannot update GUI: No config loaded.")

    def _get_mc_config_from_gui(self):
        if not self.loaded_mc_config: return None
        mc_conf = copy.deepcopy(self.loaded_mc_config)
        self.log_to_console("Getting MC config from GUI (placeholder)...")
        # 예시: motor_type
        mode_str = self.optionmenu_1.get()
        if mode_str == "BLDC": mc_conf['motor_type'] = 0
        elif mode_str == "FOC": mc_conf['motor_type'] = 2
        # ... 여기에 각 설정 위젯 값 읽어서 mc_conf 업데이트 코드 추가 ...
        print("  (Get MC Config Placeholder - Add widget reads here)")
        return mc_conf

    def _get_app_config_from_gui(self):
        if not self.loaded_app_config: return None
        app_conf = copy.deepcopy(self.loaded_app_config)
        self.log_to_console("Getting APP config from GUI (placeholder)...")
        # ... 여기에 각 설정 위젯 값 읽어서 app_conf 업데이트 코드 추가 ...
        print("  (Get APP Config Placeholder - Add widget reads here)")
        return app_conf

    def _update_config_button_states(self):
        connected = bool(self.serial_connection and self.serial_connection.is_open)
        read_state = "disabled" if (not connected or self.config_read_in_progress or self.config_write_in_progress) else "normal"
        write_state = "disabled" if (not connected or self.config_write_in_progress or self.config_read_in_progress or not self.loaded_mc_config) else "normal"
        getattr(self, "sidebar_button_read_all", None) and self.sidebar_button_read_all.configure(state=read_state)
        getattr(self, "sidebar_button_write_all", None) and self.sidebar_button_write_all.configure(state=write_state)

    # ... (다른 UI 이벤트 핸들러, 상태 업데이트, 플로팅 메서드 등 - 이전과 동일하므로 pass 로 대체) ...
    def open_input_dialog_event(self): pass
    def change_appearance_mode_event(self, new_appearance_mode: str): customtkinter.set_appearance_mode(new_appearance_mode); self._update_plot_theme()
    def change_scaling_event(self, new_scaling: str): new_scaling_float = int(new_scaling.replace("%", "")) / 100; customtkinter.set_widget_scaling(new_scaling_float)
    def _on_control_mode_change(self): self.log_to_console(f"Control mode: {self.control_mode.get()}"); self.stop_button_event(log=False); self._update_control_panel_state()
    def _update_control_panel_state(self):
        mode = self.control_mode.get(); connected = bool(self.serial_connection and self.serial_connection.is_open)
        getattr(self,"slider_duty",None) and self.slider_duty.configure(state="normal" if mode=="Duty" and connected else "disabled")
        getattr(self,"slider_current",None) and self.slider_current.configure(state="normal" if mode=="Current" and connected else "disabled")
        getattr(self,"entry_rpm",None) and self.entry_rpm.configure(state="normal" if mode=="RPM" and connected else "disabled")
        getattr(self,"button_set_rpm",None) and self.button_set_rpm.configure(state="normal" if mode=="RPM" and connected else "disabled")
        if hasattr(self,"tabview3"):
             try:
                 cp_tab = self.tabview3.tab("Control Panel"); [w.configure(state="normal" if connected else "disabled") for w in cp_tab.winfo_children() if isinstance(w, customtkinter.CTkRadioButton)]
             except: pass
        getattr(self,"stop_button",None) and self.stop_button.configure(state="normal" if connected else "disabled")
    def _slider_duty_event(self, value):
        if self.control_mode.get() == "Duty": command_duty = max(0.0, min(0.95, float(value) / 100.0)); self._send_if_connected(SetDutyCycle(command_duty))
    def _slider_current_event(self, value):
        if self.control_mode.get() == "Current": command_current = round(float(value), 1); self._send_if_connected(SetCurrent(command_current))
    def _set_rpm_event(self):
        if self.control_mode.get() == "RPM":
            try: command_rpm = int(self.rpm_var.get()); self.log_to_console(f"Setting RPM: {command_rpm}"); self._send_if_connected(SetRPM(command_rpm))
            except ValueError: tkinter.messagebox.showerror("Input Error", "Enter valid integer RPM.")
    def stop_button_event(self, log=True):
        if log: self.log_to_console("STOP pressed.")
        sent = self._send_if_connected(SetCurrent(0))
        if sent:
             if log: self.log_to_console("STOP command sent.")
             getattr(self,"duty_var",None) and self.duty_var.set(0); getattr(self,"current_var",None) and self.current_var.set(0); getattr(self,"rpm_var",None) and self.rpm_var.set(0)
        elif log: self.log_to_console("Cannot send STOP: Not connected.", error=True)
    def _send_if_connected(self, command):
        if self.serial_connection and self.serial_connection.is_open:
            success = read.send_command(self.serial_connection, command)
            if not success: self.log_to_console(f"Failed send: {command}", error=True)
            return success
        return False
    def send_console_command_event(self, event=None):
        entry_widget = getattr(self, "entry", None); command = entry_widget.get() if entry_widget else None
        if command: self.log_to_console(f"> {command}"); entry_widget.delete(0, tkinter.END); command_lower = command.lower()
        commands = {"help": lambda: self.log_to_console("Cmds: help, clear, test_fault, ports"), "clear": self._clear_console, "test_fault": self.update_labels_fault_test, "ports": lambda: self.log_to_console(f"Ports: {self.get_COM_ports()}")}; action = commands.get(command_lower)
        if action: action() if callable(action) else self.log_to_console(action)
        else: self.log_to_console("Unknown command.")
    def _clear_console(self):
        textbox_widget = getattr(self, "textbox", None)
        if textbox_widget: textbox_widget.configure(state="normal"); textbox_widget.delete("1.0", tkinter.END); textbox_widget.insert("0.0", "Console Output:\n"); textbox_widget.configure(state="disabled")
    def _update_ui_connection_state(self, connected: bool = False, connecting: bool = False):
        if connecting: status_text, status_color = "Connecting...", "orange"; connect_state, disconnect_state = "disabled", "disabled"; plot_should_be_active = False
        elif connected: status_text, status_color = "Connected", "light green"; connect_state, disconnect_state = "disabled", "normal"; plot_should_be_active = self.is_plotting
        else: status_text, status_color = "Disconnected", ("gray80", "light coral")[customtkinter.get_appearance_mode() == "Dark"]; valid_port_selected = (self.selected_com_port.get() and "found" not in self.selected_com_port.get() and "Select" not in self.selected_com_port.get()); connect_state = "normal" if valid_port_selected else "disabled"; disconnect_state = "disabled"; plot_should_be_active = False
        self.is_plotting = plot_should_be_active
        getattr(self, "sidebar_is_connected", None) and self.sidebar_is_connected.configure(text=status_text, text_color=status_color)
        getattr(self, "sidebar_button_connect", None) and self.sidebar_button_connect.configure(state=connect_state)
        getattr(self, "sidebar_button_disconnect", None) and self.sidebar_button_disconnect.configure(state=disconnect_state)
        self._update_config_button_states() # Config 버튼 상태 업데이트
        self._update_control_panel_state()
        self._update_plot_button_states()
    def print_fault_code(self, number): codes = {0:"NONE", 1:"OVER V", 2:"UNDER V", 3:"DRV", 4:"ABS CUR", 5:"FET TEMP", 6:"MOTOR TEMP", 7:"GATE >V", 8:"GATE <V", 9:"MCU <V", 10:"WDG RESET", 11:"ENC SPI", 12:"ENC SIN<", 13:"ENC SIN>", 14:"FLASH ERR", 15:"OFFSET 1", 16:"OFFSET 2", 17:"OFFSET 3", 18:"UNBALANCED", 19:"BRK", 20:"RES LOT", 21:"RES LOS", 22:"RES DOS", 23:"PV FAULT", 24:"DUTY WRITE", 25:"CURR WRITE"}; try: fault_num = int(number); return codes.get(fault_num, f"Unknown ({fault_num})") except: return "Invalid Fault"
    def update_labels(self, values):
        if not values: [getattr(self, name, None) and getattr(self, name).configure(text="N/A") for name in ["real_voltage_read", "real_duty_read", "real_mot_curr_read", "real_batt_curr_read", "real_erpm_read", "real_temp_mos_read", "real_power_read", "real_fault_read"]]; return
        try: update = lambda name, text: getattr(self,name,None) and getattr(self,name).configure(text=text); update("real_voltage_read", f"{values.v_in:.2f} V"); update("real_duty_read", f"{values.duty_cycle_now * 100:.1f} %"); update("real_mot_curr_read", f"{values.avg_motor_current:.2f} A"); update("real_batt_curr_read", f"{values.avg_input_current:.2f} A"); update("real_erpm_read", f"{values.rpm:.0f} RPM"); update("real_temp_mos_read", f"{values.temp_fet:.1f} °C"); power = values.v_in * values.avg_input_current; update("real_power_read", f"{power:.1f} W"); fault_code_int = 0; fault_code_raw = getattr(values, "mc_fault_code", 0); fault_code_int = ord(fault_code_raw) if isinstance(fault_code_raw, bytes) and fault_code_raw else int(fault_code_raw) if isinstance(fault_code_raw, int) else 0; update("real_fault_read", self.print_fault_code(fault_code_int))
        except Exception as e: print(f"Label update error: {e}")
    def update_labels_fault_test(self): pass # Placeholder
    def log_to_console(self, message, error=False):
        textbox_widget = getattr(self, "textbox", None)
        if not textbox_widget or not textbox_widget.winfo_exists(): return
        try: timestamp = time.strftime("%H:%M:%S"); formatted_message = f"[{timestamp}] {message}\n"; tag = ("error",) if error else ("normal",); textbox_widget.configure(state="normal"); textbox_widget.insert(tkinter.END, formatted_message, tag);
        if error:
            if "error" not in textbox_widget.tag_names(): textbox_widget.tag_add("error", "1.0", "end")
            textbox_widget.tag_config("error", foreground="red")
        textbox_widget.see(tkinter.END); textbox_widget.configure(state="disabled")
        except Exception as e: print(f"Console log error: {e}")
    def process_queue(self):
        try:
            while not self.data_queue.empty():
                values = self.data_queue.get_nowait()
                if self.serial_connection and self.serial_connection.is_open: self.update_labels(values);
                if hasattr(values, "timestamp"):
                    if self.plot_start_time is None: self.plot_start_time = values.timestamp
                    elapsed_time = values.timestamp - self.plot_start_time; duty = getattr(values, "duty_cycle_now", 0.0); current = getattr(values, "avg_motor_current", 0.0)
                    self.time_data.append(elapsed_time); self.duty_data.append(duty); self.current_data.append(current)
            while not self.error_queue.empty():
                error_msg = self.error_queue.get_nowait(); self.log_to_console(error_msg, error=True)
                if ("Disconnected" in error_msg or "Serial Error" in error_msg) and not (self.serial_connection and self.serial_connection.is_open): self._update_ui_connection_state(connected=False)
        except queue.Empty: pass
        except Exception as e: print(f"Queue processing error: {e}")
        finally: self.after(50, self.process_queue)
    def _setup_plot_axes(self):
        if not self.plot_figure or not self.ax_duty or not self.ax_current: return
        self.ax_duty.clear(); self.ax_current.clear(); p = plt.rcParams; bg_color=p["figure.facecolor"]; fg_color=p["text.color"]; grid_color=p["grid.color"]; blue_color=p["axes.prop_cycle"].by_key()["color"][0]; red_color=p["axes.prop_cycle"].by_key()["color"][1]
        self.ax_duty.set_xlabel("Time (s)", color=fg_color); self.ax_duty.set_ylabel("Duty Cycle", color=blue_color); self.ax_duty.set_ylim(-0.05, 1.05); self.ax_duty.tick_params(axis="y", colors=blue_color); self.ax_duty.tick_params(axis="x", colors=fg_color); self.ax_duty.grid(True, linestyle="--", alpha=0.6, color=grid_color); self.ax_duty.set_facecolor(bg_color); [spine.set_color(grid_color) for spine in self.ax_duty.spines.values()]
        self.ax_current.set_ylabel("Motor Current (A)", color=red_color); self.ax_current.tick_params(axis="y", colors=red_color); self.ax_current.set_ylim(-1, 1); self.ax_current.set_facecolor("none"); [spine.set_color(grid_color) for spine in self.ax_current.spines.values()]
        (self.line_duty,) = self.ax_duty.plot([], [], lw=1.5, color=blue_color, label="Duty"); (self.line_current,) = self.ax_current.plot([], [], lw=1.5, color=red_color, label="Current")
        lines, labels = self.ax_duty.get_legend_handles_labels(); lines2, labels2 = self.ax_current.get_legend_handles_labels()
        try: legend = self.ax_duty.legend(lines + lines2, labels + labels2, loc="upper left"); legend.get_frame().set_facecolor(bg_color); legend.get_frame().set_edgecolor(grid_color); [text.set_color(fg_color) for text in legend.get_texts()]
        except Exception as e: print(f"Legend error: {e}")
        try: self.plot_figure.tight_layout()
        except: print("Warning: tight_layout() failed.")
    def _reset_plot(self): self.time_data.clear(); self.duty_data.clear(); self.current_data.clear(); self.plot_start_time = None; line_duty=getattr(self,"line_duty",None); line_current=getattr(self,"line_current",None); ax_duty=getattr(self,"ax_duty",None); ax_current=getattr(self,"ax_current",None); plot_canvas=getattr(self,"plot_canvas",None); [line.set_data([],[]) for line in [line_duty, line_current] if line]; [ax.set_xlim(0, self.plot_time_window) for ax in [ax_duty] if ax]; [ax.set_ylim(-1, 1) for ax in [ax_current] if ax]; [canvas.draw_idle() for canvas in [plot_canvas] if canvas and canvas.get_tk_widget().winfo_exists()]
    def _trigger_plot_update(self): plot_canvas = getattr(self, "plot_canvas", None); widget_exists = plot_canvas and plot_canvas.get_tk_widget().winfo_exists()
    if self.is_plotting and widget_exists and len(self.time_data) > 0:
        try: self._update_plot_visuals()
        except Exception as e: print(f"Plot Update Error: {e}"); self.is_plotting=False; self._update_plot_button_states()
    try: self.after(self.plot_update_interval, self._trigger_plot_update)
    except Exception: pass
    def _update_plot_visuals(self):
        if not self.is_plotting or not hasattr(self, "plot_canvas") or not self.plot_canvas or not hasattr(self, "line_duty") or not self.line_duty: return
        time_list=list(self.time_data); duty_list=list(self.duty_data); current_list=list(self.current_data);
        if not time_list: return
        self.line_duty.set_data(time_list, duty_list); self.line_current.set_data(time_list, current_list)
        current_max_time = time_list[-1]; xmin = max(0, current_max_time - self.plot_time_window); xmax = xmin + self.plot_time_window; self.ax_duty.set_xlim(xmin, xmax)
        if current_list: min_curr, max_curr = min(current_list), max(current_list); padding = max((max_curr - min_curr)*0.1, 0.5); curr_ymin=min_curr-padding; curr_ymax=max_curr+padding; current_ylim=self.ax_current.get_ylim()
        else: curr_ymin, curr_ymax = -1, 1; current_ylim=(-1,1)
        if isinstance(current_ylim,(list,tuple)) and len(current_ylim)==2:
             if abs(current_ylim[0]-curr_ymin)>0.1 or abs(current_ylim[1]-curr_ymax)>0.1 or current_ylim==(-1,1): self.ax_current.set_ylim(curr_ymin, curr_ymax)
        else: self.ax_current.set_ylim(curr_ymin, curr_ymax)
        if self.plot_canvas.get_tk_widget().winfo_exists():
             try: self.plot_canvas.draw_idle()
             except Exception as e: print(f"Canvas draw error: {e}")
    def _update_plot_theme_params(self): is_dark=customtkinter.get_appearance_mode()=="Dark"; bg_color="#2B2B2B" if is_dark else "#F0F0F0"; fg_color="lightgrey" if is_dark else "#333333"; grid_color="#555555" if is_dark else "#D0D0D0"; blue_color="#5699E9"; red_color="#D95252"; plt.rcParams.update({"axes.facecolor":bg_color, "axes.edgecolor":grid_color, "axes.labelcolor":fg_color, "xtick.color":fg_color, "ytick.color":fg_color, "grid.color":grid_color, "figure.facecolor":bg_color, "text.color":fg_color, "legend.facecolor":bg_color, "legend.edgecolor":grid_color, "axes.prop_cycle":plt.cycler(color=[blue_color,red_color])})
    def _update_plot_theme(self): if not self.plot_figure: return; self._update_plot_theme_params(); self._setup_plot_axes();
    if hasattr(self,"plot_canvas") and self.plot_canvas and self.plot_canvas.get_tk_widget().winfo_exists():
         try: self.plot_canvas.draw_idle()
         except Exception as e: print(f"Theme update draw error: {e}")
    def _start_plotting_event(self): self.log_to_console("Starting plot..."); self._reset_plot(); self.is_plotting = True; self._update_plot_button_states()
    def _stop_plotting_event(self): self.log_to_console("Stopping plot."); self.is_plotting = False; self._update_plot_button_states()
    def _update_plot_button_states(self): start_button=getattr(self,"plot_start_button",None); stop_button=getattr(self,"plot_stop_button",None);
    if not start_button or not stop_button: return; connected=bool(self.serial_connection and self.serial_connection.is_open); start_state="normal" if connected and not self.is_plotting else "disabled"; stop_state="normal" if connected and self.is_plotting else "disabled"; start_button.configure(state=start_state); stop_button.configure(state=stop_state)
    def on_closing(self):
        print("Closing application..."); self.pause_datareader = True # Stop reader first
        if hasattr(self, "tk"): # Check if tk exists before calling after info/cancel
            try: after_ids = self.tk.call("after", "info").split(); [self.after_cancel(after_id) for after_id in after_ids if self.tk.call("after", "info", after_id)] # Cancel pending
            except Exception as e_cancel: print(f"Ignoring after cancel errors: {e_cancel}")
        if self.data_reader and self.data_reader.is_alive(): print("Signalling DataReader stop..."); self.data_reader.stop()
        ser = self.serial_connection # Local ref
        if ser and ser.is_open: print("Sending final stop..."); try: self.stop_button_event(log=False); time.sleep(0.1) except Exception as e_stop: print(f"Ignoring stop err: {e_stop}")
        print("Closing serial..."); self.serial_connection = None
        if ser: read.close_serial_port(ser) # Close using local ref
        if self.data_reader and self.data_reader.is_alive(): print("Waiting for DataReader join..."); self.data_reader.join(timeout=1.0);
        if self.data_reader and self.data_reader.is_alive(): print("Warning: DataReader did not stop.")
        if self.plot_figure: print("Closing plot..."); try: plt.close(self.plot_figure) except Exception as e_plt: print(f"Ignoring plot close err: {e_plt}")
        print("Destroying GUI..."); try: self.destroy() except Exception as e_destroy: print(f"Final destroy err: {e_destroy}")

if __name__ == "__main__":
    app = App()
    app.mainloop()

# --- END OF FILE realtime_gui.py ---
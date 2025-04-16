# --- START OF FILE realtime_gui.py ---

import tkinter
import tkinter.messagebox
import customtkinter
import glob
import threading
import time
import queue
import serial  # Import directly
import sys  # Need sys module for platform detection
from collections import deque  # For efficient data storage for plotting

# Import functions and classes from read.py
import read
from pyvesc.VESC.messages import SetCurrent, SetDutyCycle, SetRPM  # Import commands

# Matplotlib imports for plotting
import matplotlib

matplotlib.use("TkAgg")  # Explicitly use TkAgg backend for compatibility
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

customtkinter.set_appearance_mode("Dark")
customtkinter.set_default_color_theme("blue")


# --- DataReader Thread ---
class DataReader(threading.Thread):
    def __init__(self, app_queue, error_queue):
        threading.Thread.__init__(self)
        self.app_queue = app_queue
        self.error_queue = error_queue
        self.serial_connection = None
        self.running = True
        self.lock = threading.Lock()

    def run(self):
        while self.running:
            connection = None
            with self.lock:
                connection = self.serial_connection

            if connection and connection.is_open:
                try:
                    values = read.get_realtime_data(connection)
                    if values:
                        values.timestamp = time.time()
                        self.app_queue.put(values)

                except serial.SerialException as se:
                    error_message = f"Serial Error in Reader: {se}"
                    print(error_message)
                    # Avoid sending too many disconnect messages if error persists
                    with self.lock:
                        if (
                            self.serial_connection
                        ):  # Only report if we thought we were connected
                            self.error_queue.put(error_message)
                            ser_to_close = self.serial_connection
                            self.serial_connection = None
                            read.close_serial_port(ser_to_close)
                            self.error_queue.put("Disconnected due to serial error.")

                except Exception as e:
                    error_message = f"DataReader Error: {e}"
                    print(error_message)
                    with self.lock:
                        if self.serial_connection:
                            self.error_queue.put(error_message)
                            ser_to_close = self.serial_connection
                            self.serial_connection = None
                            read.close_serial_port(ser_to_close)
                            self.error_queue.put("Disconnected due to error.")

            # Reduce sleep slightly but ensure it yields CPU time
            time.sleep(0.05)

        print("DataReader thread stopping.")
        with self.lock:
            if self.serial_connection:
                read.close_serial_port(self.serial_connection)
                self.serial_connection = None

    def stop(self):
        print("Stopping DataReader thread...")
        self.running = False

    def set_serial_connection(self, ser):
        with self.lock:
            if self.serial_connection and self.serial_connection.is_open:
                read.close_serial_port(self.serial_connection)
            self.serial_connection = ser

    def get_serial_connection(self):
        with self.lock:
            return self.serial_connection


# --- Main Application Class ---
class App(customtkinter.CTk):
    def __init__(self):
        # Call super().__init__() FIRST
        super().__init__()

        # Data Handling
        self.data_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.data_reader = DataReader(self.data_queue, self.error_queue)
        self.serial_connection = None

        # Plotting Attributes
        self.plot_update_interval = 100  # ms
        self.plot_time_window = 15  # seconds
        self.plot_max_points = (
            int(self.plot_time_window / 0.05) + 2
        )  # Add buffer points

        self.time_data = deque(maxlen=self.plot_max_points)
        self.duty_data = deque(maxlen=self.plot_max_points)
        self.current_data = deque(maxlen=self.plot_max_points)
        self.plot_start_time = None
        self.is_plotting = False  # Start with plotting stopped

        self.plot_figure = None
        self.ax_duty = None
        self.ax_current = None
        self.line_duty = None
        self.line_current = None
        self.plot_canvas = None
        self.plot_toolbar = None
        self.plot_start_button = None
        self.plot_stop_button = None

        # Window Configuration
        self.title("ROTOM CONTROL")
        self.geometry(f"{1200}x{750}")  # Adjusted size

        # Configure grid layout (10% : 60% : 30%)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=6)
        self.grid_columnconfigure(4, weight=3)
        # Ensure rows can expand if needed, esp. for plot
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Create UI Sections (Order matters for dependencies)
        self._create_sidebar()
        self._create_main_tabs_and_plot()
        self._create_realtime_panel()
        self._create_control_panel()

        # Default Values & States
        self.com_port_optionmenu.set("Select Port")
        self.appearance_mode_optionemenu.set("Dark")
        self.scaling_optionemenu.set("100%")
        self.optionmenu_1.set("ESC Mode")
        self.optionmenu_2.set("Motor Direction")
        self.sensor_option.set("Sensor Type")
        self.sensor_ABI_option.set("ABI Counts")

        # Set initial states AFTER all widgets are created
        self._on_com_port_selected(self.selected_com_port.get())
        self._update_ui_connection_state(connected=False)

        # Start background tasks
        self.data_reader.start()
        self.process_queue()  # Start queue checking loop
        # Start the plot update trigger loop (it checks is_plotting flag internally)
        self.after(self.plot_update_interval, self._trigger_plot_update)

        # Handle window close event
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # --- UI Creation Methods ---
    def _create_sidebar(self):
        self.sidebar_frame = customtkinter.CTkFrame(self, width=140, corner_radius=0)
        self.sidebar_frame.grid(
            row=0, column=0, rowspan=3, sticky="nsew"
        )  # Span all rows

        # Add row configure for sidebar if spacing is needed (optional)
        # self.sidebar_frame.grid_rowconfigure(10, weight=1) # Example spacer row

        self.logo_label = customtkinter.CTkLabel(
            self.sidebar_frame,
            text="ROTOM\nCONTROL",
            font=customtkinter.CTkFont(size=20, weight="bold"),
        )
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        self.selected_com_port = customtkinter.StringVar(value="")
        self.com_port_optionmenu = customtkinter.CTkOptionMenu(
            self.sidebar_frame,
            values=self.get_COM_ports(),  # Get ports on init
            variable=self.selected_com_port,
            command=self._on_com_port_selected,
        )
        self.com_port_optionmenu.grid(row=1, column=0, padx=20, pady=(10, 10))

        self.sidebar_button_refresh = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self.sidebar_button_refresh,
            text="Refresh Ports",
        )
        self.sidebar_button_refresh.grid(row=2, column=0, padx=20, pady=10)

        # Connect button uses the renamed event handler
        self.sidebar_button_connect = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self._sidebar_button_connect_event,
            text="Connect",
        )
        self.sidebar_button_connect.grid(row=3, column=0, padx=20, pady=10)

        self.sidebar_button_disconnect = customtkinter.CTkButton(
            self.sidebar_frame,
            command=self.sidebar_button_disconnect,
            text="Disconnect",
        )
        self.sidebar_button_disconnect.grid(row=4, column=0, padx=20, pady=10)

        self.sidebar_is_connected = customtkinter.CTkLabel(
            self.sidebar_frame,
            text="Disconnected",
            font=customtkinter.CTkFont(weight="bold"),
        )
        self.sidebar_is_connected.grid(row=5, column=0, padx=20, pady=10)

        self.sidebar_button_read = customtkinter.CTkButton(
            self.sidebar_frame, command=self.sidebar_button_read, text="Read Conf"
        )
        self.sidebar_button_read.grid(row=6, column=0, padx=20, pady=10)

        self.sidebar_button_write = customtkinter.CTkButton(
            self.sidebar_frame, command=self.sidebar_button_write, text="Write Conf"
        )
        self.sidebar_button_write.grid(row=7, column=0, padx=20, pady=10)

        # Appearance/Scaling towards the bottom
        self.appearance_mode_optionemenu = customtkinter.CTkOptionMenu(
            self.sidebar_frame,
            values=["Light", "Dark", "System"],
            command=self.change_appearance_mode_event,
        )
        self.appearance_mode_optionemenu.grid(
            row=8, column=0, padx=20, pady=(20, 10)
        )  # Add vertical space

        self.scaling_optionemenu = customtkinter.CTkOptionMenu(
            self.sidebar_frame,
            values=["80%", "90%", "100%", "110%", "120%"],
            command=self.change_scaling_event,
        )
        self.scaling_optionemenu.grid(row=9, column=0, padx=20, pady=(10, 20))

    def _create_main_tabs_and_plot(self):
        self.tabview = customtkinter.CTkTabview(self)
        # Span rows 0, 1 and columns 1, 2, 3
        self.tabview.grid(
            row=0,
            rowspan=2,
            column=1,
            columnspan=3,
            padx=(20, 0),
            pady=(20, 20),
            sticky="nsew",
        )

        tabs = ["Setting", "Detection", "Sensor", "Communication", "Console", "Plot"]
        for tab in tabs:
            self.tabview.add(tab)

        # --- Setting Tab ---
        setting_tab = self.tabview.tab("Setting")
        setting_tab.grid_columnconfigure(
            (0, 1), weight=1
        )  # Allow widgets to expand if needed
        self.optionmenu_1 = customtkinter.CTkOptionMenu(
            setting_tab, dynamic_resizing=True, values=["BLDC", "FOC"]
        )
        self.optionmenu_1.grid(row=0, column=0, padx=20, pady=(20, 10))
        self.optionmenu_2 = customtkinter.CTkOptionMenu(
            setting_tab, dynamic_resizing=True, values=["True", "False"]
        )
        self.optionmenu_2.grid(row=1, column=0, padx=20, pady=(10, 10))
        self.string_input_button = customtkinter.CTkButton(
            setting_tab, text="Open Input Dialog", command=self.open_input_dialog_event
        )
        self.string_input_button.grid(row=2, column=0, padx=20, pady=(10, 10))

        # --- Sensor Tab ---
        sensor_tab = self.tabview.tab("Sensor")
        sensor_tab.grid_columnconfigure((0, 1), weight=1)
        self.sensor_option = customtkinter.CTkOptionMenu(
            sensor_tab, dynamic_resizing=True, values=["None", "AS5047", "ABI", "Hall"]
        )
        self.sensor_option.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="w")
        self.sensor_ABI_option = customtkinter.CTkOptionMenu(
            sensor_tab, dynamic_resizing=True, values=["2048", "4000", "4096", "8192"]
        )  # Example counts
        self.sensor_ABI_option.grid(row=1, column=0, padx=20, pady=(10, 10), sticky="w")

        # --- Console Tab ---
        console_tab = self.tabview.tab("Console")
        console_tab.grid_columnconfigure(0, weight=1)  # Textbox expands
        console_tab.grid_columnconfigure(1, weight=0)  # Button fixed size
        console_tab.grid_rowconfigure(0, weight=1)  # Textbox expands vertically
        console_tab.grid_rowconfigure(1, weight=0)  # Entry/Button fixed height
        self.textbox = customtkinter.CTkTextbox(console_tab, corner_radius=5)
        self.textbox.grid(
            row=0, column=0, columnspan=2, padx=10, pady=(10, 5), sticky="nsew"
        )
        self.textbox.insert("0.0", "Console Output:\n")
        self.textbox.configure(state="disabled")  # Make read-only
        self.entry = customtkinter.CTkEntry(
            console_tab, placeholder_text="Type command (e.g., help)"
        )
        self.entry.grid(row=1, column=0, padx=(10, 5), pady=(5, 10), sticky="ew")
        self.entry.bind("<Return>", self.send_console_command_event)  # Bind Enter key
        self.send_button = customtkinter.CTkButton(
            console_tab, text="Send", width=80, command=self.send_console_command_event
        )
        self.send_button.grid(row=1, column=1, padx=(0, 10), pady=(5, 10), sticky="w")

        # --- Plot Tab Setup ---
        plot_tab = self.tabview.tab("Plot")
        # Configure grid inside the plot tab
        plot_tab.grid_columnconfigure(0, weight=1)  # Single column for content
        plot_tab.grid_rowconfigure(0, weight=1)  # Canvas row expands
        plot_tab.grid_rowconfigure(1, weight=0)  # Toolbar fixed height
        plot_tab.grid_rowconfigure(2, weight=0)  # Button row fixed height

        # Set matplotlib style and parameters
        plt.style.use(
            "seaborn-v0_8-darkgrid"
            if customtkinter.get_appearance_mode() == "Dark"
            else "seaborn-v0_8-whitegrid"
        )
        self._update_plot_theme_params()

        # Create Figure and Axes
        self.plot_figure = Figure(figsize=(5, 4), dpi=100)
        self.plot_figure.set_facecolor(plt.rcParams["figure.facecolor"])
        self.ax_duty = self.plot_figure.add_subplot(111)
        self.ax_current = self.ax_duty.twinx()  # Share X axis

        # Setup axes appearance, labels, lines, legend
        self._setup_plot_axes()

        # Create Canvas and embed in Tkinter
        self.plot_canvas = FigureCanvasTkAgg(self.plot_figure, master=plot_tab)
        self.plot_canvas_widget = self.plot_canvas.get_tk_widget()
        self.plot_canvas_widget.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        # Create Toolbar Frame and Toolbar
        toolbar_frame = customtkinter.CTkFrame(plot_tab, fg_color="transparent")
        toolbar_frame.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 5))
        try:
            self.plot_toolbar = NavigationToolbar2Tk(self.plot_canvas, toolbar_frame)
            self.plot_toolbar.update()
        except Exception as e:
            print(f"Error creating Matplotlib toolbar: {e}")
            self.log_to_console(f"Plot Toolbar Error: {e}", error=True)
            self.plot_toolbar = None

        # Create Plot Start/Stop Button Frame and Buttons
        button_frame = customtkinter.CTkFrame(plot_tab, fg_color="transparent")
        button_frame.grid(row=2, column=0, pady=(5, 10))
        # Center buttons within the frame (optional, could use pack)
        # button_frame.grid_columnconfigure(0, weight=1)
        # button_frame.grid_columnconfigure(1, weight=0) # Start button
        # button_frame.grid_columnconfigure(2, weight=0) # Stop button
        # button_frame.grid_columnconfigure(3, weight=1)

        self.plot_start_button = customtkinter.CTkButton(
            button_frame,
            text="Start Plotting",
            command=self._start_plotting_event,
            fg_color="green",
            hover_color="dark green",
        )
        self.plot_start_button.pack(
            side=tkinter.LEFT, padx=10
        )  # Use pack for simple side-by-side

        self.plot_stop_button = customtkinter.CTkButton(
            button_frame,
            text="Stop Plotting",
            command=self._stop_plotting_event,
            fg_color="red",
            hover_color="dark red",
        )
        self.plot_stop_button.pack(side=tkinter.LEFT, padx=10)  # Use pack

    def _create_realtime_panel(self):
        self.tabview2 = customtkinter.CTkTabview(self)
        # Span rows 0, 1, 2 and column 4
        self.tabview2.grid(
            row=0, rowspan=3, column=4, padx=(20, 20), pady=(20, 20), sticky="nsew"
        )
        self.tabview2.add("Realtime Data")
        rt_tab = self.tabview2.tab("Realtime Data")
        rt_tab.grid_columnconfigure((0, 1), weight=1)  # Allow columns to expand
        # Rows have fixed height based on content
        rt_tab.grid_rowconfigure(list(range(8)), weight=0)

        # Helper function for creating label pairs with CENTER alignment
        def create_rt_label_pair(parent, text, row, col):
            lbl_text = customtkinter.CTkLabel(parent, text=text, font=("Arial", 16))
            lbl_text.grid(
                row=row, column=col, padx=10, pady=(10, 0), sticky="s"
            )  # South-Center
            lbl_value = customtkinter.CTkLabel(
                parent, text="N/A", font=("Arial", 20, "bold")
            )
            lbl_value.grid(
                row=row + 1, column=col, padx=10, pady=(0, 10), sticky="n"
            )  # North-Center
            return lbl_value

        # Create all label pairs
        self.real_voltage_read = create_rt_label_pair(rt_tab, "Voltage", 0, 0)
        self.real_duty_read = create_rt_label_pair(rt_tab, "Duty Cycle", 0, 1)
        self.real_mot_curr_read = create_rt_label_pair(rt_tab, "Motor Current", 2, 0)
        self.real_batt_curr_read = create_rt_label_pair(rt_tab, "Battery Current", 2, 1)
        self.real_erpm_read = create_rt_label_pair(rt_tab, "ERPM", 4, 0)
        self.real_temp_mos_read = create_rt_label_pair(rt_tab, "MOSFET Temp", 4, 1)
        self.real_power_read = create_rt_label_pair(rt_tab, "Input Power", 6, 0)
        self.real_fault_read = create_rt_label_pair(rt_tab, "Fault Code", 6, 1)

    def _create_control_panel(self):
        self.tabview3 = customtkinter.CTkTabview(self)
        # Row 2, spanning columns 1, 2, 3
        self.tabview3.grid(
            row=2, column=1, columnspan=3, padx=(20, 0), pady=(0, 20), sticky="nsew"
        )
        self.tabview3.add("Control Panel")
        cp_tab = self.tabview3.tab("Control Panel")
        # Configure grid inside control panel tab
        cp_tab.grid_columnconfigure((0, 1, 2), weight=1)  # Sliders/Inputs expand
        cp_tab.grid_columnconfigure(3, weight=0)  # Stop button fixed
        cp_tab.grid_rowconfigure(0, weight=0)  # Radio buttons fixed height
        cp_tab.grid_rowconfigure(1, weight=1)  # Sliders/Inputs expand vertically
        cp_tab.grid_rowconfigure(2, weight=0)  # Labels fixed height

        # Control Mode Radio Buttons
        self.control_mode = tkinter.StringVar(value="None")
        modes = [("Duty", "Duty"), ("Current", "Current"), ("RPM", "RPM")]
        for i, (text, mode) in enumerate(modes):
            rb = customtkinter.CTkRadioButton(
                cp_tab,
                text=text,
                variable=self.control_mode,
                value=mode,
                command=self._on_control_mode_change,
            )
            rb.grid(row=0, column=i, pady=10, padx=10, sticky="n")

        # Duty Slider
        self.duty_var = tkinter.DoubleVar()
        self.slider_duty = customtkinter.CTkSlider(
            cp_tab,
            from_=0,
            to=100,
            number_of_steps=100,
            variable=self.duty_var,
            command=self._slider_duty_event,
            orientation="horizontal",
        )
        self.slider_duty.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        self.slider_duty.set(0)
        self.text_duty = customtkinter.CTkLabel(
            cp_tab, textvariable=self.duty_var, font=("", 16)
        )
        self.text_duty.grid(row=2, column=0, padx=20, pady=0, sticky="n")

        # Current Slider
        self.current_var = tkinter.DoubleVar()
        max_abs_current = 20  # Example Max Current
        current_steps = max_abs_current * 2 * 10  # 0.1A resolution
        self.slider_current = customtkinter.CTkSlider(
            cp_tab,
            from_=-max_abs_current,
            to=max_abs_current,
            number_of_steps=current_steps,
            variable=self.current_var,
            command=self._slider_current_event,
            orientation="horizontal",
        )
        self.slider_current.grid(row=1, column=1, padx=20, pady=5, sticky="ew")
        self.slider_current.set(0)
        self.text_current = customtkinter.CTkLabel(
            cp_tab, textvariable=self.current_var, font=("", 16)
        )
        self.text_current.grid(row=2, column=1, padx=20, pady=0, sticky="n")

        # RPM Input
        self.rpm_var = tkinter.IntVar()
        self.entry_rpm = customtkinter.CTkEntry(
            cp_tab, textvariable=self.rpm_var, width=80
        )
        self.entry_rpm.grid(row=1, column=2, padx=20, pady=5, sticky="n")
        self.button_set_rpm = customtkinter.CTkButton(
            cp_tab, text="Set RPM", command=self._set_rpm_event, width=80
        )
        self.button_set_rpm.grid(row=2, column=2, padx=20, pady=5, sticky="n")

        # Stop Button
        self.stop_button = customtkinter.CTkButton(
            cp_tab,
            command=self.stop_button_event,
            text="STOP",
            fg_color="#D9262C",
            hover_color="#B7181E",
            width=100,
            height=40,
            font=customtkinter.CTkFont(size=16, weight="bold"),
        )
        self.stop_button.grid(
            row=0, column=3, rowspan=3, padx=20, pady=10, sticky="ns"
        )  # Span rows 0-2

        # Set initial states
        self._update_control_panel_state()

    # --- Event Handlers & Logic ---
    def _on_com_port_selected(self, selected_port):
        # Enable/disable connect button based on selection
        if hasattr(self, "sidebar_button_connect"):
            valid_port = (
                selected_port
                and "found" not in selected_port
                and "Select" not in selected_port
            )
            self.sidebar_button_connect.configure(
                state="normal" if valid_port else "disabled"
            )

    def get_COM_ports(self):
        # Improved COM port detection
        ports = []
        platform = sys.platform
        if "linux" in platform or "darwin" in platform:
            ports.extend(glob.glob("/dev/ttyACM*"))
            ports.extend(glob.glob("/dev/ttyUSB*"))
            ports.extend(glob.glob("/dev/cu.*"))  # macOS specific
        elif "win" in platform:
            try:
                from serial.tools.list_ports import comports

                ports = [port.device for port in comports()]
            except ImportError:
                print(
                    "Warning: pyserial 'list_ports' not available. Falling back to glob."
                )
                ports.extend(glob.glob("COM*"))  # Fallback for Windows
        # Filter out common non-serial ports
        ports = [
            p
            for p in ports
            if all(f not in p for f in ["Bluetooth", "Wireless", "Dial-Up"])
        ]
        return sorted(ports) if ports else ["No ports found"]

    def sidebar_button_refresh(self):
        # Refresh COM port dropdown
        ports = self.get_COM_ports()
        current_selection = self.selected_com_port.get()
        self.com_port_optionmenu.configure(values=ports)  # Update the dropdown values

        # Set the selection after refreshing
        if not ports or ports[0] == "No ports found":
            self.com_port_optionmenu.set("No ports found")
            self.selected_com_port.set("")
        else:
            # Try to keep the current selection if it's still valid
            if current_selection in ports:
                self.com_port_optionmenu.set(current_selection)
                # self.selected_com_port variable is already set
            else:
                # Otherwise, select the first available port
                self.com_port_optionmenu.set(ports[0])
                self.selected_com_port.set(ports[0])  # Update variable too

        # Update the connect button state based on the final selection
        self._on_com_port_selected(self.selected_com_port.get())

        print("COM Ports Refreshed")
        self.log_to_console("COM Ports Refreshed")

    # Renamed method for the connect button event
    def _sidebar_button_connect_event(self):
        port = self.selected_com_port.get()
        if not port or port in ["Select Port", "No ports found"]:
            tkinter.messagebox.showwarning(
                "Connect Error", "Please select a valid COM port."
            )
            return

        self.log_to_console(f"Attempting to connect to {port}...")
        # Update UI immediately to show "Connecting..."
        if hasattr(self, "sidebar_is_connected"):  # Check if widget exists
            self.sidebar_is_connected.configure(
                text="Connecting...", text_color="orange"
            )
        self._update_ui_connection_state(connecting=True)  # Disable buttons etc.
        self.update()  # Force UI update

        # Start connection attempt in a separate thread
        connect_thread = threading.Thread(
            target=self._attempt_connection, args=(port,), daemon=True
        )
        connect_thread.start()

    def _attempt_connection(self, port):
        """Worker function for serial connection attempt (runs in thread)."""
        try:
            # Close previous connection safely
            if self.serial_connection and self.serial_connection.is_open:
                temp_ser = self.serial_connection
                self.serial_connection = None  # Clear app reference first
                self.data_reader.set_serial_connection(None)  # Tell reader thread
                read.close_serial_port(temp_ser)  # Close the port
                time.sleep(0.1)  # Brief pause

            # Attempt to open new connection
            new_ser = serial.Serial(
                port, baudrate=115200, timeout=0.2
            )  # Slightly longer timeout

            if new_ser.is_open:
                self.serial_connection = new_ser  # Store new connection object
                self.data_reader.set_serial_connection(
                    self.serial_connection
                )  # Pass to reader
                # Schedule UI update in main thread using self.after
                self.after(0, self._connection_success, port)
            else:
                # Should not happen if serial.Serial doesn't raise error, but handle defensively
                self.serial_connection = None
                raise serial.SerialException(
                    f"Serial port {port} did not open (no exception raised)."
                )

        except serial.SerialException as e:
            self.serial_connection = None  # Ensure connection is None on failure
            self.after(0, self._connection_failure, port, str(e))
        except Exception as e:  # Catch other potential errors (permissions, etc.)
            self.serial_connection = None
            self.after(
                0, self._connection_failure, port, f"An unexpected error occurred: {e}"
            )

    def _connection_success(self, port):
        """Callback run in main thread on successful connection."""
        self._update_ui_connection_state(connected=True)
        self.log_to_console(f"Connected to {port} successfully.")
        # Don't auto-start plotting, enable Start button via _update_ui...

    def _connection_failure(self, port, error_msg):
        """Callback run in main thread on failed connection."""
        self.log_to_console(f"Connection Failed to {port}: {error_msg}", error=True)
        tkinter.messagebox.showerror(
            "Connection Error", f"Failed to connect to {port}:\n{error_msg}"
        )
        self.serial_connection = None  # Ensure connection is cleared
        self.data_reader.set_serial_connection(None)  # Ensure reader knows
        self._update_ui_connection_state(connected=False)  # Update UI state

    def sidebar_button_disconnect(self):
        """Handles disconnect button click."""
        self.log_to_console("Disconnecting...")
        self.is_plotting = False  # Stop plotting visually
        ser_to_close = self.serial_connection  # Get ref before clearing
        self.serial_connection = None
        self.data_reader.set_serial_connection(None)  # Tell reader

        if ser_to_close:
            read.close_serial_port(ser_to_close)  # Close the port

        self._update_ui_connection_state(connected=False)  # Update button states etc.
        self.after(50, self.update_labels, None)  # Schedule label reset shortly after
        self.log_to_console("Disconnected.")
        self._reset_plot()  # Clear plot data

    # Placeholders for Read/Write Config buttons
    def sidebar_button_read(self):
        self.log_to_console("Read Config button pressed (Not Implemented)")
        tkinter.messagebox.showinfo(
            "Not Implemented", "Reading VESC configuration is not yet implemented."
        )

    def sidebar_button_write(self):
        self.log_to_console("Write Config button pressed (Not Implemented)")
        tkinter.messagebox.showinfo(
            "Not Implemented", "Writing VESC configuration is not yet implemented."
        )

    def _on_control_mode_change(self):
        """Handles radio button selection for control mode."""
        mode = self.control_mode.get()
        self.log_to_console(f"Control mode changed to: {mode}")
        # Safety: Stop motor when changing control modes
        if self.serial_connection and self.serial_connection.is_open:
            self.stop_button_event(log=False)  # Stop without logging it as manual stop
        self._update_control_panel_state()  # Update enabled/disabled states

    def _update_control_panel_state(self):
        """Updates enabled/disabled state of control panel widgets."""
        mode = self.control_mode.get()
        connected = bool(self.serial_connection and self.serial_connection.is_open)

        # Use getattr for safety in case widgets aren't created yet
        getattr(self, "slider_duty", None) and self.slider_duty.configure(
            state="normal" if mode == "Duty" and connected else "disabled"
        )
        getattr(self, "slider_current", None) and self.slider_current.configure(
            state="normal" if mode == "Current" and connected else "disabled"
        )
        getattr(self, "entry_rpm", None) and self.entry_rpm.configure(
            state="normal" if mode == "RPM" and connected else "disabled"
        )
        getattr(self, "button_set_rpm", None) and self.button_set_rpm.configure(
            state="normal" if mode == "RPM" and connected else "disabled"
        )

        # Update radio buttons state
        if hasattr(self, "tabview3"):  # Check if tabview exists
            try:  # Check if tab exists
                cp_tab = self.tabview3.tab("Control Panel")
                for widget in cp_tab.winfo_children():
                    if isinstance(widget, customtkinter.CTkRadioButton):
                        widget.configure(state="normal" if connected else "disabled")
            except Exception:  # Tab might not be fully initialized yet
                pass  # Ignore if tab/widgets not ready

        # Update stop button state
        getattr(self, "stop_button", None) and self.stop_button.configure(
            state="normal" if connected else "disabled"
        )

    # --- Control Command Sending Methods ---
    def _slider_duty_event(self, value):
        """Sends SetDutyCycle command based on slider."""
        if self.control_mode.get() == "Duty":
            duty_value = float(value) / 100.0
            # Clamp duty cycle command (e.g., 0.0 to 0.95 for safety)
            command_duty = max(0.0, min(0.95, duty_value))
            self._send_if_connected(SetDutyCycle(command_duty))

    def _slider_current_event(self, value):
        """Sends SetCurrent command based on slider."""
        if self.control_mode.get() == "Current":
            command_current = round(float(value), 1)  # Round to 1 decimal place
            self._send_if_connected(SetCurrent(command_current))

    def _set_rpm_event(self):
        """Sends SetRPM command based on entry."""
        if self.control_mode.get() == "RPM":
            try:
                command_rpm = int(self.rpm_var.get())
                self.log_to_console(f"Setting RPM: {command_rpm}")
                self._send_if_connected(SetRPM(command_rpm))
            except ValueError:
                self.log_to_console("Invalid RPM value entered.", error=True)
                tkinter.messagebox.showerror(
                    "Input Error", "Please enter a valid integer for RPM."
                )

    def stop_button_event(self, log=True):
        """Sends SetCurrent(0) command."""
        if log:
            self.log_to_console("STOP button pressed.")
        sent = self._send_if_connected(SetCurrent(0))
        if sent:
            if log:
                self.log_to_console("STOP command sent.")
            # Reset control input variables/widgets visually
            getattr(self, "duty_var", None) and self.duty_var.set(0)
            getattr(self, "current_var", None) and self.current_var.set(0)
            getattr(self, "rpm_var", None) and self.rpm_var.set(0)
        elif log:
            self.log_to_console("Cannot send STOP: Not connected.", error=True)

    def _send_if_connected(self, command):
        """Helper to send a pyvesc command only if connected."""
        if self.serial_connection and self.serial_connection.is_open:
            success = read.send_command(self.serial_connection, command)
            if not success:
                self.log_to_console(f"Failed to send command: {command}", error=True)
            return success
        else:
            # Optionally log that command wasn't sent due to no connection
            # self.log_to_console("Command not sent: Not connected.")
            return False

    # --- End Control Command Sending ---

    # --- Console Command Handling ---
    def send_console_command_event(self, event=None):
        """Handles Enter key or Send button in console."""
        entry_widget = getattr(self, "entry", None)
        if not entry_widget:
            return
        command = entry_widget.get()
        if command:
            self.log_to_console(f"> {command}")  # Log user input
            entry_widget.delete(0, tkinter.END)  # Clear entry field

            command_lower = command.lower()
            # Define commands and their actions
            commands = {
                "help": lambda: self.log_to_console(
                    "Available commands: help, clear, test_fault, ports"
                ),
                "clear": self._clear_console,
                "test_fault": self.update_labels_fault_test,
                "ports": lambda: self.log_to_console(
                    f"Available ports: {self.get_COM_ports()}"
                ),
            }
            action = commands.get(command_lower)  # Look up action

            if action:
                if callable(action):
                    action()  # Call the function
                else:
                    self.log_to_console(action)  # Print help string etc.
            else:
                self.log_to_console("Unknown command. Type 'help'.")
                # Consider: Send unknown commands directly to VESC? (Advanced)

    def _clear_console(self):
        """Clears the console textbox."""
        textbox_widget = getattr(self, "textbox", None)
        if textbox_widget:
            textbox_widget.configure(state="normal")
            textbox_widget.delete("1.0", tkinter.END)
            textbox_widget.insert("0.0", "Console Output:\n")  # Add header back
            textbox_widget.configure(state="disabled")

    # --- End Console Command Handling ---

    # --- Other UI Event Handlers ---
    def open_input_dialog_event(self):
        """Opens a simple input dialog."""
        dialog = customtkinter.CTkInputDialog(text="Enter value:", title="Input Dialog")
        value = dialog.get_input()
        if value:
            self.log_to_console(f"Input Dialog returned: {value}")
            print("CTkInputDialog:", value)

    def change_appearance_mode_event(self, new_appearance_mode: str):
        """Changes the application theme."""
        customtkinter.set_appearance_mode(new_appearance_mode)
        self.log_to_console(f"Appearance mode changed to: {new_appearance_mode}")
        self._update_plot_theme()  # Update plot colors to match

    def change_scaling_event(self, new_scaling: str):
        """Changes the UI scaling."""
        new_scaling_float = int(new_scaling.replace("%", "")) / 100
        customtkinter.set_widget_scaling(new_scaling_float)
        self.log_to_console(f"UI Scaling changed to: {new_scaling}")

    # --- End Other UI Event Handlers ---

    # --- State Update and Data Display ---
    def _update_ui_connection_state(
        self, connected: bool = False, connecting: bool = False
    ):
        """Updates UI elements based on connection status."""
        if connecting:
            status_text, status_color = "Connecting...", "orange"
            connect_state, disconnect_state, read_write_state = (
                "disabled",
                "disabled",
                "disabled",
            )
            plot_should_be_active = False  # Don't plot during connection attempt
        elif connected:
            status_text, status_color = "Connected", "light green"
            connect_state, disconnect_state, read_write_state = (
                "disabled",
                "normal",
                "normal",
            )
            plot_should_be_active = (
                self.is_plotting
            )  # Keep current plotting state if reconnecting etc.
        else:  # Disconnected
            status_text, status_color = (
                "Disconnected",
                ("gray80", "light coral")[
                    customtkinter.get_appearance_mode() == "Dark"
                ],
            )
            valid_port_selected = (
                self.selected_com_port.get()
                and "found" not in self.selected_com_port.get()
                and "Select" not in self.selected_com_port.get()
            )
            connect_state = "normal" if valid_port_selected else "disabled"
            disconnect_state, read_write_state = "disabled", "disabled"
            plot_should_be_active = False  # Stop plotting on disconnect

        # Update plotting flag based on connection state change
        self.is_plotting = plot_should_be_active

        # Safely update sidebar widgets
        getattr(
            self, "sidebar_is_connected", None
        ) and self.sidebar_is_connected.configure(
            text=status_text, text_color=status_color
        )
        getattr(
            self, "sidebar_button_connect", None
        ) and self.sidebar_button_connect.configure(state=connect_state)
        getattr(
            self, "sidebar_button_disconnect", None
        ) and self.sidebar_button_disconnect.configure(state=disconnect_state)
        getattr(
            self, "sidebar_button_read", None
        ) and self.sidebar_button_read.configure(state=read_write_state)
        getattr(
            self, "sidebar_button_write", None
        ) and self.sidebar_button_write.configure(state=read_write_state)

        # Update other dependent UI sections
        self._update_control_panel_state()
        self._update_plot_button_states()  # Update plot buttons

    def print_fault_code(self, number):
        """Converts VESC fault code number to human-readable string."""
        # Compact fault codes for brevity
        codes = {
            0: "NONE",
            1: "OVER V",
            2: "UNDER V",
            3: "DRV",
            4: "ABS CUR",
            5: "FET TEMP",
            6: "MOTOR TEMP",
            7: "GATE >V",
            8: "GATE <V",
            9: "MCU <V",
            10: "WDG RESET",
            11: "ENC SPI",
            12: "ENC SIN<",
            13: "ENC SIN>",
            14: "FLASH ERR",
            15: "OFFSET 1",
            16: "OFFSET 2",
            17: "OFFSET 3",
            18: "UNBALANCED",
            19: "BRK",
            20: "RES LOT",
            21: "RES LOS",
            22: "RES DOS",
            23: "PV FAULT",
            24: "DUTY WRITE",
            25: "CURR WRITE",
        }
        try:
            fault_num = int(number)
            return codes.get(fault_num, f"Unknown ({fault_num})")
        except (ValueError, TypeError):
            return "Invalid Fault"  # Handle non-integer input

    def update_labels(self, values):
        """Updates the real-time data labels safely."""
        if not values:  # Handle case where values is None (e.g., on disconnect)
            for label_name in [
                "real_voltage_read",
                "real_duty_read",
                "real_mot_curr_read",
                "real_batt_curr_read",
                "real_erpm_read",
                "real_temp_mos_read",
                "real_power_read",
                "real_fault_read",
            ]:
                label = getattr(self, label_name, None)
                if label:
                    label.configure(text="N/A")
            return

        # Proceed if values object exists
        try:
            # Helper lambda to update label safely
            update_widget_text = lambda name, text: getattr(
                self, name, None
            ) and getattr(self, name).configure(text=text)

            # Update each label
            update_widget_text("real_voltage_read", f"{values.v_in:.2f} V")
            update_widget_text(
                "real_duty_read", f"{values.duty_cycle_now * 100:.1f} %"
            )  # Assumes 0-1 range
            update_widget_text(
                "real_mot_curr_read", f"{values.avg_motor_current:.2f} A"
            )
            update_widget_text(
                "real_batt_curr_read", f"{values.avg_input_current:.2f} A"
            )
            update_widget_text("real_erpm_read", f"{values.rpm:.0f} RPM")
            update_widget_text("real_temp_mos_read", f"{values.temp_fet:.1f} °C")
            power = values.v_in * values.avg_input_current
            update_widget_text("real_power_read", f"{power:.1f} W")

            # Decode fault code carefully
            fault_code_int = 0
            fault_code_raw = getattr(
                values, "mc_fault_code", 0
            )  # Default to 0 if attribute missing
            if isinstance(fault_code_raw, bytes) and fault_code_raw:
                try:
                    fault_code_int = ord(fault_code_raw)
                except TypeError:
                    pass  # Handle empty bytes string
            elif isinstance(fault_code_raw, int):
                fault_code_int = fault_code_raw
            update_widget_text("real_fault_read", self.print_fault_code(fault_code_int))

        except AttributeError as e:
            # This might happen if pyvesc response format changes or is incomplete
            print(f"Label update warning (Attribute Missing): {e}")
            # Avoid flooding console, maybe log only once
        except Exception as e:
            # Catch other unexpected errors during update
            print(f"Label update error (Other): {e}")

    def update_labels_fault_test(self):
        """Cycles through fault codes for display testing via console command."""

        # Define a simple mock class locally
        class MockFaultData:
            v_in = 24
            duty_cycle_now = 0.5
            avg_motor_current = 5
            avg_input_current = 2
            rpm = 10000
            temp_fet = 45
            mc_fault_code = 0

        # List of fault codes to test
        fault_codes_to_test = [0, 1, 2, 3, 4, 5, 6, 8, 11, 25, 99]  # Known and unknown

        # Get current fault text
        current_fault_text = "N/A"
        fault_label = getattr(self, "real_fault_read", None)
        if fault_label:
            current_fault_text = fault_label.cget("text")

        # Map text back to code
        current_code_int = -1
        fault_text_to_code = {v: k for k, v in self.print_fault_code(0).items()}
        if "Unknown" in current_fault_text:
            try:
                current_code_int = int(current_fault_text.split("(")[1].split(")")[0])
            except:
                pass
        elif current_fault_text in fault_text_to_code:
            current_code_int = fault_text_to_code[current_fault_text]

        # Find next code in test list
        try:
            current_index = fault_codes_to_test.index(current_code_int)
        except ValueError:
            current_index = -1
        next_index = (current_index + 1) % len(fault_codes_to_test)
        next_fault_code = fault_codes_to_test[next_index]

        # Update mock data and call update_labels
        mock_data_instance = MockFaultData()
        mock_data_instance.mc_fault_code = next_fault_code
        self.log_to_console(
            f"Testing Fault Display: {self.print_fault_code(next_fault_code)}"
        )
        self.update_labels(mock_data_instance)

    def log_to_console(self, message, error=False):
        """Appends a timestamped message to the console textbox safely."""
        textbox_widget = getattr(self, "textbox", None)
        if not textbox_widget:
            return  # Exit if textbox doesn't exist
        try:
            timestamp = time.strftime("%H:%M:%S")
            formatted_message = f"[{timestamp}] {message}\n"
            tag = ("error",) if error else ("normal",)  # Tag must be a sequence

            # Ensure widget is usable before interaction
            if textbox_widget.winfo_exists():
                textbox_widget.configure(state="normal")  # Enable writing
                textbox_widget.insert(tkinter.END, formatted_message, tag)
                if error:
                    # Ensure tag exists before configuring
                    if "error" not in textbox_widget.tag_names():
                        textbox_widget.tag_add("error", "1.0", "end")  # Add if missing
                    textbox_widget.tag_config("error", foreground="red")
                textbox_widget.see(tkinter.END)  # Scroll to bottom
                textbox_widget.configure(state="disabled")  # Disable writing again
        except Exception as e:
            # Fallback print if logging fails (e.g., during shutdown)
            print(f"Console log error: {e}")

    def process_queue(self):
        """Processes messages from data and error queues."""
        try:
            # Process Data Queue
            while not self.data_queue.empty():
                values = self.data_queue.get_nowait()
                # Only process if connection is still valid
                if self.serial_connection and self.serial_connection.is_open:
                    self.update_labels(values)
                    # Store data for plotting (always store if connected, plotting flag controls display)
                    if hasattr(values, "timestamp"):
                        if self.plot_start_time is None:
                            self.plot_start_time = values.timestamp
                        elapsed_time = values.timestamp - self.plot_start_time
                        # Ensure attributes exist before appending
                        duty = getattr(values, "duty_cycle_now", 0.0)
                        current = getattr(values, "avg_motor_current", 0.0)
                        self.time_data.append(elapsed_time)
                        self.duty_data.append(duty)
                        self.current_data.append(current)

            # Process Error Queue
            while not self.error_queue.empty():
                error_msg = self.error_queue.get_nowait()
                self.log_to_console(error_msg, error=True)
                # Update UI state if error indicates disconnection
                if "Disconnected" in error_msg or "Serial Error" in error_msg:
                    # Check if connection is really closed before updating UI
                    if not (self.serial_connection and self.serial_connection.is_open):
                        self._update_ui_connection_state(connected=False)

        except queue.Empty:
            pass  # Normal case, no messages
        except Exception as e:
            # Catch potential errors during queue processing itself
            print(f"Queue processing error: {e}")
        finally:
            # Always reschedule the next check
            self.after(50, self.process_queue)  # Check queue every 50ms

    # --- End State Update and Data Display ---

    # --- Plotting Methods ---
    def _setup_plot_axes(self):
        """Configures the plot axes appearance, labels, lines, and legend."""
        # Ensure figure and axes objects exist
        if not self.plot_figure or not self.ax_duty or not self.ax_current:
            return

        # Clear previous settings
        self.ax_duty.clear()
        self.ax_current.clear()

        # Get current theme colors from rcParams
        p = plt.rcParams
        bg_color = p["figure.facecolor"]
        fg_color = p["text.color"]
        grid_color = p["grid.color"]
        blue_color = p["axes.prop_cycle"].by_key()["color"][0]
        red_color = p["axes.prop_cycle"].by_key()["color"][1]

        # Configure Duty Cycle Axis (Left Y-axis)
        self.ax_duty.set_xlabel("Time (s)", color=fg_color)
        self.ax_duty.set_ylabel("Duty Cycle", color=blue_color)
        self.ax_duty.set_ylim(-0.05, 1.05)  # Assuming 0-1 duty range + padding
        self.ax_duty.tick_params(
            axis="y", colors=blue_color
        )  # Tick label color matches line
        self.ax_duty.tick_params(axis="x", colors=fg_color)  # X tick label color
        self.ax_duty.grid(True, linestyle="--", alpha=0.6, color=grid_color)
        self.ax_duty.set_facecolor(bg_color)
        # Set spine colors to match grid for less contrast if desired
        for spine in self.ax_duty.spines.values():
            spine.set_color(grid_color)

        # Configure Motor Current Axis (Right Y-axis)
        self.ax_current.set_ylabel("Motor Current (A)", color=red_color)
        self.ax_current.tick_params(
            axis="y", colors=red_color
        )  # Tick label color matches line
        self.ax_current.set_ylim(-1, 1)  # Initial limits, will be updated dynamically
        self.ax_current.set_facecolor("none")  # Make background transparent
        # Match spine colors
        for spine in self.ax_current.spines.values():
            spine.set_color(grid_color)

        # Create plot lines (must be done *after* axes are configured)
        (self.line_duty,) = self.ax_duty.plot(
            [], [], lw=1.5, color=blue_color, label="Duty"
        )
        (self.line_current,) = self.ax_current.plot(
            [], [], lw=1.5, color=red_color, label="Current"
        )

        # Create legend (after lines are created)
        lines, labels = self.ax_duty.get_legend_handles_labels()
        lines2, labels2 = self.ax_current.get_legend_handles_labels()
        try:
            # Attach legend preferably to one axis (e.g., ax_duty)
            legend = self.ax_duty.legend(
                lines + lines2, labels + labels2, loc="upper left"
            )
            if legend:  # Check if legend object was created
                legend.get_frame().set_facecolor(bg_color)
                legend.get_frame().set_edgecolor(grid_color)
                for text in legend.get_texts():
                    text.set_color(fg_color)
        except Exception as e:
            print(f"Error creating plot legend: {e}")  # Log if legend fails

        # Adjust layout to prevent labels overlapping
        try:
            self.plot_figure.tight_layout()
        except Exception:
            # tight_layout can sometimes fail, especially if window is small
            print("Warning: plot_figure.tight_layout() failed.")
            pass

    def _reset_plot(self):
        """Clears plot data, resets start time, and redraws empty plot."""
        self.time_data.clear()
        self.duty_data.clear()
        self.current_data.clear()
        self.plot_start_time = None

        # Safely reset plot lines and axes limits if they exist
        line_duty = getattr(self, "line_duty", None)
        line_current = getattr(self, "line_current", None)
        ax_duty = getattr(self, "ax_duty", None)
        ax_current = getattr(self, "ax_current", None)
        plot_canvas = getattr(self, "plot_canvas", None)

        if line_duty:
            line_duty.set_data([], [])
        if line_current:
            line_current.set_data([], [])
        if ax_duty:
            ax_duty.set_xlim(0, self.plot_time_window)
        if ax_current:
            ax_current.set_ylim(-1, 1)  # Reset current Y limits

        # Redraw the canvas safely
        if (
            plot_canvas and plot_canvas.get_tk_widget().winfo_exists()
        ):  # Check widget exists
            try:
                plot_canvas.draw_idle()
            except Exception as e:
                print(f"Error drawing idle on plot reset: {e}")
        print("Plot data cleared.")

    def _trigger_plot_update(self):
        """Periodically calls the visual plot update function if plotting is enabled."""
        # Check plotting flag and necessary plot elements
        plot_canvas = getattr(self, "plot_canvas", None)
        widget_exists = plot_canvas and plot_canvas.get_tk_widget().winfo_exists()

        if self.is_plotting and widget_exists and len(self.time_data) > 0:
            try:
                self._update_plot_visuals()
            except Exception as e:
                print(f"Error updating plot visuals: {e}")
                self.log_to_console(f"Plot Update Error: {e}", error=True)
                self.is_plotting = False  # Stop plotting on error
                self._update_plot_button_states()  # Update button states

        # Always reschedule the next trigger check
        # Use try-except in case self (the app) is destroyed before this runs
        try:
            self.after(self.plot_update_interval, self._trigger_plot_update)
        except Exception:
            pass  # Ignore if app is destroyed

    def _update_plot_visuals(self):
        """Updates the plot lines, axes limits, and redraws the canvas."""
        # Ensure required elements exist and plotting is active
        if (
            not self.is_plotting
            or not hasattr(self, "plot_canvas")
            or not self.plot_canvas
            or not hasattr(self, "line_duty")
            or not self.line_duty
        ):
            return

        # Convert deque to list for plotting
        time_list = list(self.time_data)
        duty_list = list(self.duty_data)
        current_list = list(self.current_data)

        # Exit if no time data (e.g., just after reset)
        if not time_list:
            return

        # Update line data
        self.line_duty.set_data(time_list, duty_list)
        self.line_current.set_data(time_list, current_list)

        # Update X axis limits (sliding window)
        current_max_time = time_list[-1]
        xmin = max(0, current_max_time - self.plot_time_window)
        xmax = xmin + self.plot_time_window
        self.ax_duty.set_xlim(xmin, xmax)

        # Update adaptive Y limits for current axis
        if current_list:  # Avoid errors if current_list is empty
            min_curr, max_curr = min(current_list), max(current_list)
            # Calculate padding, ensure minimum padding if range is zero or very small
            padding = max((max_curr - min_curr) * 0.1, 0.5)
            curr_ymin = min_curr - padding
            curr_ymax = max_curr + padding
            current_ylim = self.ax_current.get_ylim()
        else:  # Default limits if no current data
            curr_ymin, curr_ymax = -1, 1
            current_ylim = (-1, 1)  # Assume default to force update

        # Update Y limits only if they change significantly or are default
        if (
            isinstance(current_ylim, (list, tuple)) and len(current_ylim) == 2
        ):  # Ensure ylim is valid
            if (
                abs(current_ylim[0] - curr_ymin) > 0.1
                or abs(current_ylim[1] - curr_ymax) > 0.1
                or current_ylim == (-1, 1)
            ):
                self.ax_current.set_ylim(curr_ymin, curr_ymax)
        else:  # If ylim was invalid, set it
            self.ax_current.set_ylim(curr_ymin, curr_ymax)

        # Redraw canvas safely
        if self.plot_canvas.get_tk_widget().winfo_exists():
            try:
                self.plot_canvas.draw_idle()
            except Exception as e:
                print(f"Canvas draw_idle error: {e}")

    def _update_plot_theme_params(self):
        """Sets the global matplotlib rcParams based on current CTk theme."""
        is_dark = customtkinter.get_appearance_mode() == "Dark"
        # Define theme colors
        bg_color = "#2B2B2B" if is_dark else "#F0F0F0"  # Match CTk background closer
        fg_color = "lightgrey" if is_dark else "#333333"  # Text/tick color
        grid_color = "#555555" if is_dark else "#D0D0D0"  # Grid line color
        # Get default CTk blue/red or choose custom ones
        blue_color = "#5699E9"  # CTk blue
        red_color = "#D95252"  # Softer red

        plt.rcParams.update(
            {
                "axes.facecolor": bg_color,
                "axes.edgecolor": grid_color,
                "axes.labelcolor": fg_color,
                "xtick.color": fg_color,
                "ytick.color": fg_color,
                "grid.color": grid_color,
                "figure.facecolor": bg_color,
                "text.color": fg_color,
                "legend.facecolor": bg_color,
                "legend.edgecolor": grid_color,
                "axes.prop_cycle": plt.cycler(
                    color=[blue_color, red_color]
                ),  # Set line colors
            }
        )

    def _update_plot_theme(self):
        """Applies the current theme to the plot."""
        if not self.plot_figure:
            return  # Exit if plot not ready
        self._update_plot_theme_params()  # Update global parameters
        self._setup_plot_axes()  # Re-run axes setup to apply parameters
        # Redraw canvas safely
        if (
            hasattr(self, "plot_canvas")
            and self.plot_canvas
            and self.plot_canvas.get_tk_widget().winfo_exists()
        ):
            try:
                self.plot_canvas.draw_idle()
            except Exception as e:
                print(f"Error redrawing canvas on theme update: {e}")

    # --- New Plot Button Methods ---
    def _start_plotting_event(self):
        """Handles the 'Start Plotting' button click."""
        self.log_to_console("Starting plot...")
        self._reset_plot()  # Clear previous data for a fresh start
        self.is_plotting = True
        self._update_plot_button_states()  # Update button enabled/disabled

    def _stop_plotting_event(self):
        """Handles the 'Stop Plotting' button click."""
        self.log_to_console("Stopping plot.")
        self.is_plotting = False
        self._update_plot_button_states()  # Update button enabled/disabled

    def _update_plot_button_states(self):
        """Updates the enabled/disabled state of plot Start/Stop buttons."""
        # Check if buttons exist first
        start_button = getattr(self, "plot_start_button", None)
        stop_button = getattr(self, "plot_stop_button", None)
        if not start_button or not stop_button:
            return

        # Determine states based on connection and plotting flag
        connected = bool(self.serial_connection and self.serial_connection.is_open)
        start_state = "normal" if connected and not self.is_plotting else "disabled"
        stop_state = "normal" if connected and self.is_plotting else "disabled"

        # Configure buttons safely
        start_button.configure(state=start_state)
        stop_button.configure(state=stop_state)

    # --- End Plotting Methods ---

    # --- Application Shutdown ---
    def on_closing(self):
        """Handles window close event gracefully."""
        print("Closing application...")
        # Log might fail if textbox already destroying
        # self.log_to_console("Shutdown initiated.")

        # 1. Stop activities that might access GUI/Tkinter later
        self.is_plotting = False

        # 2. Cancel all pending 'after' jobs FIRST
        print("Cancelling scheduled tasks...")
        try:
            after_ids = self.tk.call("after", "info").split()
            for after_id in after_ids:
                try:
                    # Check if ID still valid before cancelling
                    self.tk.call("after", "info", after_id)
                    self.after_cancel(after_id)
                except tkinter.TclError:
                    pass  # Ignore if ID already gone
                except Exception as e_cancel_check:
                    print(f"Ignoring cancel check error: {e_cancel_check}")
        except Exception as e_get_ids:
            print(f"Ignoring 'after' cancel errors: {e_get_ids}")

        # 3. Stop the background thread
        if self.data_reader and self.data_reader.is_alive():
            print("Signalling DataReader thread to stop...")
            self.data_reader.stop()

        # 4. Send final stop command (optional, best effort)
        ser = self.serial_connection  # Use local ref
        if ser and ser.is_open:
            print("Sending final stop command...")
            try:
                self.stop_button_event(log=False)
                time.sleep(0.1)  # Brief pause for command tx
            except Exception as e:
                print(f"Ignoring stop command error: {e}")

        # 5. Close serial port
        print("Closing serial port...")
        self.serial_connection = None  # Clear app reference
        if ser:
            read.close_serial_port(ser)  # Close using local ref

        # 6. Wait for the background thread to finish
        if self.data_reader and self.data_reader.is_alive():
            print("Waiting for DataReader thread to join...")
            self.data_reader.join(timeout=1.0)  # Wait up to 1 sec
            if self.data_reader.is_alive():
                print("Warning: DataReader thread did not stop gracefully.")

        # 7. Close plot figure
        if self.plot_figure:
            print("Closing plot figure...")
            try:
                plt.close(self.plot_figure)
            except Exception as e_plt:
                print(f"Ignoring plot close error: {e_plt}")

        # 8. Destroy the main window (LAST step)
        print("Destroying GUI...")
        try:
            self.destroy()
        except Exception as e_destroy:
            print(f"Error during final destroy: {e_destroy}")

    # --- End Application Shutdown ---


if __name__ == "__main__":
    app = App()
    app.mainloop()

# --- END OF FILE realtime_gui.py ---

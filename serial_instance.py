# serial_instance.py
import serial

ser = None


def open_serial(port):
    global ser
    if ser is None:
        ser = serial.Serial(port, baudrate=115200, timeout=0.05)
        # print(f"Opened serial port {ser.port}")
    return ser


def get_serial():
    return ser


def close_serial():
    ser.close()

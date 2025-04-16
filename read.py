# --- START OF FILE read.py ---

import pyvesc
import time
from pyvesc.VESC.messages import GetVersion
from pyvesc.VESC.messages import GetValues
from pyvesc.VESC.messages.getters import GetMcConfRequest, GetAppConfRequest
from pyvesc.VESC.messages.parser import (
    parse_mc_conf_serialized,
    parse_app_conf_serialized,
)
from pyvesc.protocol.interface import encode_request  # 요청 인코더
from pyvesc.protocol.packet.codec import unframe  # 응답 디코더

# from serial_instance import open_serial, get_serial, close_serial # No longer needed
import serial  # Import serial directly if needed for exceptions

from pyvesc import VESC
from pyvesc.VESC.messages import (
    GetVersion,
    GetValues,
    SetRPM,
    SetCurrent,
    SetDutyCycle,  # Added for duty cycle control
    SetRotorPositionMode,
    GetRotorPosition,
)

TIMEOUT = 1.0  # 기본 타임아웃 값 (초)

# Global status flag - might be better managed within App class state
serial_status = False


# Function now expects an *open* serial object
def get_realtime_data(ser):
    """
    Requests and decodes VESC telemetry data using an existing serial connection.

    Args:
        ser: An open pyserial Serial object connected to the VESC.

    Returns:
        A VESC message object (e.g., GetValues) containing the data, or None if an error occurs.
    """
    global serial_status
    if ser is None or not ser.is_open:
        serial_status = False
        return None

    try:
        # Send request
        ser.write(pyvesc.encode_request(GetValues))

        # Try to read the response - adjust buffer size if needed, 128 is often enough for GetValues
        # A more robust method might involve checking ser.in_waiting and reading in chunks,
        # but pyvesc.decode expects a buffer it can consume from.
        buffer = ser.read(128)

        if buffer:
            (response, consumed) = pyvesc.decode(buffer)
            if consumed > 0:  # Check if decode was successful
                serial_status = True
                # print(response) # Optional: for debugging
                return response
            else:
                # Decode failed or returned no meaningful data
                print("Warning: VESC decode consumed 0 bytes.")
                serial_status = False
                return None
        else:
            # Read timed out or returned nothing
            # print("Warning: VESC read returned no data.") # Can be noisy
            serial_status = False
            return None

    except serial.SerialException as e:
        print(f"Serial Error during read: {e}")
        serial_status = False
        close_serial_port(ser)  # Attempt to close the problematic port
        return None
    except Exception as e:
        # Catch other potential errors (e.g., pyvesc decode issues, attribute errors)
        print(f"Error processing VESC data: {e}")
        serial_status = False
        return None


# Function to send a command - expects an open serial object
def send_command(ser, command):
    """
    Encodes and sends a VESC command using an existing serial connection.

    Args:
        ser: An open pyserial Serial object connected to the VESC.
        command: A pyvesc message object (e.g., SetCurrent(10)).

    Returns:
        True if the command was sent successfully, False otherwise.
    """
    if ser is None or not ser.is_open:
        print("Error: Serial port not open for sending command.")
        return False
    try:
        ser.write(pyvesc.encode(command))
        return True
    except serial.SerialException as e:
        print(f"Serial Error during write: {e}")
        close_serial_port(ser)  # Attempt to close
        return False
    except Exception as e:
        print(f"Error sending VESC command: {e}")
        return False


# Explicit function to close the serial port
def close_serial_port(ser):
    """Closes the serial port if it's open."""
    global serial_status
    if ser and ser.is_open:
        try:
            ser.close()
            print(f"Serial port {ser.port} closed.")
        except Exception as e:
            print(f"Error closing serial port {ser.port}: {e}")
    serial_status = False  # Update status regardless


# Kept for potential direct script use, but GUI should manage connection
# if __name__ == "__main__":
#    port = "/dev/cu.usbmodem3041" # Or your actual port
#    my_ser = None
#    try:
#        my_ser = serial.Serial(port, baudrate=115200, timeout=0.05)
#        print(f"Connected to {port}")
#        start_time = time.time()
#        while time.time() - start_time < 5: # Run for 5 seconds
#             data = get_realtime_data(my_ser)
#             if data:
#                 print(f"RPM: {data.rpm}, V_in: {data.v_in}, Fault: {data.mc_fault_code}")
#             else:
#                 print("No data received.")
#                 # break # Optional: Stop if data fails
#             time.sleep(0.1)
#        # Example command: Stop the motor
#        send_command(my_ser, SetCurrent(0))
#        print("Sent stop command.")
#
#    except serial.SerialException as e:
#        print(f"Failed to open serial port {port}: {e}")
#    finally:
#        close_serial_port(my_ser)
# --- END OF FILE read.py ---


def get_mc_configuration(ser):
    """MCCONF 설정을 요청하고 파싱하여 반환"""
    print("  (read.py) GET_MCCONF 요청...")
    clear_input_buffer(ser)  # clear_input_buffer 헬퍼가 필요하거나 여기서 직접 구현
    request = encode_request(GetMcConfRequest)  # <<--- GetMcConfRequest 클래스 사용
    try:
        ser.write(request)
        time.sleep(TIMEOUT)  # TIMEOUT 상수 필요
        response = ser.read(4096)
        if response:
            payload, consumed = unframe(response)
            if payload and payload[0] == GetMcConfRequest.id:
                parsed_conf = parse_mc_conf_serialized(
                    payload[1:]
                )  # <<--- MCCONF 파서 사용
                if parsed_conf and "MCCONF_SIGNATURE" in parsed_conf:
                    return parsed_conf
            # ... (오류 처리) ...
        return None
    except Exception as e:
        print(f"  (read.py) GET_MCCONF 처리 오류: {e}")
        return None


def get_app_configuration(ser):
    """APPCONF 설정을 요청하고 파싱하여 반환"""
    print("  (read.py) GET_APPCONF 요청...")
    clear_input_buffer(ser)
    request = encode_request(GetAppConfRequest)  # <<--- GetAppConfRequest 클래스 사용
    try:
        ser.write(request)
        time.sleep(TIMEOUT)
        response = ser.read(4096)  # APPCONF 크기에 맞게 조정 가능
        if response:
            payload, consumed = unframe(response)
            if payload and payload[0] == GetAppConfRequest.id:
                parsed_conf = parse_app_conf_serialized(
                    payload[1:]
                )  # <<--- APPCONF 파서 사용
                if parsed_conf and "APPCONF_SIGNATURE" in parsed_conf:
                    return parsed_conf
            # ... (오류 처리) ...
        return None
    except Exception as e:
        print(f"  (read.py) GET_APPCONF 처리 오류: {e}")
        return None


def clear_input_buffer(ser, wait_time=0.1):
    # ... (함수 코드) ...
    if ser and ser.is_open:
        ser.reset_input_buffer()
        time.sleep(wait_time)
        if ser.in_waiting > 0:
            try:
                ser.read(ser.in_waiting)
            except Exception as e:
                print(f"  버퍼 비우기 중 오류: {e}")

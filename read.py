import pyvesc
import time
import serial
import struct
import pprint

# !!! 필요한 모듈/클래스/함수 import 확인 및 수정 !!!
try:
    from pyvesc.VESC.messages import GetValues
    from pyvesc.VESC.messages.getters import GetMcConfRequest, GetAppConfRequest
    from pyvesc.VESC.messages.vesc_protocol_utils import (
        parse_mc_conf_serialized,
        parse_app_conf_serialized,
    )
    from pyvesc.protocol.interface import encode_request, encode
    from pyvesc.protocol.packet.codec import unframe
    from pyvesc import decode

except ImportError as e:
    print(f"오류(read.py): 필요한 pyvesc 컴포넌트 import 실패 ({e}).")
    raise

# --- 타임아웃 값 조정 ---
TIMEOUT = 0.05  # DataReader sleep time
# 설정 읽기 타임아웃을 약간 더 늘림 (VESC 응답 시간 고려)
CONFIG_READ_TIMEOUT = 2.5  # seconds


# --- get_realtime_data: SerialException 다시 발생시키도록 유지 ---
def get_realtime_data(ser):
    if ser is None or not ser.is_open:
        return None
    try:
        request = encode_request(GetValues)
        ser.write(request)
        buffer = ser.read(128)  # Read based on ser.timeout
        if buffer:
            try:
                (response_object, consumed) = decode(buffer)
                if consumed > 0 and isinstance(response_object, GetValues):
                    return response_object
            except Exception:
                pass  # Ignore decode errors silently
        return None
    except serial.SerialException as e:
        # DataReader가 SerialException을 잡고 처리하도록 다시 발생시킴
        print(f"Debug(read): Raising SerialException in get_realtime_data: {e}")
        raise e
    except Exception as e:
        print(f"Error(read): Unexpected error in get_realtime_data: {e}")
        return None


# --- send_command: 변경 없음 ---
def send_command(ser, command):
    if ser is None or not ser.is_open:
        return False
    try:
        ser.write(encode(command))
        return True
    except serial.SerialException as e:
        print(f"Serial Error writing {type(command).__name__}: {e}")
        return False
    except Exception as e:
        print(f"Error sending {type(command).__name__}: {e}")
        return False


# --- close_serial_port: 변경 없음 ---
def close_serial_port(ser):
    if ser and ser.is_open:
        port_name = ser.port
        try:
            ser.close()
            print(f"Info(read): Serial port {port_name} closed.")
        except Exception as e:
            print(f"Error closing port {port_name}: {e}")


# --- clear_input_buffer 강화 ---
def clear_input_buffer(ser, wait_time=0.05):
    """입력 버퍼를 더 확실하게 비웁니다."""
    if ser and ser.is_open:
        try:
            # print(f"Debug(read): Clearing input buffer ({ser.in_waiting} bytes initially)...")
            ser.reset_input_buffer()
            time.sleep(wait_time)  # 짧은 대기 후
            remaining = ser.read(ser.in_waiting)  # 혹시 그 사이에 들어온 데이터 읽기
            # if remaining: print(f"Debug(read): Cleared {len(remaining)} more bytes.")
        except Exception as e:
            print(f"Error(read): Error clearing input buffer: {e}")


# --- 설정 읽기 함수 로직 개선 ---
def _read_config_response(ser, request_message_class, parser_func, timeout):
    """설정 응답을 읽고 파싱하는 내부 헬퍼 함수 (루프 및 ID 확인 포함)."""
    request = encode_request(request_message_class)
    request_id = request_message_class.id
    print(
        f"Info(read): Requesting {request_message_class.__name__} (ID: {request_id})..."
    )
    clear_input_buffer(ser)  # 요청 전 버퍼 비우기

    try:
        ser.write(request)
        start_time = time.monotonic()
        buffer = b""  # 수신 버퍼 초기화

        while time.monotonic() - start_time < timeout:
            # 새 데이터 읽기 (Non-blocking하게 또는 짧은 타임아웃으로)
            bytes_to_read = ser.in_waiting
            if bytes_to_read > 0:
                buffer += ser.read(bytes_to_read)

            # 버퍼에서 원하는 패킷 ID를 찾아 파싱 시도
            while True:  # 버퍼 안에 여러 패킷이 있을 수 있음
                try:
                    # unframe은 첫 번째 유효 프레임을 찾거나 예외 발생
                    payload, consumed = unframe(buffer)
                    if payload:
                        # print(f"Debug(read): Unframed packet. ID: {payload[0]}, Len: {len(payload)}, Consumed: {consumed}")
                        if payload[0] == request_id:
                            # 원하는 패킷 발견! 파싱 시도
                            parsed_conf = parser_func(payload[1:])
                            if parsed_conf and isinstance(parsed_conf, dict):
                                # Signature 존재 여부는 파서가 결정하거나 여기서 추가 확인 가능
                                print(
                                    f"Info(read): {request_message_class.__name__} parsed successfully."
                                )
                                return parsed_conf
                            else:
                                print(
                                    f"Error(read): {request_message_class.__name__} parsing failed after unframe."
                                )
                                buffer = buffer[
                                    consumed:
                                ]  # 파싱 실패해도 해당 부분은 버퍼에서 제거
                                continue  # 다음 패킷 시도
                        else:
                            # ID가 다른 유효한 패킷 -> 무시하고 버퍼에서 제거
                            # print(f"Debug(read): Ignoring packet with ID {payload[0]}.")
                            buffer = buffer[consumed:]
                            continue  # 다음 패킷 시도
                    else:
                        # 유효한 패킷 없음 (버퍼에 불완전한 데이터만 남음)
                        break  # 내부 while 루프 탈출, 외부 while에서 더 읽기

                except NeedMoreData:
                    # 현재 버퍼로는 패킷 완성 불가 -> 더 읽어야 함
                    # print("Debug(read): Need more data...")
                    break  # 내부 while 루프 탈출, 외부 while에서 더 읽기
                except ValueError:
                    # CRC 실패 등 -> 해당 패킷은 손상됨, 버퍼 시작 부분 제거 시도?
                    # print("Debug(read): ValueError during unframe (CRC? discard byte?)")
                    if buffer:
                        buffer = buffer[
                            1:
                        ]  # 손상 가능성 있는 첫 바이트 제거하고 다시 시도
                    else:
                        break  # 버퍼 비었으면 탈출
                # except Exception as e_unframe:
                # print(f"Debug(read): Unexpected unframe error: {e_unframe}")
                # if buffer: buffer = buffer[1:] # 알수없는 오류시에도 첫 바이트 제거
                # else: break

            # 다음 읽기 시도 전 짧은 대기 (CPU 사용 방지)
            time.sleep(0.02)

        # Timeout 도달
        print(
            f"Error(read): Timeout waiting for {request_message_class.__name__} response."
        )
        print(f"Debug(read): Final buffer on timeout: {buffer!r}")
        return None

    except serial.SerialException as e:
        print(f"Serial Error during {request_message_class.__name__} read/write: {e}")
        raise e  # Worker 스레드가 잡도록 예외 발생
    except Exception as e:
        print(f"Error processing {request_message_class.__name__}: {e}")
        traceback.print_exc()
        return None


def get_mc_configuration(ser):
    """VESC에서 MCCONF를 읽어옵니다 (내부 헬퍼 함수 사용)."""
    # Signature 확인은 파서가 하거나 여기서 추가 가능 (예: 'MCCONF_SIGNATURE' in result)
    return _read_config_response(
        ser, GetMcConfRequest, parse_mc_conf_serialized, CONFIG_READ_TIMEOUT
    )


def get_app_configuration(ser):
    """VESC에서 APPCONF를 읽어옵니다 (내부 헬퍼 함수 사용)."""
    # Signature 확인은 파서가 하거나 여기서 추가 가능 (예: 'APPCONF_SIGNATURE' in result)
    return _read_config_response(
        ser, GetAppConfRequest, parse_app_conf_serialized, CONFIG_READ_TIMEOUT
    )

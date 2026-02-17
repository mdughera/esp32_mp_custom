import machine
import struct
import asyncio
import json
from time_utils import TimeUtils
from daikin_defs import altherma3HTAll
import time

class Daikin:
    # Define the labels you want to keep
    filterLabels = [
        "Operation Mode",
        "Outdoor air temp. R1T",
        "INV primary current",
        "INV secondary current",
        "Compressor outlet temperature",
        "IU operation mode",
        "DHW setpoint",
        "LW setpoint main",
        "SmartGridContact2",
        "SmartGridContact1",
        "Water pump operation",
        "Leaving water temp. before BUH R1T",
        "Leaving water temp. after BUH R2T",
        "DHW tank temp. R5T",
        "Reheat",
        "Storage ECO",
        "Storage comfort",
        "Powerful DHW Operation",
        "Space heating Operation",
        "Flow sensor",
        "Water heat exchanger inlet temp.",
        "Water heat exchanger outlet temp."
    ]    
    
    status = {}
    _uart = None
    # define response lengths to stop waiting for UART when message has been completely received
    _response_lengths = { 0x00: 14, 0x10: 20, 0x11: 10, 0x20: 21, 0x21: 20, 0x30: 19, 0x60: 21, 0x61: 20, 0x62: 21, 0x63: 21, 0x64: 20, 0x65: 21, 0xA0: 20, 0xA1: 20 }
    _MAX_BUFFER_LENGTH = 21 # maximum value of _response_lengths 
    _register_cache = {}
    _lock = asyncio.Lock()
    _labelDefs = []
    _current_day = None # use it to reset day counters
    _buffer = bytearray(_MAX_BUFFER_LENGTH) # used by irq, preallocated buffer
    _buffer_pos = 0 # used by irq, first buffer position to be written
    _data_ready = asyncio.ThreadSafeFlag() # used by irq and send_message
    
    @classmethod
    def init(cls, tx=1, rx=2, filter=True): # filter=False all items in the definition are retrieved and calculated
        cls._uart = machine.UART(1, baudrate=9600, bits=8, parity=0, stop=1, tx=tx, rx=rx)  # Use UART1, TX=1, RX=2

        # Build the filtered list of values to be read
        cls._labelDefs = []
        for item in altherma3HTAll:
            if item[5] in cls.filterLabels or not filter:
                cls._labelDefs.append(item)
        print("Daikin initialized")

    @classmethod   
    def _convert_case_114(cls, data):
        if len(data) != 2:
            raise ValueError("Input must be two bytes.")

        # Special case check for "not available"
        if data[0] == 0 and data[1] == 128:
            return "---"

        # Reconstruct 16-bit value from two bytes (little endian)
        num2 = (data[1] << 8) | data[0]

        # Check if negative (sign bit is set)
        is_negative = data[1] & 0x80
        if is_negative:
            num2 = ~((num2 ^ 0xFFFF) + 1) & 0xFFFF  # Two's complement to positive

        # Extract the upper and lower byte parts
        int_part = (num2 & 0xFF00) >> 8
        frac_part = (num2 & 0x00FF) / 256.0

        dbl_data = (int_part + frac_part) * 10.0

        if is_negative:
            dbl_data *= -1.0

        return dbl_data
   
    @classmethod
    def _convert_case_119(cls, data):
        if data[0] == 0 and data[1] == 128:
            return "---"
        
        # Unpack 2 bytes as unsigned short (little-endian)
        num3 = struct.unpack('<H', data[0:2])[0]  # Little endian: low byte first

        # Mask to remove the highest bit from data[0]
        num3 = ((data[1] << 8) | (data[0] & 0x7F))

        integer_part = (num3 & 0xFF00) >> 8  # top 8 bits
        fractional_part = (num3 & 0x00FF) / 256.0  # bottom 8 bits divided by 256

        dbl_data = integer_part + fractional_part
        return dbl_data
    
    _values_203 = { 0: "Normal", 1: "Error", 2: "Warning", 3: "Caution" }
    
    @classmethod
    def _convert_table_204(cls, data_byte):
        array = " ACEHFJLPU987654"
        array2 = "0123456789AHCJEF"

        num = (data_byte >> 4) & 0x0F
        num2 = data_byte & 0x0F

        return array[num] + array2[num2]

    # in espaltherma 0: "Fan only"
    _values_217 = { 0: "OFF", 1: "Heating", 2: "Cooling", 3: "Auto", 4: "Ventilation", 5: "Auto Cool", 6: "Auto Heat", 7: "Dry", 8: "Aux.", 9: "Cooling Storage", 10: "Heating Storage" }

    @classmethod
    def _convert_table_300(cls, byte_value: int, table_id: int) -> str:
        """Checks if the bit at (table_id % 10) is set in a byte value."""
        bitmask = 1 << (table_id % 10)
        return "ON" if byte_value & bitmask else "OFF"

    @classmethod
    def _convert_table_315(cls, data_byte):
        # Mask the high nibble and shift right
        b = (data_byte & 0xF0) >> 4

        # Mapping like a switch-case
        mode_map = {
            0: "Stop",
            1: "Heating",
            2: "Cooling",
            3: "??",
            4: "DHW",
            5: "Heating + DHW",
            6: "Cooling + DHW",
        }

        # Return the corresponding string or "-" if not found
        return mode_map.get(b, "-")   
   
    @classmethod
    def _convert_press_to_temp(cls, data):
        """
        Converts pressure to temperature for R32 refrigerant using a 6th-degree polynomial.
        Parameters:
            data (float): Pressure value.
        Returns:
            float: Corresponding temperature.
        """
        num = -2.6989493795556e-07 * data**6
        num2 = 4.26383417104661e-05 * data**5
        num3 = -0.00262978346547749 * data**4
        num4 = 0.0805858127503585 * data**3
        num5 = -1.31924457284073 * data**2
        num6 = 13.4157368435437 * data
        num7 = -51.1813342993155
        return num + num2 + num3 + num4 + num5 + num6 + num7
   
    @classmethod
    def parse_value(cls, register, config):
        addr, offset, conversion_id, size, data_type, label = config
        offset += 3 # first 3 chars in register are request values
        if conversion_id == 105 and size == 2: 
            value = (struct.unpack('<h', register[offset:offset+size])[0]) / 10 # '<h' = little-endian 16-bit signed
        elif conversion_id == 105 and size == 1:
            value = (struct.unpack('<b', register[offset:offset+1])[0]) / 10  # 1-byte signed
        elif conversion_id == 114 and size == 2:
            value = cls._convert_case_114(register[offset:offset+size])
        elif conversion_id == 118 and size == 2: 
            value = (struct.unpack('<h', register[offset:offset+size])[0]) / 100 # '<h' = little-endian 16-bit signed
        elif conversion_id == 119 and size == 2:
            value = cls._convert_case_119(register[offset:offset+size])
        elif conversion_id == 152 and size == 2: 
            value = (struct.unpack('<H', register[offset:offset+size])[0]) / 10 # '<h' = little-endian 16-bit unsigned
        elif conversion_id == 152 and size == 1:
            value = register[offset] # 1 byte unsigned
        elif conversion_id == 161 and size == 1:
            value = register[offset] * 0.5 # 1 byte unsigned
        elif conversion_id == 203:
            value = cls._values_203.get(register[offset], "Not_mapped")
        elif conversion_id == 204:
            value = cls._convert_table_204(register[offset])
        elif conversion_id == 211 and size == 1: # espaltherma sets to OFF when zero. Why?
            value = (struct.unpack('<b', register[offset:offset+1])[0]) / 10  # 1-byte signed
        elif conversion_id == 217:
            value = cls._values_217.get(register[offset], "Not_mapped")
        elif conversion_id in [300, 301, 302, 303, 304, 305, 306, 307]:
            value = cls._convert_table_300(register[offset], conversion_id)            
        elif conversion_id == 315:
            value = cls._convert_table_315(register[offset])
        elif conversion_id == 405 and size == 2:
            value = cls._convert_press_to_temp((struct.unpack('<h', register[offset:offset+size])[0]) / 10) # '<h' = little-endian 16-bit signed
        else:
            value = " ".join(f"0x{b:02X}" for b in register[offset:offset+size]) + " not decoded"
            
        return value

    @classmethod
    def uart_irq_handler(cls, _):
        data = cls._uart.read()
        if data:
            for b in data:
                if cls._buffer_pos < cls._MAX_BUFFER_LENGTH:
                    cls._buffer[cls._buffer_pos] = b
                    cls._buffer_pos += 1
            cls._data_ready.set()


    @classmethod
    async def send_message(cls, payload, response_length=None):
        checksum = (~sum(payload)) & 0xFF
        message = bytes(payload + [checksum])
        
        cls._buffer_pos = 0
        cls._data_ready.clear()
        cls._uart.write(message)

        for _ in range(3):
            try:
                await asyncio.wait_for(cls._data_ready.wait(), timeout=0.3)
                await asyncio.sleep(0.03) # wait for irq to complete filling the buffer (enough for 20 bytes at 9600 baud)
                if response_length and cls._buffer_pos >= response_length:
                    return bytes(cls._buffer[:cls._buffer_pos])  # success
            except asyncio.TimeoutError:
                pass  # just retry, but code in finally is executed
            finally:
                cls._data_ready.clear()  # always clear unless we're returning

        # Failed after 3 tries
        if cls._buffer_pos == 2 and cls._buffer[0] == 0x15 and cls._buffer[1] == 0xEA:
            print(','.join(f"{b:02X}" for b in message) + " - unknown request")
            return None

        if cls._buffer_pos == 0:
            print(','.join(f"{b:02X}" for b in message) + " - no response (timeout)")
            return None

        print(','.join(f"{b:02X}" for b in message) + " - bad response (partial?)")
        return None

    @classmethod
    def update_energy(cls, mode, now):
        """
        Update energy counters for a given mode (heat, cool, loss).
        """
        last_name = f"{mode}_last_timestamp"
        produced_name = f"day_{mode}_produced"
        consumed_name = f"day_{mode}_consumed"
        on_sec_name = f"day_{mode}_on_sec"

        last = cls.status[last_name]
        if last is not None:
            sec = int(time.ticks_diff(now, last) / 1000)
            cls.status[consumed_name] += cls.status["power"] * sec / 3600 / 1000  # W -> kWh
            cls.status[produced_name] += cls.status["thermal_power"] * sec / 3600 / 1000
            cls.status[on_sec_name] += sec

        cls.status[last_name] = now
        cls.status[on_sec_name.replace("_on_sec", "_on")] = int(cls.status[on_sec_name] / 60)
        
        # Update cumulated COP
        if mode in ["heat", "loss"]:
            # Include both heating and loss counters for net heating COP
            total_produced = cls.status.get("day_heat_produced", 0) + cls.status.get("day_loss_produced", 0)
            total_consumed = cls.status.get("day_heat_consumed", 0) + cls.status.get("day_loss_consumed", 0)
            cls.status["day_heat_COP"] = total_produced / total_consumed if total_consumed != 0 else 0
        elif mode == "cool":
            # Cooling COP normal
            cls.status["day_cool_COP"] = cls.status[produced_name] / cls.status[consumed_name] if cls.status[consumed_name] > 0 else 0

    @classmethod
    async def get(cls):
        async with cls._lock:
            # Cache registers to avoid repeating UART calls
            register_cache = {}
            cls._uart.irq(handler = cls.uart_irq_handler, trigger = 1) # initialize irq handler
            for item in cls._labelDefs:
                addr, offset, conversion_id, size, data_type, label = item
                if addr not in register_cache:
                    register_cache[addr] = await cls.send_message([0x03, 0x40, addr], cls._response_lengths[addr])
                if(register_cache[addr] is None):
                    return(json.dumps(cls.status))
                cls.status[label] = cls.parse_value(register_cache[addr], item)
            cls._uart.irq(handler = None) # de-initialize irq handler
            t = TimeUtils.getdst()
            cls.status['year'] = t[0]
            cls.status['month'] = t[1]
            cls.status['day'] = t[2]
            cls.status['hour'] = t[3]
            cls.status['min'] = t[4]
            cls.status['sec'] = t[5]
            cls.status["time"] = "%02d:%02d:%02d" % (cls.status["hour"], cls.status["min"], cls.status["sec"])
            
            modes = ["heat", "cool", "loss"]

            # reset day counters at midnight (works also the first time this method is called)
            if cls._current_day != cls.status['day']:
                for mode in modes:
                    cls.status[f"day_{mode}_consumed"] = 0
                    cls.status[f"day_{mode}_produced"] = 0
                    cls.status[f"day_{mode}_COP"] = 0
                    cls.status[f"day_{mode}_on_sec"] = 0
                    cls.status[f"day_{mode}_on"] = 0
                    cls.status[f"{mode}_last_timestamp"] = None
                cls._current_day = cls.status['day']
                cls.status.pop("day_loss_COP", None) # COP for loss mode does not make sense 

            # calculate power consumed (Heat pump + Circulation pump)
            cls.status["power"] = int(3 * 230 * cls.status["INV primary current"])
            if cls.status["Water pump operation"] == "ON":
                cls.status["power"] += 70  # estimated pump power
            # calculate thermal power
            if cls.status["Water pump operation"] == "OFF":
                cls.status["thermal_power"] = 0
            else:
                dT = cls.status["Water heat exchanger outlet temp."] - cls.status["Water heat exchanger inlet temp."]
                cls.status["thermal_power"] = int(0.0004 * 1000 * 4180 * dT)
            now = time.ticks_ms()
            # Determine active mode, calculate COP and energy
            if cls.status["Operation Mode"] == "Heating":
                mode = "heat"
                cls.status["mode"] = cls.status["Operation Mode"]
                cls.update_energy(mode, now)
            elif cls.status["Operation Mode"] == "Cooling":
                mode = "cool"
                cls.status["mode"] = cls.status["Operation Mode"]
                cls.status["thermal_power"] = -cls.status["thermal_power"] # temp difference in H/E is opposite when cooling
                cls.update_energy(mode, now)
            elif cls.status["Operation Mode"] == "OFF":
                # Unit OFF → check if pump is ON and thermal power < 0 → this is LOSS MODE
                if cls.status["Water pump operation"] == "ON":
                    mode = "loss"
                    cls.status["mode"] = "Antifreeze"
                    cls.update_energy(mode, now)
                else:
                    mode = "off"
                    cls.status["mode"] = "OFF"
            # instant COP 
            cls.status["COP"] = cls.status["thermal_power"] / cls.status["power"] if cls.status["Operation Mode"] != "OFF" else 0
            # Reset timestamps for inactive modes only
            for m in modes:
                if m != mode:  # skip the active mode
                    cls.status[f"{m}_last_timestamp"] = None

            return(json.dumps(cls.status))
    
    @classmethod
    async def scheduler(cls):
       while(True):
            try:
                await asyncio.sleep(60)
                print("Daikin scheduler start")
                await cls.get()
            except Exception as e:
                print("Daikin scheduler error: {}".format(e))
            finally:
                print("Daikin scheduler end")
                
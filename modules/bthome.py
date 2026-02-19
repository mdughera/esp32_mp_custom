from bluetooth import BLE
import time
import uasyncio
import json
from time_utils import TimeUtils
import math
import gc # experimental: call gc.collect before turning on BLE to see if it soles WIFI issues
import network
from micropython import const


class BTHome:
    _ble = None
    _lock = uasyncio.Lock()
    _devices = None
    _irq_buffer = {}
    _irq_error = None
    _irq_error_count = 0
    _BUFFER_SIZE = 32
    _start = 0
    _SCANNING_TIMEOUT = 5 # scanning timeout after the first broadcast is received
    _ONE_YEAR_SECONDS = 365 * 24 * 60 * 60  # 31,536,000 seconds, one year
    
    # Convert a BLE address as 7c:c6:b6:72:9d:ae in the corresponding bytes as received in irq
    @staticmethod
    def addr_to_bytes(str):
        return(bytes.fromhex(str.replace(":", "")))
    
    @classmethod
    async def init(cls, loop, broadcast_interval = 60):
        cls._ble = BLE()
        cls._broadcast_interval = broadcast_interval
        with open('settings/bthome.json', 'r') as file:
            json_content = file.read()
        cls._devices = json.loads(json_content)
        for key, device in cls._devices.items():
            device['last_seen'] = -1
            # initialize irq buffer
            cls._irq_buffer[cls.addr_to_bytes(key)] = {                 
                'data': bytearray(cls._BUFFER_SIZE),
                'last_seen': 0,
                'rssi': 0
                }
            cls._init_day_counters(key)
        print("BTHome devices: ", cls._devices)
        
        # --- Create scanning tasks ---
        for addr_mac, device_info in cls._devices.items():
            loop.create_task(cls.scan_device(addr_mac, device_info))


    @classmethod
    def _init_day_counters(cls, addr_mac, day=-1):
        cls._devices[addr_mac].update({'day': day})
        cls._devices[addr_mac].update({'temperature_max': -999})
        cls._devices[addr_mac].update({'temperature_min': 999})
        cls._devices[addr_mac].update({'humidity_max': 0})
        cls._devices[addr_mac].update({'humidity_min': 999})
        cls._devices[addr_mac].update({'moisture_max': 0})
        cls._devices[addr_mac].update({'moisture_min': 999})

    @classmethod
    async def start(cls):
        async with cls._lock:
            if(not cls._ble.active() and cls._start > 0): # safeguard: it should never happen but resets status just in case
                cls._start = 0
            if(cls._start > 0):
                cls._start += 1
                print("BLE observer already started")
                return
            try:
                cls._ble.active(True)
                await uasyncio.sleep(0.05)
                cls._ble.irq(BTHome._ble_irq)
                scanDuration_ms = 0 # scanning duration in ms, 0 for infinite scanning
                interval_us = 100_000 
                window_us = 50_000 # the same window as interval, means continuous scan
                active = False #do not care for a reply for a scan from the transmitter
                cls._ble.gap_scan(scanDuration_ms,interval_us,window_us,active)
                cls._start=1
            except Exception as e:
                print(f"Error activating BLE: {e}")
                cls._start=0
                try:
                    cls._ble.active(False)
                except:
                    pass
            else:
                print("BLE observer started")

    @classmethod
    async def stop(cls):
        async with cls._lock:
            if(not cls._ble.active()):
                cls._start=0
                return
            if(cls._start > 1):
                cls._start -= 1
                print(f"BLE remains active ({cls._start} left)")
                return
            try:
                cls._start=0
                cls._ble.gap_scan(None)
                await uasyncio.sleep_ms(100)
                cls._ble.irq(None)
                cls._ble.active(False)
            except Exception as e:
                print(f"Error stopping BLE: {e}")
            print("BLE stopped")        

    # Callback to handle incoming BLE advertisements
    # *** prints in this routine may disconnect WEBREPL or cause crashes ***
    @staticmethod
    def _ble_irq(event, data):
        _IRQ_SCAN_RESULT = const(5)
        _IRQ_SCAN_DONE = const(6)
        try: 
            if event == _IRQ_SCAN_RESULT:
                addr_type, addr, adv_type, rssi, adv_data = data
                addr_mac = bytes(addr)
                if addr_mac in BTHome._irq_buffer:
                    buffer = BTHome._irq_buffer[addr_mac]
                    if(buffer['last_seen'] == 0): # avoid updates in case of message bursts from the same address
                        buffer['rssi'] = rssi
                        new_data = adv_data[:BTHome._BUFFER_SIZE]
                        buffer['data'][:len(new_data)] = new_data
                        buffer['data'][len(new_data):] = b'\x00' * (BTHome._BUFFER_SIZE - len(new_data))
                        buffer['last_seen'] = time.ticks_ms()
            elif event == _IRQ_SCAN_DONE:
                pass
        except Exception as e:
            BTHome._irq_error = e
            BTHome._irq_error_count += 1

    '''
    sample message from Shelly Blu H&T

    02 length
    01 AD data type
    06 modes
    0d length 13 bytes
    16 AD service data type
    d2 BThome UUID 1st byte (controllare questi due byte per capire se è un msg BTHome)
    fc BThome UUID 2nd byte 
    44 BTHome device info
    00ab non sono riuscito a capire cosa siano, la parsificazione inizia dal byte successivo
    01 battery
    64 battery value (100%)
    2e humidity 
    37 humidity value (55%)
    45 temperature
    eb temperature value (23.5)
    other bytes may appear here and must not be considered
    '''
    @classmethod
    def parse_values(cls, addr_mac, data, rssi, last_seen):
        if (data[5:7] != bytes.fromhex("d2fc")): # BTHome UUID: if not present, do not consider the message
            return()
        cls._devices[addr_mac].update({'button': 0}) # the button message does not always arrive
        cls._devices[addr_mac].update({'rssi': rssi})
        cls._devices[addr_mac].update({'last_seen': last_seen}) 
        i = 10
        while(i<len(data)-1):
            if(data[i] == 0x01):
                cls._devices[addr_mac].update({'battery': data[i+1]})
                i += 2
            elif(data[i] == 0x2e):
                cls._devices[addr_mac].update({'humidity': data[i+1]})
                i += 2
            elif(data[i] == 0x3a):
                cls._devices[addr_mac].update({'button': data[i+1]}) 
                i += 2
            elif(data[i] == 0x45):
                raw_value = int.from_bytes(data[i+1:i+3], 'little')
                # Handle signed conversion
                signed_value = raw_value - 65536 if raw_value > 32767 else raw_value
                temperature = signed_value / 10
                cls._devices[addr_mac].update({'temperature': temperature})
                i += 3
            else:
                #print(f"BTHome unknown code in position {i} {data[i]}")
                i += 2
        t = TimeUtils.getdst()
        if(cls._devices[addr_mac]['day'] != t[2]):
            cls._init_day_counters(addr_mac, t[2])
        cls._devices[addr_mac]['moisture'] = cls.absolute_humidity(cls._devices[addr_mac]['temperature'], cls._devices[addr_mac]['humidity'])
        if(cls._devices[addr_mac]['temperature'] > cls._devices[addr_mac]['temperature_max']):
            cls._devices[addr_mac]['temperature_max'] = cls._devices[addr_mac]['temperature']
        if(cls._devices[addr_mac]['temperature'] < cls._devices[addr_mac]['temperature_min']):
            cls._devices[addr_mac]['temperature_min'] = cls._devices[addr_mac]['temperature']                 
        if(cls._devices[addr_mac]['humidity'] > cls._devices[addr_mac]['humidity_max']):
            cls._devices[addr_mac]['humidity_max'] = cls._devices[addr_mac]['humidity']
        if(cls._devices[addr_mac]['humidity'] < cls._devices[addr_mac]['humidity_min']):
            cls._devices[addr_mac]['humidity_min'] = cls._devices[addr_mac]['humidity']
        if(cls._devices[addr_mac]['moisture'] > cls._devices[addr_mac]['moisture_max']):
            cls._devices[addr_mac]['moisture_max'] = cls._devices[addr_mac]['moisture']
        if(cls._devices[addr_mac]['moisture'] < cls._devices[addr_mac]['moisture_min']):
            cls._devices[addr_mac]['moisture_min'] = cls._devices[addr_mac]['moisture']
        return()
        
    @classmethod
    async def scan_device(cls, addr_mac, device_info):
        """ Asynchronous task to scan continuously for a specific device, with sleep intervals """
        timeout = cls._broadcast_interval * 2  # seconds to wait when device has to be discovered (overwritten after first receive)
        interval = 0.5  # Check every 0.5 seconds
        pause_duration = cls._broadcast_interval - 4 # after a successful receive sleep until 4 seconds before the next broadcast
        buffer = cls._irq_buffer[cls.addr_to_bytes(addr_mac)]
        while True:
            await cls.start()  # Start BLE scanning
            print(f"Started scanning for device {device_info['name']} ({addr_mac})")
            time_waited = 0
            buffer['last_seen'] = 0
            while (buffer['last_seen'] == 0):
                if time_waited >= timeout:
                    break
                await uasyncio.sleep(interval)
                time_waited += interval
            
            # Once data is received or timeout occurred, stop BLE scanning for this device
            if(cls._start>0):
                await cls.stop()
            if cls._irq_error:
                print("IRQ ERROR:", cls._irq_error)
                print("IRQ count:", cls._irq_error_count)
                cls._irq_error = None
            if (buffer['last_seen'] != 0): # there is data to be processed
                cls.parse_values(addr_mac, buffer['data'], buffer['rssi'], buffer['last_seen'])
                print(f"Finished parsing data for {device_info['name']}")
                timeout = cls._SCANNING_TIMEOUT # now we know when the device broadcast and we can reduce the timeout (this is done each time even if needed only the first time)
                buffer['last_seen'] = 0
                # print("BUFFER:", cls._irq_buffer)
            else:
                print(f"No data received from {device_info['name']}")
            

            # Pause for a specified duration before restarting scan
            # elapsed_time is the number of seconds passed from the last received message
            # if the last received message was older than one minute, subtract the minutest and take only the remaining seconds
            elapsed_time = int(time.ticks_diff(time.ticks_ms(), cls._devices[addr_mac]['last_seen']) / 1000) % 60
            await uasyncio.sleep(pause_duration - elapsed_time)

    @classmethod
    def create_tasks(cls, loop):
        # --- Create scanning tasks ---
        for addr_mac, device_info in cls._devices.items():
            loop.create_task(cls.scan_device(addr_mac, device_info))

    @classmethod
    def get(cls):
        return(cls._devices)
    
    @classmethod
    def absolute_humidity(cls, temp_c, rh_percent):
        '''
        Calculate absolute humidity in g/m³
        temp_c: temperature in Celsius
        rh_percent: relative humidity in %
        '''
        # constants
        mw = 18.016  # g/mol, molecular weight of water
        r = 8314.3   # J/(kmol*K), universal gas constant
        # saturation vapor pressure (hPa)
        svp = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
        # actual vapor pressure (hPa)
        avp = (rh_percent / 100.0) * svp * 100  # convert hPa to Pa
        # absolute humidity formula
        ah = int((1000 * mw / r) * avp / (temp_c + 273.15))  # g/m³
        return ah
    




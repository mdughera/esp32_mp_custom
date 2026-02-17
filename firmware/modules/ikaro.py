from modbus import Modbus
import json
import struct
import uasyncio as asyncio
from time_utils import TimeUtils
import time
import machine

class Ikaro:
    _slave = 1
    _modbus = None
    status = dict()
    _settings = None
    _manual = False
    
    @classmethod
    def init(cls, name, tx, rx, slave=1, timeout=4):
        cls._modbus = Modbus(Modbus.RTU, tx=tx, rx=rx, timeout=timeout)
        cls._slave = slave
        cls.status['name'] = name
        cls.init_day_counters()
        print(f"Ikaro initialized: {cls.status['name']} tx={cls._modbus.tx} rx={cls._modbus.rx} slave={cls._slave} timeout={cls._modbus.timeout}")
        with open('settings/ikaro.json', 'r') as file:
            json_content = file.read()
            cls._settings = json.loads(json_content)
        print("Settings: ", cls._settings)

    @classmethod
    async def set_slave(cls, new):
        print(f"MODIFICA INDIRIZZO {cls.old} in {new}")
        values = [new]
        resp = await cls._modbus.exec(cls._slave, Modbus.WRITE_HOLDING_REGISTERS, 28321, values)
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")
        
    @classmethod
    async def set_all(cls, data):
        if(data['fan_set'] != cls.status['fan_set'] or data['temp_set'] != cls.status['temp_set']):
            print("Set manual mode ", data['fan_set'], data['temp_set'])
            await cls.set(data['fan_set'], data['temp_set'])
            cls._manual = True
        else:
            print("Save schedule settings")
            cls._settings['mode'] = data['mode']
            cls._settings['from_to'] = data['schedule_from_to']
            cls._settings['fan'] = data['schedule_fan']
            cls._settings['temp'] = data['schedule_temp']
            print("Salvataggio impostazioni Ikaro ", cls._settings)
            with open('settings/ikaro.json', 'w') as file:
                json.dump(cls._settings, file)
            if(cls._manual):
                print("Reset to auto")
                cls._manual = False
        
    @classmethod
    async def set(cls, fan, temp):
        on = 1 if(fan > 0) else 0 # turn off fan set status to off
        print("Ikaro set ", fan, temp)
        values=[on, cls._settings["mode"], fan, 0x0000,0x0000,0x0000,0x0000,0x0000,0x0000, temp, temp] # set cool and heat temp to the same value
        resp = await cls._modbus.exec(cls._slave, Modbus.WRITE_HOLDING_REGISTERS, 28301, values)
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")
        
    @classmethod
    async def scheduler(cls):
        while(True):
            try:
                print("Scheduler start")
                await cls.get()
                from_h, from_m, to_h, to_m = \
                    int(cls._settings["from_to"][:2]), int(cls._settings["from_to"][2:4]), int(cls._settings["from_to"][4:6]), int(cls._settings["from_to"][6:8])
                t = TimeUtils.getdst()
                print(f"current {t[3]:0>2}:{t[4]:0>2} from {from_h:0>2}:{from_m:0>2} to {to_h:0>2}:{to_m:0>2}")
                from_t = from_h * 60 + from_m
                to_t = to_h * 60 + to_m
                cur_t = t[3]*60 + t[4]
                if from_t <= cur_t <= to_t:
                    print("The current time is inside the time range.")
                    if(cls._manual is False and (cls._settings["fan"] != cls.status["fan_set"] or cls._settings["temp"] != cls.status["temp_set"])):
                        print("imposta i valori programmati")
                        await cls.set(cls._settings["fan"], cls._settings["temp"])
                else:
                    print("The current time is outside the time range.")
                    if(cls.status["on"]):
                        print("imposta stato a off")
                        await cls.set(0, cls._settings["temp"])
                    if(cls._manual):
                        print("Reset to auto mode")
                        cls._manual = False
                sleep_interval = 120
            except Exception as e:
                print("Scheduler error: {}".format(e))
                sleep_interval = 3
            finally:
                print("Scheduler end")
                await asyncio.sleep(sleep_interval)
    
    @classmethod
    def init_day_counters(cls):
        cls.status['day'] = None
        cls.status['day_active_sec'] = cls.status['day_active'] = cls.status['day_max_temp'] = 0
        cls.status['day_min_temp'] = 999
        cls.status['last_timestamp'] = None    
    
    @classmethod
    async def get(cls):
        for i in range(3):
            try: 
                #print(f"LETTURA INPUT REGISTERS DI {cls._slave}")
                resp = await cls._modbus.exec(cls._slave, Modbus.READ_INPUT_REGISTERS, 46801, 3)
                #print(f"READ response: {resp}")
                cls.status['temp'], dummy, cls.status['fan'] = struct.unpack("!HHH", resp)
                #print(f"TEMP={cls.status['temp']} FAN={cls.status['fan']}")
                #print(f"LETTURA HOLDING REGISTERS DI {cls._slave}")
                resp = await cls._modbus.exec(cls._slave, Modbus.READ_HOLDING_REGISTERS, 28301, 11)
                #print(f"READ response: {resp}")
                cls.status['status'], cls.status['mode'], cls.status['fan_set'] = struct.unpack("!HHH", resp)
                cls.status['on'] = True if cls.status['status'] == 1 else False
                cls.status['temp_cool_set'], cls.status['temp_heat_set'] = struct.unpack("!HH", resp[18:])
                #print(f"STATUS={cls.status['status']} MODO={cls.status['mode']} FAN_SET={cls.status['fan_set']}")
                if(cls.status['mode'] == 1 or cls.status['mode'] == 2):
                #   print(f"TEMP_SET={cls.status['temp_cool_set']}")
                    cls.status['temp_set'] = cls.status['temp_cool_set']
                else:
                #    print(f"TEMP_SET={cls.status['temp_heat_set']}")
                    cls.status['temp_set'] = cls.status['temp_heat_set']
                t = TimeUtils.getdst()
                if(cls.status['day'] != t[2]):
                    cls.init_day_counters()
                cls.status['year'] = t[0]
                cls.status['month'] = t[1]
                cls.status['day'] = t[2]
                cls.status['hour'] = t[3]
                cls.status['min'] = t[4]
                cls.status['sec'] = t[5]
                cls.status["time"] = "%02d:%02d:%02d" % (cls.status["hour"], cls.status["min"], cls.status["sec"])
                cls.status["schedule_from_to"] = cls._settings["from_to"]
                cls.status["mode"] = cls._settings["mode"]  # mode from device is not reliable when turned off
                cls.status["schedule_fan"] = cls._settings["fan"]
                cls.status["schedule_temp"] = cls._settings["temp"]
                
                if(cls.status['on']):
                    now = time.ticks_ms()
                    if(cls.status['last_timestamp'] is not None):
                        sec = int(time.ticks_diff(now, cls.status["last_timestamp"]) / 1000)
                        cls.status["day_active_sec"] += sec
                        cls.status["day_active"] = int(cls.status["day_active_sec"] / 60)
                    cls.status["last_timestamp"] = now
                else:
                    cls.status["last_timestamp"] = None
                if cls.status['temp'] < cls.status['day_min_temp']:
                    cls.status['day_min_temp'] = cls.status['temp']
                if cls.status['temp'] > cls.status['day_max_temp']:
                    cls.status['day_max_temp'] = cls.status['temp']
                    
                print("Ikaro.get()=", cls.status)
                return(json.dumps(cls.status))
            except Exception as e: 
                print("Get error: {}".format(e))
            await asyncio.sleep(2)
        raise ValueError("Error in reading data from device")

    @classmethod
    async def set_status(cls, status):
        print(f"IMPOSTAZIONE STATO {cls._slave} A {status}")
        values = [status]
        resp = await cls._modbus.exec(cls._slave, Modbus.WRITE_HOLDING_REGISTERS, 28301, values)
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")
        
    @classmethod
    async def set_mode(cls, mode):
        print(f"IMPOSTAZIONE MODE {cls._slave} A {mode}")
        values = [mode]
        resp = await cls._modbus.exec(cls._slave, Modbus.WRITE_HOLDING_REGISTERS, 28302, values)
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")
        
    @classmethod
    def set_fan(cls, speed, slave=1):
        print(f"IMPOSTAZIONE VENTOLA {slave} A {speed}")
        values = [speed]
        resp = asyncio.run(cls._modbus.exec(slave, Modbus.WRITE_HOLDING_REGISTERS, 28303, values))
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")
        
    @classmethod
    def set_target_cond(cls, temp, slave=1):
        print(f"IMPOSTAZIONE TEMPERATURA COND {slave} A {temp}")
        values = [temp]
        resp = asyncio.run(cls._modbus.exec(slave, Modbus.WRITE_HOLDING_REGISTERS, 28310, values))
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")
        
    @classmethod
    def set_target_risc(cls, temp, slave=1):
        print(f"IMPOSTAZIONE TEMPERATURA RISC {slave} A {temp}")
        values = [temp]
        resp = asyncio.run(cls._modbus.exec(slave, Modbus.WRITE_HOLDING_REGISTERS, 28311, values))
        print(f"WRITE response {bytes(resp).hex()} - {resp[2]} registers")


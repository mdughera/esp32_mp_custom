import uasyncio as asyncio
import time
import struct
from modbus import Modbus

class Goodwe:
    WORK_MODES_ET: dict[int, str] = {
        0: "Wait Mode",
        1: "Normal (On-Grid)",
        2: "Normal (Off-Grid)",
        3: "Fault Mode",
        4: "Flash Mode",
        5: "Check Mode",
    }

    GRID_STATUS: dict[int, str] = {
        1: "Online",
        7: "Offline",
    }
    
    result = {
        "year":0, "month":0, "day":0,
        "hour":0, "min":0, "sec":0,

        "PV1_power":0, "PV2_power":0, "PV_power":0,
        "backup_power":0, "load_power":0,

        "battery_voltage":0.0,
        "battery_current":0.0,
        "battery_power":0,
        "battery_mode":0,
        "battery_label":"",

        "working_mode":0,
        "working_mode_label":"",

        "day_PV":0,

        "SOC":0,

        "meter_active_power":0,
        "meter_reactive_power":0,
        "meter_apparent_power":0,
        "meter_label":"",

        "charge_voltage":0,
        "charge_current":0,
        "discharge_voltage":0,
        "discharge_current":0,
        "discharge_depth":0,
        "discharge_voltage_offline":0,
        "discharge_depth_offline":0,

        "grid_status":0,
        "grid_status_label":"",

        "house":0,

        "lock_mode":0,

        # energy counters
        "day_house":0,
        "day_import":0,
        "day_export":0,
        "day_charge":0,
        "day_discharge":0,
        "energy_timestamp":0,
        "energy_day":0
    }

    
    _lock = asyncio.Lock()
    _mv_map = {}
  
    @classmethod
    def init(self, ip, port, slaveid):
        self.ip = ip # inverter ip or hostname
        self.port = port # UDP port
        self.slave = slaveid # Modbus slave id
        self.init_energy_counters()
        self.modbus = Modbus(Modbus.UDP, self.ip, self.port, 5)

    @classmethod
    async def get(cls, cached=False):
        async with cls._lock:
            try:
                if cached and "year" in cls.result:
                    return cls.result

                # --- Read blocks ---
                chunks = [
                    (35100, 96, "Inverter"),
                    #(35193, 20, "Inverter day counters"),
                    (36008, 35, "Meter"),
                    (37000, 45, "BMS"),
                    (45352, 14, "Battery"),
                    (47511, 2, "Settings"),
                ]

                for reg_addr, count, name in chunks:
                    data = await cls.modbus.exec(
                        cls.slave,
                        Modbus.READ_HOLDING_REGISTERS,
                        reg_addr,
                        count
                    )
                    
                    # header is stripped by response but CRC remains.
                    # todo: modify modbus.exec to strip CRC as well and modify check here
                    if data and len(data) == (count * 2)+2:    
                        cls._mv_map[reg_addr] = memoryview(data)
                    else:
                        cls._mv_map[reg_addr] = None
                        print(f"{name} data {reg_addr}-{reg_addr+count-1} not received. Use cache.")


                base = None
                def reg(a): return (a - base) * 2
                
                # =========================================================
                # Inverter (35100)
                # =========================================================
                base = 35100
                mv = cls._mv_map.get(base)
                if mv:
                    cls.result["year"], cls.result["month"], cls.result["day"], \
                    cls.result["hour"], cls.result["min"], cls.result["sec"] = \
                        struct.unpack_from("!6B", mv, reg(35100))

                    cls.result["time"] = "%02d:%02d:%02d" % (
                        cls.result["hour"],
                        cls.result["min"],
                        cls.result["sec"]
                    )

                    cls.result["PV1_power"], = struct.unpack_from("!I", mv, reg(35105))
                    cls.result["PV2_power"], = struct.unpack_from("!I", mv, reg(35109))
                    cls.result["PV_power"] = cls.result["PV1_power"] + cls.result["PV2_power"]

                    cls.result["backup_power"], = struct.unpack_from("!h", mv, reg(35170))
                    cls.result["load_power"],   = struct.unpack_from("!h", mv, reg(35172))
                    cls.result["house"] = cls.result["backup_power"] + cls.result["load_power"]

                    v, i = struct.unpack_from("!hh", mv, reg(35180))
                    cls.result["battery_voltage"] = v / 10
                    cls.result["battery_current"] = i / 10
                    cls.result["battery_power"] = -int(cls.result["battery_voltage"] * cls.result["battery_current"])
                    cls.result["battery_label"] = (
                        "Charging" if cls.result["battery_power"] > 0 else "Discharging"
                    )

                    cls.result["battery_mode"], = struct.unpack_from("!H", mv, reg(35184))
                    cls.result["working_mode"], = struct.unpack_from("!H", mv, reg(35187))
                    cls.result["working_mode_label"] = cls.WORK_MODES_ET.get(
                        cls.result["working_mode"], "Unknown"
                    )

                    day_pv, = struct.unpack_from("!I", mv, reg(35193))
                    cls.result["day_PV"] = day_pv / 10
                    
                '''
                # =========================================================
                # Inverter day counters (35193)
                # =========================================================
                base = 35193
                mv = cls._mv_map.get(base)
                if mv:
                    # Prova non statici
                    cls.result["e_day_sell"], = struct.unpack_from("!H", mv, reg(35199)) # energy sent out from inverter, can be used by loads or exported
                    cls.result["e_day_buy"], = struct.unpack_from("!H", mv, reg(35202)) # opposite of e_day_sell
                    cls.result["e_day_load"], = struct.unpack_from("!H", mv, reg(35205)) # does not consider backup, one digit precision
                    cls.result["e_day_charge"], = struct.unpack_from("!H", mv, reg(35208)) # reliable but one digit precision
                    cls.result["e_day_discharge"], = struct.unpack_from("!H", mv, reg(35211)) # reliable but one digit precision
                '''

                # =========================================================
                # Meter (36008)
                # =========================================================
                base = 36008
                mv = cls._mv_map.get(base)
                if mv:
                    cls.result["meter_active_power"], \
                    cls.result["meter_reactive_power"] = \
                        struct.unpack_from("!hh", mv, reg(36008))

                    cls.result["meter_apparent_power"], = \
                        struct.unpack_from("!i", mv, reg(36041))

                    cls.result["meter_label"] = "Importing" if cls.result.get("meter_active_power", 0) < 0 else "Exporting"

                # =========================================================
                # BMS (37000)
                # =========================================================
                base = 37000
                mv = cls._mv_map.get(base)
                if mv:
                    cls.result["bat_discharge_limit"], = struct.unpack_from("!H", mv, reg(37005))
                    cls.result["bat_charge_limit"],    = struct.unpack_from("!H", mv, reg(37006))
                    cls.result["SOC"],                = struct.unpack_from("!H", mv, reg(37007))

                # =========================================================
                # Battery settings (45352)
                # =========================================================
                base = 45352
                mv = cls._mv_map.get(base)
                if mv:
                    cls.result["charge_voltage"], = struct.unpack_from("!H", mv, reg(45352))
                    cls.result["charge_voltage"] /= 10
                    cls.result["charge_current"], = struct.unpack_from("!H", mv, reg(45353))
                    cls.result["charge_current"] /= 10
                    cls.result["discharge_voltage"], = struct.unpack_from("!H", mv, reg(45354))
                    cls.result["discharge_voltage"] /= 10
                    cls.result["discharge_current"], = struct.unpack_from("!H", mv, reg(45355))
                    cls.result["discharge_current"] /= 10
                    cls.result["discharge_depth"], = struct.unpack_from("!H", mv, reg(45356))
                    cls.result["discharge_voltage_offline"], = struct.unpack_from("!H", mv, reg(45357))
                    cls.result["discharge_voltage_offline"] /= 10
                    cls.result["discharge_depth_offline"], = struct.unpack_from("!H", mv, reg(45358))

                # =========================================================
                # Settings (47511)
                # =========================================================
                base = 47511               
                mv = cls._mv_map.get(base)
                if mv:
                    cls.result["grid_status"], = struct.unpack_from("!H", mv, reg(47511))

                    if cls.result["grid_status"] == 11:
                        cls.result["grid_status"] = 1

                    cls.result["grid_status_label"] = cls.GRID_STATUS.get(
                        cls.result["grid_status"], "Unknown"
                    )
                    

                cls.update_energy_counters()

            except Exception as e:
                print(f"Goodwe get error: {e}")

            return cls.result

        
    @classmethod
    def init_energy_counters(self):
        self.result["day_house"] = 0
        self.result["day_import"] = 0
        self.result["day_export"] = 0
        self.result["day_charge"] = 0
        self.result["day_discharge"] = 0
        self.result["energy_timestamp"] = 0
        self.result["energy_day"] = 0
        
    @classmethod
    def update_energy_counters(self):     
        if(self.result["day"] != self.result["energy_day"]):
            self.init_energy_counters()
            print("Energy counters reset")
        datetime_components = (self.result["year"]+2000, self.result["month"], self.result["day"], self.result["hour"], self.result["min"], self.result["sec"], 0, 0)
        current_timestamp = time.mktime(datetime_components)
        if(self.result["energy_day"] == 0):
            difference = 1
        else:
            difference = (current_timestamp - self.result["energy_timestamp"]) / 60
        self.result["energy_timestamp"] = current_timestamp
        self.result["energy_day"] = self.result["day"]
        self.result["day_house"] += (self.result["house"]*difference/60)/1000
        if(self.result["meter_active_power"] > 0):
            self.result["day_export"] += (self.result["meter_active_power"]*difference/60)/1000
        else:
            self.result["day_import"] -= (self.result["meter_active_power"]*difference/60)/1000
        if(self.result["battery_power"] < 0):
            self.result["day_discharge"] -= (self.result["battery_power"]*difference/60)/1000
        else:
            self.result["day_charge"] += (self.result["battery_power"]*difference/60)/1000
    
    @classmethod
    async def energy_logger(self, period):
        while(True):
            try:
                await asyncio.sleep(period)
                print("Energy logger start")
                await self.get()
                # control max SOC
                '''
                print(self.result['PV_power'], " ", self.result['SOC'], self.result['charge_current'])
                desired_charge_current = self.result['charge_current']  # default: keep current
                if self.result['PV_power'] > 0:
                    if self.result['SOC'] >= 80:
                        desired_charge_current = 0
                    else:
                        desired_charge_current = 10
                else: # PV_power == 0
                    desired_charge_current = 10
                if desired_charge_current != self.result['charge_current']: # apply only if changed
                    await self.charge_limit(desired_charge_current)
                else:
                    print("required charge_current already set")
                '''
            except Exception as e:
                print("Energy logger error: {}".format(e))
            print("Energy logger end")
      
    @classmethod
    async def set_ongrid(self):
        print("set_ongrid begin")
        # set status
        #await self.modbus_request(bytearray([0xF7, 0x10, 0xB9, 0x97, 0x00, 0x02, 0x04, 0x00, 0x01, 0x00, 0x00]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTERS,  47511, [0x0001, 0x0000])
        # set work mode
        #await self.modbus_request(bytearray([0xF7, 0x06, 0xB7, 0x98, 0x00, 0x00]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER,  47000, 0x0000)
        # clear battery mode
        #await self.modbus_request(bytearray([0xF7, 0x06, 0xB9, 0xAD, 0x00, 0x01]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER, 47533, 0x0001)
        print("set_ongrid end")

    @classmethod
    async def set_offgrid(self):
        print("set_offgrid begin")
        # set status
        #await self.modbus_request(bytearray([0xF7, 0x10, 0xB9, 0x97, 0x00, 0x02, 0x04, 0x00, 0x07, 0x00, 0x00]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTERS,  47511, [0x0007, 0x0000])
        # set work mode
        #await self.modbus_request(bytearray([0xF7, 0x06, 0xB7, 0x98, 0x00, 0x01]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER,  47000, 0x0001)
        # backup supply
        #await self.modbus_request(bytearray([0xF7, 0x06, 0xB0, 0xC4, 0x00, 0x01]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER,  45252, 0x0001)
        # cold start
        #await self.modbus_request(bytearray([0xF7, 0x06, 0xB0, 0xC0, 0x00, 0x04]))
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER,  45248, 0x0004)
        print("set_offgrid end")
        
    @classmethod
    def lock_mode(self, mode):
        print("lock_mode: %d", mode)
        self.result["lock_mode"] = mode
        
    @classmethod
    async def discharge_limit(self, value):
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER,  45355, int(value*10))
        print(f"set battery discharge limit to {value}A")
        
    @classmethod
    async def charge_limit(self, value):
        await self.modbus.exec(self.slave, Modbus.WRITE_HOLDING_REGISTER,  45353, int(value*10))
        print(f"set battery charge limit to {value}A")
        
 









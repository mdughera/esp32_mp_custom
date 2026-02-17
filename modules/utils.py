import math

class PVUtils:
    def __init__(self, peak_power, temp_coeff=-0.004, NOCT=45, T_ref=25):
        """
        peak_power: Peak power of PV system in watts.
        temp_coeff: Temperature coefficient per °C above 25°C (typically negative). e.g -0.4%/°C
        NOCT: Nominal Operating Cell Temperature (°C).
        T_ref: Reference temperature for rated power (default is 25°C).
        """
        self.peak_power = peak_power
        self.temp_coeff = temp_coeff
        self.NOCT = NOCT
        self.T_ref = T_ref

    def estimate_module_temperature(self, irradiance_w_m2, air_temp_c):
        """
        Estimate PV module temperature (°C) from air temperature and irradiance.
        Uses NOCT-based model and prevents unrealistic low temps.
        """
        # NOCT model: module warms above air temp depending on irradiance
        temp_module = air_temp_c + (self.NOCT - 20) / 800 * max(0, irradiance_w_m2)
        # Module cannot be cooler than air
        temp_module = max(temp_module, air_temp_c)
        return temp_module

    def estimate_power(self, irradiance_w_m2, air_temp_c):
        """
        Estimate PV power output for a given irradiance and air temperature.
        irradiance_w_m2: Global tilted irradiance in W/m².
        air_temp_c: Ambient air temperature in °C.
        Returns estimated power in watts (float).
        """
        # Avoid negative irradiance
        irradiance_w_m2 = max(0, irradiance_w_m2)

        # Module temperature
        temp_module = self.estimate_module_temperature(irradiance_w_m2, air_temp_c)

        # Temperature correction factor
        temp_factor = 1 + self.temp_coeff * (temp_module - self.T_ref)
        # Clamp temp_factor to realistic range (0%–110% efficiency)
        temp_factor = max(0.0, min(temp_factor, 1.10))

        # Power calculation with derating
        power = (irradiance_w_m2 / 1000.0) * self.peak_power * temp_factor * 0.95

        return round(power, 2)


    
class AtmosphericUtils:
    @staticmethod
    def absolute_humidity(temp_c, rh_percent):
        """
        Calculate absolute humidity in g/m³.
        temp_c: Air temperature in Celsius.
        rh_percent: Relative humidity in percent.
        Returns absolute humidity in g/m³ (integer).
        """
        mw = 18.016  # g/mol, molecular weight of water
        r = 8314.3   # J/(kmol*K), universal gas constant
        svp = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))  # hPa
        avp = (rh_percent / 100.0) * svp * 100  # convert hPa to Pa
        ah = int((1000 * mw / r) * avp / (temp_c + 273.15))
        return ah


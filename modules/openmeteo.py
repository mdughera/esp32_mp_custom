import asyncio
import json
import gc
from http_client import http_client
from time_utils import TimeUtils
from utils import PVUtils

class OpenMeteo:
    def __init__(self, latitude=45.0395, longitude=7.7771, tilt=30, azimuth=-40, peak_power=8200):
        self.latitude = latitude
        self.longitude = longitude
        self.tilt = tilt
        self.azimuth = azimuth
        self.peak_power = peak_power
        self.weather_data = None  # store last fetched data

    # ----------------------
    # Helper functions
    # ----------------------
    def parse_iso_datetime(self, dt_str):
        """Parse YYYY-MM-DDTHH:MM into tuple (year, month, day, hour, minute)"""
        date_part, time_part = dt_str.split("T")
        y, m, d = map(int, date_part.split("-"))
        h, mi = map(int, time_part.split(":"))
        return (y, m, d, h, mi)

    def is_daylight(self, dt_tuple, sunrise_tuple, sunset_tuple):
        """Return True if dt_tuple is between sunrise and sunset"""
        t_minutes = dt_tuple[3]*60 + dt_tuple[4]
        sunrise_minutes = sunrise_tuple[3]*60 + sunrise_tuple[4]
        sunset_minutes = sunset_tuple[3]*60 + sunset_tuple[4]
        return sunrise_minutes <= t_minutes <= sunset_minutes

    # ----------------------
    # Update / fetch
    # ----------------------
    async def update(self):
        """Fetch, process, and cache weather data with PV estimates (BLE-safe)"""
        url = (
            f"http://api.open-meteo.com/v1/forecast?"
            f"latitude={self.latitude}&longitude={self.longitude}"
            "&hourly=temperature_2m,global_tilted_irradiance,cloud_cover"
            "&current=global_tilted_irradiance_instant,temperature_2m,cloud_cover"
            "&daily=precipitation_hours,sunrise,sunset,daylight_duration,wind_speed_10m_max"
            f"&timezone=Europe%2FRome&forecast_days=3&tilt={self.tilt}&azimuth={self.azimuth}"
            "&past_days=0&models=best_match"
        )

        try:
            response = await http_client(url, retries=1, timeout=5, fallback_buffer_size=None)
        except Exception as e:
            print(f"OpenMeteo fetch failed: {e}")
            return False

        if response.status_code != 200:
            print(f"OpenMeteo returned error: {response.status_code}")
            return False

        await asyncio.sleep(0)
        self.weather_data = json.loads(response.body)

        # BLE-safe processing
        await self._process_weather()

        # Discard hourly data to save RAM
        if "hourly" in self.weather_data:
            del self.weather_data['hourly']

        gc.collect()
        return True

    # ----------------------
    # Process weather
    # ----------------------
    async def _process_weather(self):
        """Process hourly data on-the-fly, estimate PV, compute daily summaries (BLE-safe)"""
        data = self.weather_data
        hourly = data['hourly']
        times = hourly['time']
        irradiance = hourly['global_tilted_irradiance']
        temperature = hourly['temperature_2m']
        cloud_cover = hourly['cloud_cover']

        pv = PVUtils(peak_power=self.peak_power)

        # Current instant PV
        current = data['current']
        current['pv_power_instant'] = pv.estimate_power(
            current['global_tilted_irradiance_instant'],
            current['temperature_2m']
        )

        # Time info for remaining PV
        now = TimeUtils.getdst()
        now_date = f"{now[0]:04d}-{now[1]:02d}-{now[2]:02d}"
        now_minutes = now[3] * 60 + now[4]

        # Parse daily sunrise/sunset
        sunrise_times = [self.parse_iso_datetime(s) for s in data['daily']['sunrise']]
        sunset_times = [self.parse_iso_datetime(s) for s in data['daily']['sunset']]
        daily_dates = [f"{s[0]:04d}-{s[1]:02d}-{s[2]:02d}" for s in sunrise_times]

        # Initialize daily summary
        daily_summary = {date: {
            "pv_power_total": 0.0,
            "pv_power_remaining": 0.0,
            "temp_min": float('inf'),
            "temp_max": float('-inf'),
            "cloud_min": float('inf'),
            "cloud_max": float('-inf'),
            "cloud_sum": 0.0,
            "count": 0,
            "wind_speed_10m_max": 0.0,
            "precipitation_hours": 0.0
        } for date in daily_dates}

        # Copy daily wind and precipitation from API
        wind_api = data['daily'].get('wind_speed_10m_max', [0]*len(daily_dates))
        prec_api = data['daily'].get('precipitation_hours', [0]*len(daily_dates))
        for i, date in enumerate(daily_dates):
            daily_summary[date]['wind_speed_10m_max'] = wind_api[i]
            daily_summary[date]['precipitation_hours'] = prec_api[i]

        # Process hourly data BLE-safe
        for i, time_str in enumerate(times):
            dt_tuple = self.parse_iso_datetime(time_str)
            date = f"{dt_tuple[0]:04d}-{dt_tuple[1]:02d}-{dt_tuple[2]:02d}"
            hour_end_min = dt_tuple[3]*60 + dt_tuple[4]

            if date not in daily_summary:
                continue  # skip hours outside daily range

            record = daily_summary[date]

            # Cloud only during daylight
            idx_daily = daily_dates.index(date)
            if self.is_daylight(dt_tuple, sunrise_times[idx_daily], sunset_times[idx_daily]):
                cloud = cloud_cover[i]
            else:
                cloud = 0

            # PV calculation
            pv_power = pv.estimate_power(irradiance[i], temperature[i])
            record['pv_power_total'] += pv_power

            # Remaining PV
            if date == now_date:
                start_min = hour_end_min - 60
                if now_minutes < start_min:
                    record['pv_power_remaining'] += pv_power
                elif start_min <= now_minutes < hour_end_min:
                    record['pv_power_remaining'] += pv_power * (hour_end_min - now_minutes)/60.0
            elif date > now_date:
                record['pv_power_remaining'] += pv_power

            # Temp stats
            temp = temperature[i]
            record['temp_min'] = min(record['temp_min'], temp)
            record['temp_max'] = max(record['temp_max'], temp)

            # Cloud stats
            record['cloud_min'] = min(record['cloud_min'], cloud)
            record['cloud_max'] = max(record['cloud_max'], cloud)
            record['cloud_sum'] += cloud
            record['count'] += 1

            # Yield to BLE every 10 iterations
            if i % 10 == 0:
                await asyncio.sleep(0)

        # Finalize cloud averages
        for record in daily_summary.values():
            if record['count'] > 0:
                record['cloud_avg'] = round(record['cloud_sum'] / record['count'], 1)
            else:
                record['cloud_avg'] = 0
            del record['cloud_sum']
            del record['count']

        # Merge into weather_data daily
        data['daily'] = {
            'time': list(daily_summary.keys()),
            'pv_power_total': [round(daily_summary[d]['pv_power_total'], 2) for d in daily_summary],
            'pv_power_remaining': [round(daily_summary[d]['pv_power_remaining'], 2) for d in daily_summary],
            'temp_min': [daily_summary[d]['temp_min'] for d in daily_summary],
            'temp_max': [daily_summary[d]['temp_max'] for d in daily_summary],
            'cloud_min': [daily_summary[d]['cloud_min'] for d in daily_summary],
            'cloud_max': [daily_summary[d]['cloud_max'] for d in daily_summary],
            'cloud_avg': [daily_summary[d]['cloud_avg'] for d in daily_summary],
            'wind_speed_10m_max': [daily_summary[d]['wind_speed_10m_max'] for d in daily_summary],
            'precipitation_hours': [daily_summary[d]['precipitation_hours'] for d in daily_summary]
        }

        gc.collect()

    # ----------------------
    # Logger
    # ----------------------
    async def logger(self, interval_seconds=3600):
        """Periodically call update() every interval_seconds (BLE-safe)"""
        while True:
            try:
                print("Weather update...")
                await self.update()
                print("Weather updated")
                await asyncio.sleep(interval_seconds)
            except Exception as e:
                import traceback
                print("Error openmeteo logger:", e)
                print(traceback.format_exc())
                await asyncio.sleep(60)

    # ----------------------
    # Get daily summary
    # ----------------------
    def get_daily(self):
        """Return daily summary as a list of dicts (JSON-ready), sorted by date ascending"""
        data = self.weather_data
        if not data or "daily" not in data:
            return []

        daily = data["daily"]
        times = daily["time"]
        fields = [k for k in daily if k != "time"]

        # Sort dates ascending (ISO format yyyy-mm-dd is naturally sortable)
        sorted_indices = sorted(range(len(times)), key=lambda i: times[i])

        _, time_str = self.weather_data['current']['time'].split("T")

        result = []
        for i in sorted_indices:
            date = times[i]
            y, m, d = date.split("-")
            date_str = f"{d}/{m}/{y}"

            day_data = {"date": date_str, "time": time_str}
            for f in fields:
                day_data[f] = daily[f][i]
            result.append(day_data)

        return result


import time
import network
import machine
import socket
import uasyncio as asyncio
import struct

class TimeUtils:
    boot_time = None
    ntp_sync = False
    NTP_EPOCH = 2208988800
    ESP32_UNIX_OFFSET = 946684800
    NTP_PORT = 123

    @classmethod
    async def initialize(cls, start_time=None, events=None):
        """
        Initializes the TimeUtils class with the current time or a user-provided start time.
        :param start_time: Optional start time in seconds since epoch.
        """
        cls.boot_time = start_time if start_time is not None else time.time()
        while not cls.ntp_sync:
            try:
                if(events is not None):
                    for ev in events:
                        print(f"Waiting for: {ev.name}")
                        await ev.wait()
                        print(f"{ev.name} has been set")
                if await cls.set_RTC():  # True if successful
                    break
            except Exception as e:
                print("ntp sync error:", e)
            finally:
                await asyncio.sleep(10)

    @classmethod
    def get_uptime(cls):
        if cls.boot_time is None:
            raise ValueError("TimeUtils not initialized. Call TimeUtils.initialize() first.")

        current_time = time.time()
        uptime_seconds = int(current_time - cls.boot_time)

        days = uptime_seconds // (24 * 3600)
        uptime_seconds %= (24 * 3600)
        hours = uptime_seconds // 3600
        uptime_seconds %= 3600
        minutes = uptime_seconds // 60
        seconds = uptime_seconds % 60

        days_string = f"{days} days, " if days > 0 else ""
        uptime = f"{days_string}{hours:02}:{minutes:02}:{seconds:02}"

        return {
            "uptime_days": days,
            "uptime_hours": hours,
            "uptime_minutes": minutes,
            "uptime_seconds": seconds,
            "uptime": uptime
        }

    @classmethod
    def last_sunday(cls, year, month, hour_utc=1):
        for day in range(31, 24, -1):
            try:
                epoch = time.mktime((year, month, day, hour_utc, 0, 0, 0, 0))
                dt = time.gmtime(epoch)
                if dt[1] == month and dt[6] == 6:  # Sunday
                    return epoch
            except Exception:
                continue
        raise ValueError(f"Could not determine last Sunday for {month}/{year}")

    @classmethod
    def getdst(cls, secs=None):
        utc_secs = secs if secs is not None else time.time()
        year = time.gmtime(utc_secs)[0]
        start_dst = cls.last_sunday(year, 3, hour_utc=1)
        end_dst = cls.last_sunday(year, 10, hour_utc=1)
        delta_secs = 2 * 3600 if start_dst <= utc_secs < end_dst else 1 * 3600
        return time.gmtime(utc_secs + delta_secs)

    @classmethod
    async def get_ntp_time(cls, ntp_server="pool.ntp.org", timeout=5):
        """
        Fetch NTP time asynchronously (MicroPython / ESP32 safe).
        - Uses a single UDP socket
        - Caches DNS to avoid repeated blocking lookups
        - Polls asynchronously without hammering lwIP
        Returns UNIX timestamp on success, None on failure.
        """
        sock = None
        try:
            # Resolve NTP server once and cache
            if not hasattr(cls, "_ntp_addr") or cls._ntp_addr[0] != ntp_server:
                cls._ntp_addr = socket.getaddrinfo(ntp_server, cls.NTP_PORT)[0][-1]

            addr = cls._ntp_addr

            # Create one UDP socket for the request
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)

            # Build NTP request packet
            request = b'\x1b' + 47 * b'\0'

            # Send request
            sock.sendto(request, addr)

            # Poll for response asynchronously
            poll_interval = 0.2  # 200 ms, lwIP-friendly
            attempts = int(timeout / poll_interval)
            for _ in range(attempts):
                await asyncio.sleep(poll_interval)
                try:
                    response, _ = sock.recvfrom(48)
                    ntp_time = struct.unpack("!I", response[40:44])[0]
                    return ntp_time - cls.NTP_EPOCH - cls.ESP32_UNIX_OFFSET
                except OSError:
                    continue  # no data yet, retry

            # Timeout reached
            print(f"NTP request to {ntp_server} timed out")
            return None

        except Exception as e:
            print("NTP request failed:", e)
            return None

        finally:
            if sock:
                sock.close()


    @classmethod
    async def set_RTC(cls, ntp_server="time.google.com", timeout=5):
        """
        Fetch NTP time and update the board's RTC.
        Preserves logging and raises RuntimeError on failure.
        """
        print("Fetching NTP time...")
        timestamp = await cls.get_ntp_time(ntp_server, timeout)

        if not timestamp:
            print("Failed to update RTC.")
            cls.ntp_sync = False
            return False  # Retry-friendly

        try:
            t = time.localtime(timestamp)
            rtc_time = (t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0)
            rtc = machine.RTC()
            rtc.datetime(rtc_time)
            cls.ntp_sync = True
            cls.boot_time = time.time()
            print("RTC Updated:", rtc.datetime())
            return True
        except Exception as e:
            print("RTC update error:", e)
            cls.ntp_sync = False
            raise RuntimeError(f"RTC update failed: {e}")

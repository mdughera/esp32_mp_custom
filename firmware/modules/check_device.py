import asyncio
import socket

async def check_device(host, port=80, timeout=2, retries=2):
    # Step 1: DNS resolution
    try:
        addr = socket.getaddrinfo(host, port)[0][4]  # addr = (ip, port)
        ip = addr[0]  # extract IP
    except Exception as e:
        print(f"Error resolving address {host}: {e}")
        return False
    
    for attempt in range(retries):
        try:
            reader, writer = await asyncio.wait_for_ms(
                asyncio.open_connection(ip, port), int(timeout * 1000)
            )
            writer.close()
            try:
                await writer.wait_closed()
            except AttributeError:
                pass
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep_ms(100)  # brief pause before retry
    print("Failed:", host)
    return False
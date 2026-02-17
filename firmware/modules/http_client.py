import asyncio

READ_SIZE = 512
MAX_HEADER = 2048


class HttpResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self.body = body

    def __repr__(self):
        return "<HttpResponse {} {} bytes>".format(
            self.status_code, len(self.body)
        )


async def http_client(url, retries=3, timeout=5, fallback_buffer_size=None):

    # --- URL parse ---
    use_ssl = False
    if url.startswith("https://"):
        use_ssl = True
        url = url[8:]
        port = 443
    elif url.startswith("http://"):
        url = url[7:]
        port = 80
    else:
        port = 80

    if '/' in url:
        host, path = url.split('/', 1)
        path = '/' + path
    else:
        host = url
        path = '/'


    async def single_request():
        reader = writer = None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=use_ssl),
                timeout
            )

            # --- send request ---
            req = (
                "GET {} HTTP/1.1\r\n"
                "Host: {}\r\n"
                "Connection: close\r\n\r\n"
            ).format(path, host)

            writer.write(req.encode())
            await writer.drain()

            # --- read headers ---
            header_bytes = b""

            while b"\r\n\r\n" not in header_bytes:
                chunk = await asyncio.wait_for(reader.read(64), timeout)
                if not chunk:
                    break

                header_bytes += chunk
                if len(header_bytes) > MAX_HEADER:
                    raise ValueError("Header too large")

            header_text, _, remaining = header_bytes.partition(b"\r\n\r\n")
            lines = header_text.decode().split("\r\n")

            # status
            try:
                status = int(lines[0].split()[1])
            except:
                status = 500

            # headers
            headers = {}
            for l in lines[1:]:
                if ':' in l:
                    k, v = l.split(':', 1)
                    headers[k.lower().strip()] = v.strip()

            # --- body ---
            transfer_encoding = headers.get("transfer-encoding", "")
            content_length = headers.get("content-length")

            # CHUNKED
            if "chunked" in transfer_encoding:
                body_parts = []
                buffer = bytearray(remaining)

                while True:
                    while b"\r\n" not in buffer:
                        buffer.extend(await reader.read(64))

                    p = buffer.find(b"\r\n")
                    line = buffer[:p]
                    buffer = buffer[p+2:]

                    size = int(bytes(line), 16)
                    if size == 0:
                        break

                    while len(buffer) < size + 2:
                        buffer.extend(await reader.read(size+2-len(buffer)))

                    body_parts.append(buffer[:size])
                    buffer = buffer[size+2:]

                body = b"".join(body_parts).decode()


            # FIXED LENGTH
            elif content_length:
                content_length = int(content_length)
                body = bytearray(content_length)
                idx = 0

                if remaining:
                    body[:len(remaining)] = remaining
                    idx = len(remaining)

                while idx < content_length:
                    chunk = await reader.read(content_length-idx)
                    if not chunk:
                        break
                    body[idx:idx+len(chunk)] = chunk
                    idx += len(chunk)

                body = body.decode()

            # SAFE FALLBACK BUFFER
            elif fallback_buffer_size:
                body = bytearray(fallback_buffer_size)
                idx = 0

                if remaining:
                    if len(remaining) > fallback_buffer_size:
                        raise ValueError("Body too large")

                    body[:len(remaining)] = remaining
                    idx = len(remaining)

                while True:
                    chunk = await reader.read(READ_SIZE)
                    if not chunk:
                        break

                    if idx + len(chunk) > fallback_buffer_size:
                        raise ValueError("Body too large")

                    body[idx:idx+len(chunk)] = chunk
                    idx += len(chunk)

                body = body[:idx].decode()

            # DYNAMIC
            else:
                parts = [remaining.decode()] if remaining else []

                while True:
                    chunk = await reader.read(READ_SIZE)
                    if not chunk:
                        break
                    parts.append(chunk.decode())

                body = "".join(parts)

            return HttpResponse(status, headers, body)

        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except:
                    pass


    # --- retry loop ---
    for i in range(retries):
        try:
            return await asyncio.wait_for_ms(
                single_request(),
                int(timeout*1000)
            )

        except Exception as e:
            print("HTTP error:", e)
            if i == retries-1:
                return HttpResponse(500, None, str(e))

            await asyncio.sleep_ms(1000)

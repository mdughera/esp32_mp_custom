import uasyncio as asyncio
import uerrno
import json
import os

# MIME types for static files
_MIME = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json",
}

class HttpError(Exception):
    pass

class Request:
    def __init__(self):
        self.method = ""
        self.url = ""
        self.headers = {}
        self.args = {}
        self.read = None
        self.write = None
        self.close = None
        self.json = None

# ---------------- Safe write helper ----------------
async def write(request, data):
    try:
        if isinstance(data, str):
            data = data.encode('ISO-8859-1')
        await request.write(data)
    except OSError as e:
        if e.args[0] not in (uerrno.ECONNRESET,
                             getattr(uerrno, 'ENOTCONN', 128)):
            raise

# ---------------- File sending helper ----------------
async def send_file(request, filename, binary=False):
    mode = 'rb' if binary else 'r'
    try:
        with open(filename, mode) as f:
            while True:
                chunk = f.read(512)  # larger chunk for efficiency
                if not chunk:
                    break
                await write(request, chunk)
    except OSError as e:
        if e.args[0] == uerrno.ENOENT:
            raise HttpError(request, 404, "File Not Found")
        else:
            raise

# ---------------- Headers ----------------
async def send_headers(request, status_code=200, content_type=b"text/html", cache=True):
    await write(request, f"HTTP/1.1 {status_code}\r\n")
    await write(request, b"Content-Type: " + content_type + b"\r\n")
    if cache:
        await write(request, b"Cache-Control: max-age=31536000\r\n")
    else:
        await write(request, b"Cache-Control: no-cache, no-store, must-revalidate\r\n")
        await write(request, b"Pragma: no-cache\r\n")
        await write(request, b"Expires: 0\r\n")
    # CORS headers
    await write(request, b"Access-Control-Allow-Origin: *\r\n")
    await write(request, b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n")
    await write(request, b"Access-Control-Allow-Headers: Content-Type, Authorization\r\n\r\n")

# ---------------- HTTP Server ----------------
class HttpServer:
    STATIC_DIR = './html'

    def __init__(self, port=80, address='0.0.0.0', max_connections=10):
        self.port = port
        self.address = address
        self.routes = {}
        self._before_hooks = []
        self._after_hooks = []
        self._error_hooks = []

        # Connection management
        self._connections = set()
        self._lock = asyncio.Lock()
        self.max_connections = max_connections

    # -------- Hook registration --------
    def before(self, func):
        self._before_hooks.append(func)
        return func

    def after(self, func):
        self._after_hooks.append(func)
        return func

    def on_error(self, func):
        self._error_hooks.append(func)
        return func

    # Route decorator
    def route(self, path, methods=['GET']):
        def decorator(func):
            self.routes[path] = (func, methods)
            return func
        return decorator

    # ----------------- Request handler -----------------
    async def handle(self, reader, writer):
        conn_id = id(writer)

        # ---------------- Limit concurrent connections ----------------
        async with self._lock:
            if len(self._connections) >= self.max_connections:
                try:
                    await writer.awrite(
                        "HTTP/1.1 503 Service Unavailable\r\n"
                        "Content-Type: text/html\r\n\r\n"
                        "<h1>503 Too many connections</h1>"
                    )
                    await writer.aclose()
                except OSError:
                    pass
                return
            self._connections.add(conn_id)

        # ----------------- Process request -----------------
        request = Request()
        request.read = reader.read
        request.write = writer.awrite
        request.close = writer.aclose
        status_code = 200

        try:
            # ---------------- Read request line ----------------
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:
                return
            parts = line.decode().split()
            if len(parts) != 3:
                return
            request.method, full_url, _ = parts

            # ---------------- Query params ----------------
            if "?" in full_url:
                path, query = full_url.split("?", 1)
                request.url = path
                request.args = {k: v for k, v in
                                (p.split("=", 1) if "=" in p else (p,"")
                                 for p in query.split("&"))}
            else:
                request.url = full_url
                request.args = {}

            # ---------------- Headers ----------------
            while True:
                hline = await asyncio.wait_for(reader.readline(), 5)
                if not hline or hline == b'\r\n':
                    break
                hline = hline.decode()
                if ":" in hline:
                    k, v = hline.split(":", 1)
                    request.headers[k.strip()] = v.strip()

            # ---------------- POST JSON ----------------
            if request.method == "POST":
                length = int(request.headers.get("Content-Length", 0))
                body = b""
                while len(body) < length:
                    body += await asyncio.wait_for(reader.read(length - len(body)), 5)
                if body:
                    try:
                        request.json = json.loads(body)
                    except:
                        request.json = None

            # ---------------- BEFORE hooks ----------------
            for h in self._before_hooks:
                try:
                    h(request)
                except Exception as e:
                    print("before hook error:", e)

            # ---------------- OPTIONS ----------------
            if request.method == "OPTIONS":
                status_code = 204
                await send_headers(request, status_code=204, cache=False)
                return

            # ---------------- Route handler ----------------
            handler, methods = self.routes.get(request.url, (None, None))
            if handler is None:
                handler, methods = self.routes.get('*', (None, None))

            if handler and request.method in methods:
                result = await handler(request)

                if isinstance(result, (dict, list)):
                    await send_headers(request, content_type=b"application/json", cache=False)
                    await write(request, json.dumps(result))
                elif isinstance(result, str):
                    await send_headers(request, content_type=b"text/plain", cache=False)
                    await write(request, result)
            else:
                await self.serve_static(request)

        except HttpError as e:
            req, code, msg = e.args
            for h in self._error_hooks:
                try:
                    h(req, code, msg)
                except:
                    pass
            await send_headers(request, status_code=code, cache=False)
            await write(request, f"<h1>{msg}</h1>")

        except (OSError, asyncio.TimeoutError) as e:
            # expected network errors or client timeouts
            print("Client connection error:", e)

        except Exception as e:
            for h in self._error_hooks:
                try:
                    h(request, 500, str(e))
                except:
                    pass
            await send_headers(request, status_code=500, cache=False)
            await write(request, "<h1>500 Internal Server Error</h1>")

        finally:
            # ---------------- AFTER hooks ----------------
            for h in self._after_hooks:
                try:
                    h(request, status_code)
                except Exception as e:
                    print("after hook error:", e)
            try:
                await writer.aclose()
            except OSError:
                pass
            async with self._lock:
                self._connections.discard(conn_id)

    # ----------------- Static files -----------------
    async def serve_static(self, request):
        url = request.url
        if '.' not in url:
            url += '.html'

        path = f"{self.STATIC_DIR}/{url[1:]}"
        ext = '.' + url.split('.')[-1]
        content_type = _MIME.get(ext, 'application/octet-stream')
        binary = ext in ('.ico', '.png', '.jpg')

        try:
            os.stat(path)
            await send_headers(request, content_type=content_type.encode(), cache=True)
            await send_file(request, path, binary=binary)
        except OSError:
            await send_headers(request, status_code=404, cache=False)
            await write(request, "<h1>404 Not Found</h1>")

    # ----------------- Run server -----------------
    async def run(self):
        print("Starting HTTP server on", self.address, self.port)
        server = await asyncio.start_server(self.handle, self.address, self.port)
        return server

import socket
import threading
import select
from urllib.parse import urlparse
from datetime import datetime
import sys
from collections import OrderedDict

# Andric Nelson z5478729

# Proxy server settings
HOST = "0.0.0.0"
BUFFER_SIZE = 4096
ZID = "z5478729"        # Student ID
PORT = 0
TIMEOUT = 0
MAX_OBJECT_SIZE = 0
MAX_CACHE_SIZE = 0

# LRU cache
CACHE = OrderedDict()
CACHE_SIZE = 0          # current cache size

# Build HTTP error reponse with correct headers
# Creates a plain text error page with correct content length and status line
def build_error_response(status_code, reason, body):
    body_bytes = body.encode()
    headers = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return headers.encode() + body_bytes, len(body_bytes)

# Get current time and format corrrectly
# Returns a timestamp string in format for HTTP logs
# eg. [22/May/2025:10:17:24 +1000]
def get_log_timestamp():
    return datetime.now().strftime("[%d/%b/%Y:%H:%M:%S +1000]")

# Log each request with required details
# Logs client IP, port, cache status, full request line, HTTP status code, size
def log_request(client_addr, cache_status, method, url, version, status_code, response_size):
    ip, port = client_addr
    log_line = f"{ip} {port} {cache_status} {get_log_timestamp()} \"{method} {url} {version}\" {status_code} {response_size}"
    print(log_line)

# Send an error response to client, log it then close connection
# Builds HTTP error response, send to client, logs this and closes client connection
def send_error_and_close(client_socket, client_addr, method, url, version, code, reason, body):
    response, size = build_error_response(code, reason, body)
    try:
        client_socket.sendall(response)
    except Exception as e:
        print(f"Failed sending error response: {e}")
    finally:
        log_request(client_addr, "-", method, url, version, code, size)
        client_socket.close()

# Normalise URL for cache key
# Ensures non case sensitive and default port and paths are correct
def normalise_url(parsed):
    scheme = parsed.scheme.lower() if parsed.scheme else "http"
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    # Remove default ports for http/https
    if (scheme == "http" and (port is None or port == 80)) or (scheme == "https" and (port is None or port == 443)):
        port_part = ""
    else:
        port_part = f":{port if port else 80}"
    path = parsed.path if parsed.path else "/"
    if parsed.query:
        path += "?" + parsed.query
    return f"{scheme}://{host}{port_part}{path}"

# Handle HTTPS tunneling and CONNECT requests
# Establishes TCP tunnel between client and remote server for HTTPS traffic
# Forwards bytes back and forth until closed, also error handles
def handle_connect(client_socket, client_addr, url, method, version):
    try:
        host, port = url.split(":")
        port = int(port)

        # Only allows HTTPS on port 443
        if port != 443:
            send_error_and_close(client_socket, client_addr, method, url, version, 400, "Bad Request", "invalid port")
            return

        # Self loop error handling
        if host in ("127.0.0.1", "localhost"):
            send_error_and_close(client_socket, client_addr, method, url, version, 421, "Misdirected Request", "proxy address")
            return

        # Open tunnel to server
        with socket.create_connection((host, port), timeout=TIMEOUT) as tunnel:
            client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            sockets = [client_socket, tunnel]
            
            # traffic between client and server
            while True:
                r, _, _ = select.select(sockets, [], sockets)
                for s in r:
                    data = s.recv(BUFFER_SIZE)
                    if not data:
                        return
                    (tunnel if s is client_socket else client_socket).sendall(data)
        
        # Successful connect
        log_request(client_addr, "-", method, url, version, 200, 0)

    # Error handling
    except ValueError:
        send_error_and_close(client_socket, client_addr, method, url, version, 400, "Bad Request", "invalid port")
    except socket.gaierror:
        send_error_and_close(client_socket, client_addr, method, url, version, 502, "Bad Gateway", "could not resolve")
    except ConnectionRefusedError:
        send_error_and_close(client_socket, client_addr, method, url, version, 502, "Bad Gateway", "connection refused")
    except socket.timeout:
        send_error_and_close(client_socket, client_addr, method, url, version, 504, "Gateway Timeout", "timed out")
    except Exception:
        send_error_and_close(client_socket, client_addr, method, url, version, 502, "Bad Gateway", "closed unexpectedly")

# Forward HTTP requests with error handling, caching and via header. 
# Handle normal HTTP request (GET, POST, HEAD)
# Adds via header and forwards request to origin server
# Caches repsonse for GET if within size contraints
# streams the response back to client
def handle_http(client_socket, client_addr, method, url, version, headers, body):
    global CACHE, CACHE_SIZE
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            send_error_and_close(client_socket, client_addr, method, url, version, 400, "Bad Request", "no host")
            return

        # prevent self loop
        if host in ("127.0.0.1", "localhost"):
            send_error_and_close(client_socket, client_addr, method, url, version, 421, "Misdirected Request", "proxy address")
            return

        cache_key = normalise_url(parsed)
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        # Build cache key and check if GET request is in cache
        if method.upper() == "GET" and cache_key in CACHE:
            cached_response = CACHE[cache_key]
            client_socket.sendall(cached_response)
            log_request(client_addr, "H", method, url, version, 200, len(cached_response))
            return

        # Via headers
        new_headers = []
        via_added = False
        for h in headers:
            if h.lower().startswith("host:"):
                new_headers.append(f"Host: {host}")
            elif h.lower().startswith("connection:"):
                new_headers.append("Connection: close")
            elif h.lower().startswith("via:"):
                new_headers.append(f"{h}, 1.1 {ZID}")
                via_added = True
            elif not h.lower().startswith("proxy-connection:"):
                new_headers.append(h)
        if not via_added:
            new_headers.append(f"Via: 1.1 {ZID}")

        # Build full HTTP request
        forward = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(new_headers) + "\r\n\r\n"
        request_data = forward.encode() + body

        with socket.create_connection((host, port), timeout=TIMEOUT) as server_socket:
            server_socket.sendall(request_data)

            # Read response header
            response_buffer = b""
            while b"\r\n\r\n" not in response_buffer:
                chunk = server_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                response_buffer += chunk

            if b"\r\n\r\n" not in response_buffer:
                client_socket.sendall(response_buffer)
                log_request(client_addr, "-", method, url, version, 502, len(response_buffer))
                return

            header_part, remaining = response_buffer.split(b"\r\n\r\n", 1)
            header_lines = header_part.decode(errors="ignore").split("\r\n")

            # Add via header to response
            via_present = any(line.lower().startswith("via:") for line in header_lines)
            if via_present:
                header_lines = [
                    f"{line}, 1.1 {ZID}" if line.lower().startswith("via:") else line
                    for line in header_lines
                ]
            else:
                header_lines.append(f"Via: 1.1 {ZID}")

            modified_headers = "\r\n".join(header_lines).encode() + b"\r\n\r\n"
            full_response = modified_headers + remaining
            client_socket.sendall(full_response)

            # Stream body to client
            if method.upper() == "GET":
                while True:
                    chunk = server_socket.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    full_response += chunk
                    client_socket.sendall(chunk)

                object_body_size = len(full_response) - len(modified_headers)
                if object_body_size <= MAX_OBJECT_SIZE:
                    # Remove from cache until space
                    while CACHE_SIZE + object_body_size > MAX_CACHE_SIZE and CACHE:
                        old_key, old_val = CACHE.popitem(last=False)
                        evicted_size = len(old_val) - len(old_val.split(b"\r\n\r\n", 1)[0]) - 4  # exclude headers
                        CACHE_SIZE -= evicted_size

                    # room in cache add to it
                    if CACHE_SIZE + object_body_size <= MAX_CACHE_SIZE:
                        CACHE[cache_key] = full_response
                        CACHE.move_to_end(cache_key)
                        CACHE_SIZE += object_body_size
                        cache_status = "M"
                    else:
                        cache_status = "M"
                else:
                    cache_status = "M"
            else:
                while True:
                    chunk = server_socket.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    client_socket.sendall(chunk)
                cache_status = "-"

            log_request(client_addr, cache_status, method, url, version, 200, len(full_response))

    # network error handling
    except socket.gaierror:
        send_error_and_close(client_socket, client_addr, method, url, version, 502, "Bad Gateway", "could not resolve")
    except ConnectionRefusedError:
        send_error_and_close(client_socket, client_addr, method, url, version, 502, "Bad Gateway", "connection refused")
    except socket.timeout:
        send_error_and_close(client_socket, client_addr, method, url, version, 504, "Gateway Timeout", "timed out")
    except Exception:
        send_error_and_close(client_socket, client_addr, method, url, version, 502, "Bad Gateway", "closed unexpectedly")

# Handles a single client connection with support for Keep-Alive
# Reads HTTP request, parse it and routes to:
# - handle_connect() for CONNECT requests
# - handle_http() for other
# Supports multiple requests on the same connection
# request Keep-Alive
def handle_client(client_socket, client_addr):
    with client_socket:
        keep_alive = True
        while keep_alive:
            # Read until the full HTTP request headers are received
            request_data = b""
            while b"\r\n\r\n" not in request_data:
                chunk = client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    return
                request_data += chunk

            try:
                header_part, body_part = request_data.split(b"\r\n\r\n", 1)
            except ValueError:
                send_error_and_close(client_socket, client_addr, "UNKNOWN", "UNKNOWN", "HTTP/1.1", 400, "Bad Request", "invalid request")
                return

            header_lines = header_part.decode(errors="ignore").split("\r\n")
            request_line = header_lines[0]
            parts = request_line.split()
            if len(parts) < 3:
                send_error_and_close(client_socket, client_addr, "UNKNOWN", "UNKNOWN", "HTTP/1.1", 400, "Bad Request", "invalid request")
                return
            method, url, version = parts
            headers = header_lines[1:]

            # Handles HTTP CONNECT
            if method.upper() == "CONNECT":
                handle_connect(client_socket, client_addr, url, method, version)
                return

            # Read request body
            content_length = 0
            for h in headers:
                if h.lower().startswith("content-length:"):
                    try:
                        content_length = int(h.split(":")[1].strip())
                    except:
                        pass
            body = body_part
            while len(body) < content_length:
                chunk = client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                body += chunk

            handle_http(client_socket, client_addr, method, url, version, headers, body)

            # Check for keep alive persistency
            if any(h.lower() == "connection: keep-alive" for h in headers):
                keep_alive = True
            else:
                break

# Start proxy server
# Binds to inputted port and listens for connections
# Each connection is handled in seperate thread
def start_proxy():
    print(f"[*] Proxy listening on {HOST}:{PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(100)
        while True:
            client_sock, addr = server.accept()
            threading.Thread(target=handle_client, args=(client_sock, addr), daemon=True).start()

# Parsed command line arguments and starts server
if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: python3 proxy.py <port> <timeout> <max_object_size> <max_cache_size>")
        sys.exit(1)
    PORT = int(sys.argv[1])
    TIMEOUT = int(sys.argv[2])
    MAX_OBJECT_SIZE = int(sys.argv[3])
    MAX_CACHE_SIZE = int(sys.argv[4])

    # Input error handling
    if TIMEOUT <= 0 or MAX_OBJECT_SIZE <= 0 or MAX_CACHE_SIZE <= 0:
        print("Error: timeout, max_object_size, and max_cache_size must be strictly positive.")
        sys.exit(1)
    if MAX_CACHE_SIZE < MAX_OBJECT_SIZE:
        print("Error: max_cache_size must be at least equal to max_object_size.")
        sys.exit(1)

    start_proxy()

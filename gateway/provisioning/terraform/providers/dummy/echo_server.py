"""Tiny echo TCP server standing in for a rixi server in the dummy provider.

Picks an ephemeral port, writes it to the file given as argv[1], then echoes back received bytes
prefixed with b"DUMMY:" so tests can confirm the path reached this exact box.
"""
import socketserver
import sys


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(4096)
            if not data:
                break
            self.request.sendall(b"DUMMY:" + data)


def main():
    port_file = sys.argv[1]
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    with open(port_file, "w") as f:
        f.write(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    main()

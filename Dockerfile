# RIXI server in a container. Bundles the server component + Pixi and resolves its env at build.
#
#   docker build -t rixi-server .
#   # authenticated (recommended): give it a JWT public key or an AES pre-shared secret
#   docker run --rm -p 9000:9000 -e RIXI_KEY_SECRET=... rixi-server
#   # or a JWT verifier:
#   docker run --rm -p 9000:9000 -v $PWD/jwt.pub:/keys/jwt.pub -e RIXI_PUBLIC_KEY=/keys/jwt.pub rixi-server
#   # open/insecure (testing ONLY — arbitrary code execution for anyone who reaches the port):
#   docker run --rm -p 9000:9000 -e RIXI_ALLOW_INSECURE=1 rixi-server
FROM ghcr.io/prefix-dev/pixi:latest

WORKDIR /app/server
# Resolve the server environment first (cache layer), then add the sources.
COPY server/pixi.toml server/pixi.lock* ./
RUN pixi install || true
COPY server/ ./
COPY docker-entrypoint.sh /usr/local/bin/rixi-entrypoint
RUN chmod +x /usr/local/bin/rixi-entrypoint

ENV RIXI_PORT=9000
EXPOSE 9000

# Secure by default: the entrypoint refuses to serve on 0.0.0.0 without auth unless
# RIXI_ALLOW_INSECURE=1 is explicitly set. Extra flags pass straight through.
ENTRYPOINT ["/usr/local/bin/rixi-entrypoint"]

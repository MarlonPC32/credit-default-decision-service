FROM rust:1.91.1-slim-bookworm@sha256:8514999d4786ef12efe89239e86b3d0a021b94b9d35108c8efe6c79ca7dc1a65 AS build

WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export CARGO_HTTP_CAINFO=/run/secrets/build_ca; fi \
    && cargo build --release --locked

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

COPY --from=build /app/target/release/credit-default-gateway /usr/local/bin/credit-default-gateway
USER 10001:10001
EXPOSE 8080
CMD ["/usr/local/bin/credit-default-gateway"]

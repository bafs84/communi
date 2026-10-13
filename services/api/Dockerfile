FROM rust:1.91-slim AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev ca-certificates && rm -rf /var/lib/apt/lists/*
COPY Cargo.toml ./
COPY Cargo.lock ./
COPY src ./src
COPY migrations ./migrations
RUN cargo build --release --locked

FROM builder AS test
RUN cargo test --locked

FROM debian:trixie-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN mkdir -p /data && chmod 700 /data
COPY --from=builder /app/target/release/communi-backend-api /usr/local/bin/communi-backend-api
CMD ["communi-backend-api"]

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS native-build
COPY debian-packages.lock /tmp/debian-packages.lock
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261109T000000Z bookworm main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261109T000000Z bookworm-updates main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261109T000000Z bookworm-security main' \
      > /etc/apt/sources.list && \
    apt-get -o Acquire::Retries=3 update && \
    xargs apt-get -o Acquire::Retries=3 install -y --no-install-recommends < /tmp/debian-packages.lock && \
    rm -rf /var/lib/apt/lists/*
COPY native/screen.c /tmp/screen.c
RUN gcc -std=c11 -O3 -fPIC -shared -ffp-contract=off -fno-fast-math /tmp/screen.c -o /tmp/libscreen.so -lm

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
WORKDIR /app
COPY minidb /app/minidb
COPY --from=native-build /tmp/libscreen.so /app/minidb/libscreen.so
RUN mkdir -p /data && chmod 0777 /data
ENTRYPOINT ["python3", "-B", "-u", "-m", "minidb.server", "/data"]

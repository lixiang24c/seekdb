FROM gcc:12-bookworm@sha256:44482187aa3865ad1e2477dc8c56bae4fd8e071e7d99c5f44956effc7f7cc846 AS build
COPY native/ /src/native/
RUN cd /src/native/vendor && sha256sum -c SHA256SUMS
RUN gcc -O3 -DYYJSON_READER_DEPTH_LIMIT=128 -DYYJSON_WRITER_DEPTH_LIMIT=128 -c /src/native/vendor/yyjson/yyjson.c -o /tmp/json.o
RUN gcc -O3 -DUTF8PROC_STATIC -c /src/native/vendor/utf8proc/utf8proc.c -o /tmp/unicode.o
RUN gcc -std=c11 -O3 -ffp-contract=off -fno-fast-math -c /src/native/screen.c -o /tmp/screen.o
RUN g++ -std=c++17 -O3 -static-libstdc++ -static-libgcc -ffp-contract=off -fno-fast-math /src/native/queryable_log/main.cpp /tmp/json.o /tmp/unicode.o /tmp/screen.o -lm -lz -o /tmp/minivectordb-log
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
WORKDIR /app
COPY --from=build /tmp/minivectordb-log /app/minivectordb-log
COPY native/vendor/yyjson/LICENSE /usr/share/licenses/minivectordb/yyjson.txt
COPY native/vendor/utf8proc/LICENSE.md /usr/share/licenses/minivectordb/utf8proc.txt
RUN mkdir -p /data && chmod 777 /data
ENTRYPOINT ["/app/minivectordb-log"]

FROM ubuntu:24.04

# gcc (compile/run), lldb (step trace), clang-format (prettify), python3 (server)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev lldb python3 clang-format ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py trace_lldb.py index.html ./

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
ENV PORT=8000
EXPOSE 8000
# ponytail: no gunicorn/uvicorn — stdlib ThreadingHTTPServer is enough for a teaching tool
CMD ["python3", "server.py"]

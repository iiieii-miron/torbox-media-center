FROM python:3.10.12-slim-bookworm

WORKDIR /app
COPY requirements.txt .

# Install common dependencies
RUN pip install $(grep -v "fuse\|git+" requirements.txt)

# Install build dependencies
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends git pkg-config libfuse-dev gcc make && \
    rm -rf /var/lib/apt/lists/*

# Architecture-specific installations
ARG TARGETPLATFORM
RUN echo "Building for $TARGETPLATFORM" && \
    case "$TARGETPLATFORM" in \
        "linux/amd64"|"linux/arm64") \
            pip install fuse-python \
            ;; \
        "linux/arm/v7"|"linux/arm/v8") \
            pip install git+https://github.com/libfuse/python-fuse \
            ;; \
        *) \
            echo "Unsupported platform: $TARGETPLATFORM" && exit 1 \
            ;; \
    esac

COPY . .

ENV TORBOX_API_KEY=
ENV MOUNT_METHOD=strm
ENV MOUNT_PATH=/torbox

CMD ["python", "main.py"]
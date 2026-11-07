# Dockerfile
FROM python:3.13.13-alpine3.23

RUN apk add --no-cache \
    ffmpeg \
    nodejs \
    npm \
    git \
    curl \
    ca-certificates \
    alpine-sdk \
    python3-dev \
    py3-setuptools \
    libffi-dev \
    openssl-dev \
    tiff-dev \
    jpeg-dev \
    openjpeg-dev \
    zlib-dev \
    freetype-dev \
    lcms2-dev \
    libwebp-dev \
    tcl-dev \
    tk-dev \
    harfbuzz-dev \
    fribidi-dev \
    libimagequant-dev \
    libxcb-dev \
    libpng-dev

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Direct engines: streamrip (Qobuz/Tidal), gamdl (Apple Music), dotify-cli
# (Spotify web/Widevine), and pyplayready (a gamdl DRM dependency). Engine
# wheels are installed --no-deps because some declared caps are mutually
# exclusive; requirements.txt explicitly carries their tested runtime tree.
RUN pip install --no-cache-dir --no-deps \
        streamrip==2.1.0 \
        gamdl==3.9.1 \
        pyplayready==0.8.5 \
        dotify-cli==3.0.6

# Fail the build if any engine is missing or cannot start, rather than
# discovering it at request time.
RUN set -eux; \
    rip --help >/dev/null; \
    gamdl --help >/dev/null; \
    dotify --help >/dev/null; \
    python3 -c "from streamrip.config import Config, set_user_defaults"; \
    python3 -c "from pyplayready.crypto import key_wrap"; \
    python3 -c "from pywidevine.license_protocol_pb2 import LicenseRequest"; \
    python3 -c "from dotify.interface.enums import AudioQuality; assert AudioQuality.FLAC_MP4_24.value == 'flac-mp4-24'"; \
    python3 -c "import google.protobuf as p; assert p.__version__.startswith('6.33.'), p.__version__"

COPY dl_utils dl_utils
COPY handlers handlers
COPY langs.json ./
COPY main.py ./
COPY utils.py ./
COPY bot.py ./

# Avoid flac download, conditionally
ARG ENABLE_FLAC="1"
ENV ENABLE_FLAC=$ENABLE_FLAC

RUN echo "> ENABLE_FLAC : $ENABLE_FLAC"

RUN if [ "$ENABLE_FLAC" = "0" ]; then \
      echo "FLAC : disabled"; \
    else \
      echo "FLAC : enabled"; \
    fi

CMD [ "python", "-u", "main.py" ]
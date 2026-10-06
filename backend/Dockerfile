FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Системные зависимости WeasyPrint (pango, harfbuzz) и шрифты с кириллицей
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libharfbuzz-subset0 \
        libffi8 \
        libjpeg62-turbo \
        libopenjp2-7 \
        fontconfig \
        fonts-dejavu-core \
        fonts-noto-core \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости ставятся из pyproject.toml отдельным слоем, чтобы правки кода его не сбрасывали
ARG INSTALL_DEV=false
COPY pyproject.toml ./
RUN python -c "import sys, tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; deps = p['dependencies'] + (p['optional-dependencies']['dev'] if sys.argv[1] == 'true' else []); print('\n'.join(deps))" "$INSTALL_DEV" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Шрифты дайджеста доступны и через fontconfig, и по локальным путям для @font-face
COPY assets/fonts/ /usr/local/share/fonts/arxiv-digest/
RUN fc-cache -f

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data /beat \
    && chown -R app:app /data /beat

COPY --chown=app:app . .

USER app
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

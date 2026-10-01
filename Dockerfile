# One image, several entrypoints (api | ingest | worker | simulator | jobs) -> identical artefact in every env.
FROM python:3.11-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY --from=build /install /usr/local
COPY fleetpulse ./fleetpulse
COPY db ./db
COPY web ./web
COPY models ./models
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
EXPOSE 8000 8001
HEALTHCHECK --interval=10s --timeout=2s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz')" || exit 1
CMD ["uvicorn", "fleetpulse.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--http", "httptools", "--loop", "uvloop", "--workers", "2"]

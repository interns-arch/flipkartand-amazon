FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docs ./docs

RUN useradd --create-home appuser && mkdir -p /srv/app/data && chown appuser /srv/app/data
USER appuser

EXPOSE 8010
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]

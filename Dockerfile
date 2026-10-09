FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./
COPY sample_files/export_config.cfg ./sample_files/export_config.cfg

RUN mkdir -p data/permits
VOLUME /app/data

EXPOSE 8000
CMD ["python", "-W", "ignore::DeprecationWarning", "app.py"]

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    libsqlite3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /scripts /data /opt/seed

COPY data/all_markets_3yr.csv /opt/seed/all_markets_3yr.csv
COPY data/hourly_6coins_2yr.csv /opt/seed/hourly_6coins_2yr.csv
COPY data/tinkoff_ru_2yr.csv /opt/seed/tinkoff_ru_2yr.csv

COPY scripts/ /scripts/
RUN chmod +x /scripts/*.py

RUN python /scripts/build_db.py

COPY app/ /app/app/

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 3000

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PORT=3000

CMD ["/start.sh"]

FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# En la nube: escucha en todas las interfaces (exige APP_PASSWORD) y guarda datos en /data
ENV HOST=0.0.0.0 TRADING_DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8765
CMD ["python", "app/server.py"]

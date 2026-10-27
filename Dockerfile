FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY curator ./curator
ENV CURATOR_DATABASE=/data/curator.db
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "curator", "--host", "0.0.0.0", "--port", "8000"]

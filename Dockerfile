FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOST=0.0.0.0 \
    PORT=8787 \
    PYTHONUNBUFFERED=1

EXPOSE 8787

CMD ["python", "-m", "app.main", "serve"]

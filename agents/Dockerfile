FROM python:3.12-slim

WORKDIR /app

RUN groupadd --system prothesmia && \
    useradd --system --gid prothesmia --no-create-home prothesmia

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY statutes/ ./statutes/
COPY clock/ ./clock/
COPY main.py .

USER prothesmia

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]

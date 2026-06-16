FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app /app/app
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md

RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -e .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

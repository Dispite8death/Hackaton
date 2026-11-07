FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY audit/ audit/
COPY data/ data/
# отчёты и кэш выносятся в тома
VOLUME ["/app/reports", "/app/.cache"]
ENTRYPOINT ["python", "-m", "audit", "run"]
CMD ["--out", "reports/latest"]

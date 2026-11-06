# One image for every hop and the toggle board; ROLE selects which.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY app/requirements.txt .
RUN pip install -r requirements.txt
COPY app/demo ./demo
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "demo.main"]

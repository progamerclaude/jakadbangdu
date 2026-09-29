FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Kolkata TRADING_MODE=paper
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jakadbangdu ./jakadbangdu
# journal, playbook, strategy.json and logs live in /app/data: mount it as a volume
VOLUME /app/data
CMD ["python", "-m", "jakadbangdu", "run"]

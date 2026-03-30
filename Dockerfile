FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grid_bot.py backtest.py sweep.py ./

# Dossier pour persister logs et state
VOLUME ["/app/data"]

CMD ["python", "grid_bot.py"]

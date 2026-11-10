FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PORT=1000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 1000

CMD ["python", "bot.py"]

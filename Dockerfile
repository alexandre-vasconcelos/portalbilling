FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./

# Dependências de sistema para pyodbc + driver do SQL Server
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl gnupg2 apt-transport-https ca-certificates \
  && curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft.gpg \
  && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
  && apt-get update \
  && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev build-essential \
  && pip install --no-cache-dir -r requirements.txt \
  && apt-get autoremove -y \
  && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 3000

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:3000", "--timeout", "300", "app:app"]

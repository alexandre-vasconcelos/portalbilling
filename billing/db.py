import os
import logging
from contextlib import contextmanager
from typing import Iterator

import pyodbc


logger = logging.getLogger("portal_billing.sqlserver")


def build_connection_string() -> str:
  host = os.getenv("SQL_SERVER_HOST")
  port_str = os.getenv("SQL_SERVER_PORT")
  database = os.getenv("SQL_SERVER_DATABASE")
  user = os.getenv("SQL_SERVER_USER")
  password = os.getenv("SQL_SERVER_PASSWORD")
  encrypt_str = os.getenv("SQL_SERVER_ENCRYPT")
  trust_cert_str = os.getenv("SQL_SERVER_TRUST_CERT")
  driver = os.getenv("SQL_SERVER_DRIVER")

  required = {
      "SQL_SERVER_HOST": host,
      "SQL_SERVER_PORT": port_str,
      "SQL_SERVER_DATABASE": database,
      "SQL_SERVER_USER": user,
      "SQL_SERVER_PASSWORD": password,
  }
  missing = [name for name, value in required.items() if not value]
  if missing:
    raise RuntimeError(
        f"Variáveis de ambiente obrigatórias ausentes para conexão SQL Server: {', '.join(missing)}"
    )

  logger.info(
      "Montando connection string SQL Server: host=%s port=%s database=%s encrypt=%s trust_cert=%s driver=%s",
      host,
      port_str,
      database,
      encrypt_str,
      trust_cert_str,
      driver or "ODBC Driver 18 for SQL Server",
  )

  port = int(port_str)
  encrypt = str(encrypt_str or "").lower() == "true"
  trust_cert = str(trust_cert_str or "").lower() == "true"
  driver = driver or "ODBC Driver 18 for SQL Server"

  parts = [
      f"DRIVER={{{driver}}}",
      f"SERVER={host},{port}",
      f"DATABASE={database}",
      f"UID={user}",
      f"PWD={password}",
      f"Encrypt={'yes' if encrypt else 'no'}",
      f"TrustServerCertificate={'yes' if trust_cert else 'no'}",
  ]
  return ";".join(parts)


def _connect() -> pyodbc.Connection:
  """
  Cria uma nova conexão com o SQL Server.
  """
  conn_str = build_connection_string()
  try:
    conn = pyodbc.connect(conn_str)
    logger.info("Conexão com SQL Server estabelecida com sucesso.")
    return conn
  except pyodbc.Error as exc:
    logger.error("Erro ao conectar ao SQL Server: %s", exc)
    raise


@contextmanager
def get_connection() -> Iterator[pyodbc.Connection]:
  """
  Fornece uma conexão por operação, garantindo fechamento adequado.
  Uso:
    with get_connection() as conn:
      cursor = conn.cursor()
      ...
  """
  conn = _connect()
  try:
    yield conn
  finally:
    try:
      conn.close()
    except Exception:
      logger.warning("Falha ao fechar conexão com SQL Server", exc_info=True)


def init_db() -> None:
  """
  Apenas tenta abrir e fechar uma conexão para falhar cedo
  em caso de problema de configuração.
  """
  with get_connection():
    # A conexão é aberta e fechada pelo context manager.
    return

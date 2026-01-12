from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from .db import get_connection


logger = logging.getLogger("portal_billing.settings")


def _utc_now_naive() -> datetime:
  """
  Retorna o horário atual em UTC, sem informação de timezone
  (compatível com colunas DATETIME2 do SQL Server).
  """
  return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_sync_settings() -> None:
  """
  Garante que a tabela e o registro de configurações de sincronização existam.
  Cria com valores padrão caso ainda não existam.
  """
  with get_connection() as conn:
    cursor = conn.cursor()

    cursor.execute(
        """
        -- Garante tabela principal de billing
        IF OBJECT_ID('dbo.BillingRecords', 'U') IS NULL
        BEGIN
          CREATE TABLE dbo.BillingRecords (
            Id INT IDENTITY(1,1) PRIMARY KEY,
            TenancyOcid VARCHAR(255) NULL,
            Region VARCHAR(64) NULL,
            AvailabilityDomain VARCHAR(64) NULL,
            SkuPartNumber VARCHAR(64) NULL,
            TagsJson NVARCHAR(MAX) NULL,
            UsageDate DATETIME2 NOT NULL,
            ServiceName VARCHAR(255) NULL,
            ResourceName VARCHAR(255) NULL,
            Cost DECIMAL(18,4) NOT NULL,
            Currency VARCHAR(10) NULL,
            UsageQuantity DECIMAL(18,4) NULL,
            UsageUnit VARCHAR(32) NULL,
            CreatedAt DATETIME2 NOT NULL DEFAULT (SYSUTCDATETIME())
          );
        END

        -- Garante tabela de usuários
        IF OBJECT_ID('dbo.Users', 'U') IS NULL
        BEGIN
          CREATE TABLE dbo.Users (
            Id INT IDENTITY(1,1) PRIMARY KEY,
            Username VARCHAR(100) NOT NULL UNIQUE,
            PasswordHash VARCHAR(255) NOT NULL,
            Role VARCHAR(20) NOT NULL,
            IsActive BIT NOT NULL DEFAULT (1),
            CreatedAt DATETIME2 NOT NULL DEFAULT (SYSUTCDATETIME())
          );
        END

        IF OBJECT_ID('dbo.SyncSettings', 'U') IS NULL
        BEGIN
          CREATE TABLE dbo.SyncSettings (
            Id INT IDENTITY(1,1) PRIMARY KEY,
            AutoEnabled BIT NOT NULL DEFAULT (1),
            CronExpression VARCHAR(64) NOT NULL DEFAULT ('0 3 * * *'),
            TimeZone VARCHAR(64) NOT NULL DEFAULT ('UTC'),
            IsSyncRunning BIT NOT NULL DEFAULT (0),
            LastManualStart DATETIME2 NULL,
            LastManualEnd DATETIME2 NULL,
            DailyLimit DECIMAL(18,2) NULL,
            DailyLimitCurrency VARCHAR(10) NULL,
            MonthlyBudget DECIMAL(18,2) NULL,
            MonthlyBudgetCurrency VARCHAR(10) NULL,
            LastUsageDateUtc DATETIME2 NULL,
            LastRateLimitAtUtc DATETIME2 NULL
          );

          INSERT INTO dbo.SyncSettings (AutoEnabled, CronExpression, TimeZone, IsSyncRunning)
          VALUES (1, '0 3 * * *', 'UTC', 0);
        END

        -- Migração para tabelas antigas: adiciona colunas se não existirem
        IF OBJECT_ID('dbo.SyncSettings', 'U') IS NOT NULL
        BEGIN
          IF COL_LENGTH('dbo.SyncSettings', 'DailyLimit') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD DailyLimit DECIMAL(18,2) NULL;
          END;

          IF COL_LENGTH('dbo.SyncSettings', 'DailyLimitCurrency') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD DailyLimitCurrency VARCHAR(10) NULL;
          END;

          IF COL_LENGTH('dbo.SyncSettings', 'MonthlyBudget') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD MonthlyBudget DECIMAL(18,2) NULL;
          END;

          IF COL_LENGTH('dbo.SyncSettings', 'MonthlyBudgetCurrency') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD MonthlyBudgetCurrency VARCHAR(10) NULL;
          END;

          IF COL_LENGTH('dbo.SyncSettings', 'LastUsageDateUtc') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD LastUsageDateUtc DATETIME2 NULL;
          END;

          IF COL_LENGTH('dbo.SyncSettings', 'LastRateLimitAtUtc') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD LastRateLimitAtUtc DATETIME2 NULL;
          END;

          IF COL_LENGTH('dbo.SyncSettings', 'ForecastUntilDate') IS NULL
          BEGIN
            ALTER TABLE dbo.SyncSettings
            ADD ForecastUntilDate DATE NULL;
          END;
        END

        -- Garante tabela de histórico de execuções de sincronização
        IF OBJECT_ID('dbo.SyncRuns', 'U') IS NULL
        BEGIN
          CREATE TABLE dbo.SyncRuns (
            Id INT IDENTITY(1,1) PRIMARY KEY,
            IsManual BIT NOT NULL,
            StartedAt DATETIME2 NOT NULL,
            CompletedAt DATETIME2 NULL,
            Status VARCHAR(20) NOT NULL,
            ErrorMessage VARCHAR(4000) NULL,
            RecordsReturned INT NULL,
            RecordsInserted INT NULL,
            WindowStart DATETIME2 NULL,
            WindowEnd DATETIME2 NULL
          );
        END
        """
    )
    conn.commit()


def get_sync_settings() -> Dict[str, Any]:
  """
  Retorna as configurações atuais de sincronização.
  """
  ensure_sync_settings()

  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT TOP (1)
          Id,
          AutoEnabled,
          CronExpression,
          TimeZone,
          IsSyncRunning,
          LastManualStart,
          LastManualEnd,
          DailyLimit,
          DailyLimitCurrency,
          MonthlyBudget,
          MonthlyBudgetCurrency,
          LastUsageDateUtc,
          LastRateLimitAtUtc,
          ForecastUntilDate
        FROM dbo.SyncSettings
        ORDER BY Id
        """
    )
    row = cursor.fetchone()
    if not row:
      # Situação improvável, mas tratamos recriando defaults
      logger.warning("Nenhuma linha em SyncSettings; recriando registros padrão.")
      ensure_sync_settings()
      cursor.execute(
          """
          SELECT TOP (1)
            Id,
            AutoEnabled,
            CronExpression,
          TimeZone,
          IsSyncRunning,
          LastManualStart,
          LastManualEnd,
          DailyLimit,
          DailyLimitCurrency,
          MonthlyBudget,
          MonthlyBudgetCurrency,
          LastUsageDateUtc,
          LastRateLimitAtUtc,
          ForecastUntilDate
          FROM dbo.SyncSettings
          ORDER BY Id
          """
      )
      row = cursor.fetchone()

    return {
        "id": row.Id,
        "auto_enabled": bool(row.AutoEnabled),
        "cron_expression": row.CronExpression,
        "timezone": row.TimeZone,
        "is_sync_running": bool(row.IsSyncRunning),
        "last_manual_start": row.LastManualStart,
        "last_manual_end": row.LastManualEnd,
        "daily_limit": float(row.DailyLimit) if getattr(row, "DailyLimit", None) is not None else None,
        "daily_limit_currency": getattr(row, "DailyLimitCurrency", None),
        "monthly_budget": float(row.MonthlyBudget) if getattr(row, "MonthlyBudget", None) is not None else None,
        "monthly_budget_currency": getattr(row, "MonthlyBudgetCurrency", None),
        "last_usage_date_utc": getattr(row, "LastUsageDateUtc", None),
        "last_rate_limit_at_utc": getattr(row, "LastRateLimitAtUtc", None),
        "forecast_until_date": getattr(row, "ForecastUntilDate", None),
    }


def update_sync_settings(
    auto_enabled: bool,
    cron_expression: str,
    timezone_str: str,
    daily_limit: float | None,
    daily_limit_currency: str | None,
    monthly_budget: float | None,
    monthly_budget_currency: str | None,
    forecast_until_date: Any | None,
) -> None:
  """
  Atualiza AutoEnabled, CronExpression e TimeZone.
  """
  ensure_sync_settings()
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE TOP (1) dbo.SyncSettings
        SET AutoEnabled = ?, CronExpression = ?, TimeZone = ?, DailyLimit = ?, DailyLimitCurrency = ?, MonthlyBudget = ?, MonthlyBudgetCurrency = ?, ForecastUntilDate = ?
        """,
        (
            1 if auto_enabled else 0,
            cron_expression,
            timezone_str,
            daily_limit,
            daily_limit_currency,
            monthly_budget,
            monthly_budget_currency,
            forecast_until_date,
        ),
    )
    conn.commit()


def mark_sync_running(is_running: bool, is_manual: bool) -> None:
  """
  Marca o estado de execução de sincronização. Quando manual, também atualiza
  os campos de início/fim da última execução manual.
  """
  ensure_sync_settings()
  with get_connection() as conn:
    cursor = conn.cursor()

    now = _utc_now_naive()

    if is_manual:
      if is_running:
        cursor.execute(
            """
            UPDATE TOP (1) dbo.SyncSettings
            SET IsSyncRunning = 1,
                LastManualStart = ?
            """,
            (now,),
        )
      else:
        cursor.execute(
            """
            UPDATE TOP (1) dbo.SyncSettings
            SET IsSyncRunning = 0,
                LastManualEnd = ?
            """,
            (now,),
        )
    else:
      cursor.execute(
          """
          UPDATE TOP (1) dbo.SyncSettings
          SET IsSyncRunning = ?
          """,
          (1 if is_running else 0,),
      )

    conn.commit()


def is_sync_running() -> bool:
  """
  Indica se há uma sincronização em andamento (manual ou automática).
  """
  settings = get_sync_settings()
  return bool(settings["is_sync_running"])


def update_last_usage_date(last_usage: datetime | None) -> None:
  """
  Atualiza a data do último dia de uso sincronizado na tabela de configurações.
  """
  ensure_sync_settings()

  value = None
  if last_usage is not None:
    if last_usage.tzinfo is not None:
      value = last_usage.astimezone(timezone.utc).replace(tzinfo=None)
    else:
      value = last_usage

  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE TOP (1) dbo.SyncSettings
        SET LastUsageDateUtc = ?
        """,
        (value,),
    )
    conn.commit()


def update_last_rate_limit_at(rate_limit_at: datetime | None) -> None:
  """
  Atualiza o horário da última vez em que a OCI retornou 429 (TooManyRequests).
  """
  ensure_sync_settings()

  value = None
  if rate_limit_at is not None:
    if rate_limit_at.tzinfo is not None:
      value = rate_limit_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
      value = rate_limit_at

  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE TOP (1) dbo.SyncSettings
        SET LastRateLimitAtUtc = ?
        """,
        (value,),
    )
    conn.commit()


def reset_billing_data() -> None:
  """
  Remove todos os registros de billing e histórico de sincronização,
  e reseta os marcadores em SyncSettings. Use com cuidado.
  """
  ensure_sync_settings()
  with get_connection() as conn:
    cursor = conn.cursor()

    # Remove dados de billing e histórico
    cursor.execute("DELETE FROM dbo.BillingRecords;")
    cursor.execute("DELETE FROM dbo.SyncRuns;")

    # Reseta flags e marcadores em SyncSettings
    cursor.execute(
        """
        UPDATE TOP (1) dbo.SyncSettings
        SET IsSyncRunning = 0,
            LastManualStart = NULL,
            LastManualEnd = NULL,
            LastUsageDateUtc = NULL,
            LastRateLimitAtUtc = NULL
        """
    )

    conn.commit()

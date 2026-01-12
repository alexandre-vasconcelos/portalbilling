from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Tuple, Optional
import logging

from .db import get_connection


logger = logging.getLogger("portal_billing.repository")


def ensure_billing_records_extra_columns() -> None:
  """
  Garante que as colunas adicionais existam em BillingRecords
  (para armazenar quantidade de uso, unidade e dimensões extras de FinOps).

  Em conjunto com `insert_billing_records`, permite usar colunas
  estendidas quando o schema suporta; se der erro de permissão ou
  de DDL, `insert_billing_records` faz fallback para o modo legado.
  """
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        IF OBJECT_ID('dbo.BillingRecords', 'U') IS NOT NULL
        BEGIN
          IF COL_LENGTH('dbo.BillingRecords', 'UsageQuantity') IS NULL
          BEGIN
            ALTER TABLE dbo.BillingRecords
            ADD UsageQuantity DECIMAL(18,4) NULL;
          END;

          IF COL_LENGTH('dbo.BillingRecords', 'UsageUnit') IS NULL
          BEGIN
            ALTER TABLE dbo.BillingRecords
            ADD UsageUnit VARCHAR(32) NULL;
          END;

          IF COL_LENGTH('dbo.BillingRecords', 'Region') IS NULL
          BEGIN
            ALTER TABLE dbo.BillingRecords
            ADD Region VARCHAR(64) NULL;
          END;

          IF COL_LENGTH('dbo.BillingRecords', 'AvailabilityDomain') IS NULL
          BEGIN
            ALTER TABLE dbo.BillingRecords
            ADD AvailabilityDomain VARCHAR(64) NULL;
          END;

          IF COL_LENGTH('dbo.BillingRecords', 'SkuPartNumber') IS NULL
          BEGIN
            ALTER TABLE dbo.BillingRecords
            ADD SkuPartNumber VARCHAR(64) NULL;
          END;

          IF COL_LENGTH('dbo.BillingRecords', 'TagsJson') IS NULL
          BEGIN
            ALTER TABLE dbo.BillingRecords
            ADD TagsJson NVARCHAR(MAX) NULL;
          END;
        END
        """
    )
    conn.commit()


def _normalize_usage_date(dt: datetime, fallback: datetime) -> datetime:
  """
  Normaliza o datetime para armazenamento/ comparação:
  - Se vier como string ISO, converte para datetime.
  - Converte para UTC.
  - Normaliza para o início do dia (00:00 UTC), para evitar duplicação
    por diferenças de horário dentro do mesmo dia.
  - Se for None ou inválido, usa o fallback (normalmente `now`).
  """
  if dt is None:
    dt = fallback

  # Trata strings ISO (ex.: retornos da API/SDK)
  if isinstance(dt, str):
    try:
      # Trata possível sufixo 'Z'
      cleaned = dt.replace("Z", "+00:00")
      dt_parsed = datetime.fromisoformat(cleaned)
      dt = dt_parsed
    except Exception:
      dt = fallback

  # Se for apenas date, transforma em datetime
  if isinstance(dt, date) and not isinstance(dt, datetime):
    dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)

  if isinstance(dt, datetime):
    if dt.tzinfo is not None:
      dt = dt.astimezone(timezone.utc)
    # Normaliza para início do dia em UTC
    normalized = datetime(dt.year, dt.month, dt.day)
    return normalized

  # Fallback final: usa o fallback normalizado
  if isinstance(fallback, datetime):
    fb = fallback
  else:
    fb = datetime.now(timezone.utc)
  return datetime(fb.year, fb.month, fb.day)


def get_last_usage_day() -> Optional[datetime]:
  """
  Retorna o maior UsageDate registrado em BillingRecords (UTC),
  ou None se ainda não houver dados.
  """
  try:
    with get_connection() as conn:
      cursor = conn.cursor()
      cursor.execute(
          """
          SELECT MAX(UsageDate) AS LastUsageDate
          FROM BillingRecords
          """
      )
      row = cursor.fetchone()
      return row.LastUsageDate if row and row.LastUsageDate is not None else None
  except Exception as exc:  # pragma: no cover - proteção extra contra falhas de driver
    logger.error("Erro ao obter último dia de uso em BillingRecords: %s", exc)
    return None


def insert_billing_records(records: List[Dict[str, Any]]) -> Tuple[int, int]:
  """
  Insere registros de billing.

  - Tenta primeiro usar o modo \"estendido\", com colunas adicionais (UsageQuantity,
    Region, etc).
  - Se não conseguir ajustar o schema (ex.: falta de permissão de ALTER TABLE),
    faz o insert em modo legado, compatível com a versão antiga do portal.
  """
  if not records:
    return 0, 0

  # Tenta garantir colunas extras; se falhar, seguimos em modo legado
  use_extended_schema = True
  try:
    ensure_billing_records_extra_columns()
  except Exception:
    # Por segurança, se algo escapar do tratamento interno, voltamos ao legado.
    use_extended_schema = False

  now = datetime.now(timezone.utc)
  with get_connection() as conn:
    cursor = conn.cursor()

    # Modo legado: mantém comportamento próximo da versão antiga,
    # sem depender de colunas extras ou upsert por chave completa.
    if not use_extended_schema:
      normalized_rows: List[Tuple[Any, ...]] = []
      for r in records:
        usage_raw = r.get("usageDate")
        usage_dt = _normalize_usage_date(usage_raw, now)

        normalized_rows.append(
            (
                r.get("tenancyOcid"),
                usage_dt,
                r.get("serviceName"),
                r.get("resourceName"),
                float(r.get("cost") or 0),
                r.get("currency") or "USD",
                now,
            )
        )

      inserted_count = 0
      for (
          tenancy,
          usage_dt,
          service,
          resource,
          cost,
          currency,
          created_at,
      ) in normalized_rows:
        cursor.execute(
            """
            IF NOT EXISTS (
              SELECT 1
              FROM BillingRecords
              WHERE ISNULL(TenancyOcid, '') = ISNULL(?, '')
                AND UsageDate = ?
                AND ISNULL(ServiceName, '') = ISNULL(?, '')
                AND ISNULL(ResourceName, '') = ISNULL(?, '')
            )
            INSERT INTO BillingRecords (
              TenancyOcid,
              UsageDate,
              ServiceName,
              ResourceName,
              Cost,
              Currency,
              CreatedAt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenancy,
                usage_dt,
                service,
                resource,
                tenancy,
                usage_dt,
                service,
                resource,
                cost,
                currency,
                created_at,
            ),
        )
        if cursor.rowcount:
          inserted_count += 1

      conn.commit()
      logger.info(
          "Registros de billing inseridos em modo legado: %d",
          inserted_count,
      )
      return inserted_count, 0

    # Modo estendido: usa colunas extras e upsert completo.
    normalized_rows_ext: List[Tuple[Any, ...]] = []
    for r in records:
      usage_raw = r.get("usageDate")
      usage_dt = _normalize_usage_date(usage_raw, now)

      normalized_rows_ext.append(
          (
              r.get("tenancyOcid"),
              r.get("region"),
              r.get("availabilityDomain"),
              r.get("skuPartNumber"),
              usage_dt,
              r.get("serviceName"),
              r.get("resourceName"),
              float(r.get("cost") or 0),
              r.get("currency") or "USD",
              float(r.get("usageQuantity") or 0),
              (r.get("usageUnit") or None),
              r.get("tagsJson") or None,
              now,
          )
      )

    inserted_count = 0
    updated_count = 0

    for (
        tenancy,
        region,
        availability_domain,
        sku_part_number,
        usage_dt,
        service,
        resource,
        cost,
        currency,
        usage_quantity,
        usage_unit,
        tags_json,
        created_at,
    ) in normalized_rows_ext:
      cursor.execute(
          """
          UPDATE BillingRecords
          SET Cost = ?,
              Currency = ?,
              UsageQuantity = ?,
              UsageUnit = ?,
              TagsJson = ?,
              Region = ?,
              AvailabilityDomain = ?,
              SkuPartNumber = ?,
              CreatedAt = ?
          WHERE ISNULL(TenancyOcid, '') = ISNULL(?, '')
            AND UsageDate = ?
            AND ISNULL(ServiceName, '') = ISNULL(?, '')
            AND ISNULL(ResourceName, '') = ISNULL(?, '')
            AND ISNULL(Region, '') = ISNULL(?, '')
            AND ISNULL(AvailabilityDomain, '') = ISNULL(?, '')
            AND ISNULL(SkuPartNumber, '') = ISNULL(?, '')
          """,
          (
              cost,
              currency,
              usage_quantity,
              usage_unit,
              tags_json,
              region,
              availability_domain,
              sku_part_number,
              created_at,
              tenancy,
              usage_dt,
              service,
              resource,
              region,
              availability_domain,
              sku_part_number,
          ),
      )
      if cursor.rowcount:
        updated_count += 1
        continue

      cursor.execute(
          """
          INSERT INTO BillingRecords (
            TenancyOcid,
            Region,
            AvailabilityDomain,
            SkuPartNumber,
            UsageDate,
            ServiceName,
            ResourceName,
            Cost,
            Currency,
            UsageQuantity,
            UsageUnit,
            TagsJson,
            CreatedAt
          )
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
              tenancy,
              region,
              availability_domain,
              sku_part_number,
              usage_dt,
              service,
              resource,
              cost,
              currency,
              usage_quantity,
              usage_unit,
              tags_json,
              created_at,
          ),
      )
      if cursor.rowcount:
        inserted_count += 1

    conn.commit()

  logger.info(
      "Registros de billing - inseridos: %d, atualizados: %d",
      inserted_count,
      updated_count,
  )
  return inserted_count, updated_count


def get_dashboard_summary(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[str, Any]:
  with get_connection() as conn:
    cursor = conn.cursor()

    # Último dia com dados em BillingRecords (para exibir "dados até" no dashboard)
    last_usage_dt = get_last_usage_day()
    last_usage_date_str = (
        last_usage_dt.date().isoformat() if last_usage_dt is not None else None
    )

    now_utc = datetime.utcnow()
    # Determina o intervalo principal (período atual)
    if start_date and end_date:
      # Usa intervalo informado [start, end]
      period_start = datetime(start_date.year, start_date.month, start_date.day)
      period_end = datetime(end_date.year, end_date.month, end_date.day) + timedelta(
          days=1
      )
    else:
      # Padrão: últimos 30 dias
      period_end = now_utc
      period_start = now_utc - timedelta(days=30)

    # Intervalo anterior, com mesma duração e imediatamente anterior ao atual.
    previous_period_start = period_start - (period_end - period_start)
    previous_period_end = period_start

    cursor.execute(
        """
        SELECT ISNULL(SUM(Cost), 0) AS TotalCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        """,
        (period_start, period_end),
    )
    row = cursor.fetchone()
    total_period = row[0] if row and row[0] is not None else 0

    # Total do período anterior (mesma duração)
    cursor.execute(
        """
        SELECT ISNULL(SUM(Cost), 0) AS TotalCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        """,
        (previous_period_start, previous_period_end),
    )
    row = cursor.fetchone()
    previous_total_period = row[0] if row and row[0] is not None else 0

    cursor.execute(
        """
        SELECT TOP 5 ServiceName, SUM(Cost) AS TotalCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        GROUP BY ServiceName
        ORDER BY TotalCost DESC
        """,
        (period_start, period_end),
    )
    top_services = []
    for s in cursor.fetchall():
      top_services.append(
          {
              "ServiceName": s.ServiceName,
              "TotalCost": float(s.TotalCost or 0),
          }
      )

    cursor.execute(
        """
        SELECT
          CAST(UsageDate AS date) AS UsageDay,
          SUM(Cost) AS TotalCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        GROUP BY CAST(UsageDate AS date)
        ORDER BY UsageDay ASC
        """,
        (period_start, period_end),
    )
    by_day: List[Dict[str, Any]] = []
    daily_limit = None
    days_over_limit: List[Dict[str, Any]] = []
    for d in cursor.fetchall():
      cost_val = float(d.TotalCost or 0)
      usage_day = d.UsageDay if hasattr(d, "UsageDay") else d[0]
      day_iso = usage_day.isoformat() if hasattr(usage_day, "isoformat") else str(
          usage_day
      )
      by_day.append(
          {
              "UsageDay": day_iso,
              "TotalCost": cost_val,
          }
      )

    cursor.execute(
        """
        SELECT MAX(CreatedAt) AS LastSyncAt
        FROM BillingRecords
        """
    )
    row = cursor.fetchone()
    last_sync = row[0] if row else None

    # Custo mensal por mês dentro do período
    cursor.execute(
        """
        SELECT
          YEAR(UsageDate) AS UsageYear,
          MONTH(UsageDate) AS UsageMonth,
          SUM(Cost) AS TotalCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        GROUP BY YEAR(UsageDate), MONTH(UsageDate)
        ORDER BY UsageYear, UsageMonth
        """,
        (period_start, period_end),
    )
    monthly = []
    for m in cursor.fetchall():
      monthly.append(
          {
              "Year": m.UsageYear,
              "Month": m.UsageMonth,
              "TotalCost": float(m.TotalCost or 0),
          }
      )

    # Quantidade de recursos "ativos" (distinct ResourceName com custo > 0 no período)
    cursor.execute(
        """
        SELECT COUNT(DISTINCT ISNULL(ResourceName, '')) AS ActiveResources
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
          AND Cost > 0
        """,
        (period_start, period_end),
    )
    row = cursor.fetchone()
    active_resources = int(row[0] or 0)

    # Recursos por serviço (ex.: Compute 5, Object Storage 3, etc.)
    cursor.execute(
        """
        SELECT
          ServiceName,
          COUNT(DISTINCT ISNULL(ResourceName, '')) AS ResourceCount
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
          AND Cost > 0
        GROUP BY ServiceName
        ORDER BY ResourceCount DESC
        """,
        (period_start, period_end),
    )
    resources_by_service: List[Dict[str, Any]] = []
    for r in cursor.fetchall():
      resources_by_service.append(
          {
              "ServiceName": r.ServiceName,
              "ResourceCount": int(r.ResourceCount or 0),
          }
      )

    # Moeda predominante (assumimos 1 moeda; pegamos a mais frequente)
    cursor.execute(
        """
        SELECT TOP 1 Currency, COUNT(*) AS Cnt
        FROM BillingRecords
        WHERE Currency IS NOT NULL
        GROUP BY Currency
        ORDER BY Cnt DESC
        """
    )
    row = cursor.fetchone()
    currency_code = row.Currency if row and row.Currency else "USD"
    if currency_code.upper() == "BRL":
      currency_symbol = "R$"
    elif currency_code.upper() == "USD":
      currency_symbol = "US$"
    else:
      currency_symbol = currency_code

    # Carrega limite diário (se configurado) e identifica dias que ultrapassam esse limite
    from .settings import get_sync_settings  # import local para evitar ciclos

    settings = get_sync_settings()
    daily_limit = settings.get("daily_limit")
    if daily_limit is not None:
      for day in by_day:
        if day["TotalCost"] > float(daily_limit):
          days_over_limit.append(day)

    # Orçamento mensal e projeção
    monthly_budget = settings.get("monthly_budget")
    month_cost = 0.0
    month_forecast = None
    month_percent = None
    month_over_budget = False

    # Cálculo de custo do mês atual em separado (sem depender do filtro de período)
    month_start = datetime(now_utc.year, now_utc.month, 1)
    if now_utc.month == 12:
      next_month_start = datetime(now_utc.year + 1, 1, 1)
    else:
      next_month_start = datetime(now_utc.year, now_utc.month + 1, 1)

    cursor.execute(
        """
        SELECT ISNULL(SUM(Cost), 0) AS MonthCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        """,
        (month_start, next_month_start),
    )
    row = cursor.fetchone()
    month_cost = float(row[0] or 0)

    # Custo do mês anterior
    if now_utc.month == 1:
      prev_month_start = datetime(now_utc.year - 1, 12, 1)
    else:
      prev_month_start = datetime(now_utc.year, now_utc.month - 1, 1)
    prev_month_end = month_start

    cursor.execute(
        """
        SELECT ISNULL(SUM(Cost), 0) AS MonthCost
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        """,
        (prev_month_start, prev_month_end),
    )
    row = cursor.fetchone()
    previous_month_cost = float(row[0] or 0)

    # Projeção simples: média diária * número de dias do mês
    days_elapsed = max((now_utc.date() - month_start.date()).days + 1, 1)
    total_days_in_month = (next_month_start.date() - month_start.date()).days
    avg_per_day = month_cost / days_elapsed if days_elapsed else 0
    month_forecast = avg_per_day * total_days_in_month

    if monthly_budget is not None and monthly_budget > 0:
      month_percent = (month_cost / float(monthly_budget)) * 100
      month_over_budget = month_forecast > float(monthly_budget)

    # Cálculo do gasto do dia atual (sempre independente do filtro de período)
    cursor.execute(
        """
        SELECT ISNULL(SUM(Cost), 0) AS TodayCost
        FROM BillingRecords
        WHERE CAST(UsageDate AS date) = CAST(GETUTCDATE() AS date)
        """
    )
    row = cursor.fetchone()
    today_cost = float(row[0] or 0)

    # Cálculo do gasto do dia anterior (ontem)
    cursor.execute(
        """
        SELECT ISNULL(SUM(Cost), 0) AS YesterdayCost
        FROM BillingRecords
        WHERE CAST(UsageDate AS date) = CAST(DATEADD(day, -1, GETUTCDATE()) AS date)
        """
    )
    row = cursor.fetchone()
    yesterday_cost = float(row[0] or 0)

    # Cálculos de variação percentual
    def _pct_change(current: float, previous: float) -> Optional[float]:
      if previous is None or previous == 0:
        return None
      try:
        return ((current - previous) / float(previous)) * 100.0
      except Exception:
        return None

    today_vs_yesterday_pct = _pct_change(today_cost, yesterday_cost)
    period_vs_previous_pct = _pct_change(float(total_period or 0), float(previous_total_period or 0))
    month_vs_previous_pct = _pct_change(month_cost, previous_month_cost)

    # Comparação anual: ano base (fim do período) x ano anterior, mês a mês
    annual_rows: List[Any] = []
    try:
      period_end_inclusive = (period_end - timedelta(days=1)).date()
      base_year = period_end_inclusive.year
      prev_year = base_year - 1

      year_range_start = datetime(prev_year, 1, 1)
      year_range_end = datetime(base_year + 1, 1, 1)

      cursor.execute(
          """
          SELECT
            YEAR(UsageDate) AS UsageYear,
            MONTH(UsageDate) AS UsageMonth,
            SUM(Cost) AS TotalCost
          FROM BillingRecords
          WHERE UsageDate >= ? AND UsageDate < ?
          GROUP BY YEAR(UsageDate), MONTH(UsageDate)
          ORDER BY UsageYear, UsageMonth
          """,
          (year_range_start, year_range_end),
      )
      annual_rows = list(cursor.fetchall() or [])
    except Exception:
      annual_rows = []

  # Projeção customizada até data definida em configurações (se houver)
  custom_forecast = None
  custom_forecast_until = None
  forecast_until_date = settings.get("forecast_until_date")
  if forecast_until_date is not None:
    try:
      # forecast_until_date pode vir como date ou datetime
      if isinstance(forecast_until_date, datetime):
        target_date = forecast_until_date.date()
      else:
        target_date = forecast_until_date

      if target_date > now_utc.date():
        days_total_to_target = (target_date - month_start.date()).days
        if days_total_to_target <= 0:
          days_total_to_target = days_elapsed
        custom_forecast = avg_per_day * days_total_to_target
        custom_forecast_until = target_date.isoformat()
    except Exception:
      custom_forecast = None
      custom_forecast_until = None

  # Monta estrutura de comparação anual a partir de annual_rows
  annual_comparison = None
  if annual_rows:
    # Recupera ano base / anterior a partir dos dados, se possível.
    years_seen = sorted({int(getattr(r, "UsageYear", r[0])) for r in annual_rows})
    if years_seen:
      base_year = years_seen[-1]
      prev_year = base_year - 1
      base_months = [0.0] * 12
      prev_months = [0.0] * 12
      for r in annual_rows:
        y = int(getattr(r, "UsageYear", r[0]))
        m = int(getattr(r, "UsageMonth", r[1]))
        total = float(getattr(r, "TotalCost", r[2]) or 0)
        idx = max(0, min(11, m - 1))
        if y == base_year:
          base_months[idx] = total
        elif y == prev_year:
          prev_months[idx] = total
      months = []
      for i in range(12):
        months.append(
            {
                "Month": i + 1,
                "BaseYearCost": base_months[i],
                "PrevYearCost": prev_months[i],
            }
        )
      annual_comparison = {
          "baseYear": base_year,
          "prevYear": prev_year,
          "months": months,
      }

  return {
      "totalPeriod": float(total_period or 0),
      "previousTotalPeriod": float(previous_total_period or 0),
      "topServices": top_services,
      "byDay": by_day,
      "monthly": monthly,
      "activeResources": active_resources,
      "resourcesByService": resources_by_service,
      "currencyCode": currency_code,
      "currencySymbol": currency_symbol,
      "dailyLimit": float(daily_limit) if daily_limit is not None else None,
      "daysOverLimit": days_over_limit,
      "todayCost": float(today_cost or 0.0),
      "todayOverLimit": bool(
          daily_limit is not None and today_cost > float(daily_limit)
      ),
      "yesterdayCost": float(yesterday_cost or 0.0),
      "yesterdayOverLimit": bool(
          daily_limit is not None and yesterday_cost > float(daily_limit)
      ),
      "monthCost": float(month_cost or 0.0),
      "previousMonthCost": float(previous_month_cost or 0.0),
      "monthlyBudget": float(monthly_budget) if monthly_budget is not None else None,
      "monthPercent": float(month_percent) if month_percent is not None else None,
      "monthForecast": float(month_forecast) if month_forecast is not None else None,
      "monthOverBudget": month_over_budget,
      "todayVsYesterdayPercent": float(today_vs_yesterday_pct) if today_vs_yesterday_pct is not None else None,
      "periodVsPreviousPercent": float(period_vs_previous_pct) if period_vs_previous_pct is not None else None,
      "monthVsPreviousPercent": float(month_vs_previous_pct) if month_vs_previous_pct is not None else None,
      "customForecast": float(custom_forecast) if custom_forecast is not None else None,
      "customForecastUntil": custom_forecast_until,
      "lastSyncAt": last_sync.isoformat() if last_sync else None,
      "periodStart": period_start.date().isoformat(),
      # period_end é exclusivo; subtrai 1 dia para exibição
      "periodEnd": (period_end - timedelta(days=1)).date().isoformat(),
      "dataUntilUtc": last_usage_date_str,
      "annualComparison": annual_comparison,
  }


def get_billing_by_day_and_service(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
  """
  Retorna dados agregados por dia (UTC) e serviço, no mesmo intervalo usado pelo dashboard.
  """
  ensure_billing_records_extra_columns()

  with get_connection() as conn:
    cursor = conn.cursor()

    now_utc = datetime.utcnow()
    if start_date and end_date:
      period_start = datetime(start_date.year, start_date.month, start_date.day)
      period_end = datetime(end_date.year, end_date.month, end_date.day) + timedelta(
          days=1
      )
    else:
      period_end = now_utc
      period_start = now_utc - timedelta(days=30)

    cursor.execute(
        """
        SELECT
          CAST(UsageDate AS date) AS UsageDay,
          ServiceName,
          SUM(Cost) AS TotalCost,
          ISNULL(SUM(UsageQuantity), 0) AS TotalQuantity,
          MAX(Currency) AS Currency,
          MAX(UsageUnit) AS UsageUnit
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        GROUP BY CAST(UsageDate AS date), ServiceName
        ORDER BY UsageDay ASC, ServiceName ASC
        """,
        (period_start, period_end),
    )

    rows: List[Dict[str, Any]] = []
    for r in cursor.fetchall():
      day = r.UsageDay if hasattr(r, "UsageDay") else r[0]
      rows.append(
          {
              "UsageDay": day.isoformat() if hasattr(day, "isoformat") else str(day),
              "ServiceName": getattr(r, "ServiceName", None),
              "TotalCost": float(getattr(r, "TotalCost", 0) or 0),
              "TotalQuantity": float(getattr(r, "TotalQuantity", 0) or 0),
              "Currency": getattr(r, "Currency", None),
              "UsageUnit": getattr(r, "UsageUnit", None),
          }
      )
    return rows


def get_billing_detailed(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
  """
  Retorna as linhas detalhadas de BillingRecords, com todas as colunas
  disponíveis, no intervalo solicitado.
  """
  ensure_billing_records_extra_columns()

  with get_connection() as conn:
    cursor = conn.cursor()

    now_utc = datetime.utcnow()
    if start_date and end_date:
      period_start = datetime(start_date.year, start_date.month, start_date.day)
      period_end = datetime(end_date.year, end_date.month, end_date.day) + timedelta(
          days=1
      )
    else:
      period_end = now_utc
      period_start = now_utc - timedelta(days=30)

    cursor.execute(
        """
        SELECT
          TenancyOcid,
          Region,
          AvailabilityDomain,
          SkuPartNumber,
          UsageDate,
          ServiceName,
          ResourceName,
          Cost,
          Currency,
          UsageQuantity,
          UsageUnit,
          TagsJson,
          CreatedAt
        FROM BillingRecords
        WHERE UsageDate >= ? AND UsageDate < ?
        ORDER BY UsageDate ASC, ServiceName ASC, ResourceName ASC
        """,
        (period_start, period_end),
    )

    rows: List[Dict[str, Any]] = []

    for r in cursor.fetchall():
      usage_dt = getattr(r, "UsageDate", None) if hasattr(r, "UsageDate") else r[5]
      usage_iso = (
          usage_dt.isoformat() if hasattr(usage_dt, "isoformat") else str(usage_dt)
      )
      rows.append(
          {
              "TenancyOcid": getattr(r, "TenancyOcid", None),
              "Region": getattr(r, "Region", None),
              "AvailabilityDomain": getattr(r, "AvailabilityDomain", None),
              "SkuPartNumber": getattr(r, "SkuPartNumber", None),
              "UsageDate": usage_iso,
              "ServiceName": getattr(r, "ServiceName", None),
              "ResourceName": getattr(r, "ResourceName", None),
              "Cost": float(getattr(r, "Cost", 0) or 0),
              "Currency": getattr(r, "Currency", None),
              "UsageQuantity": float(getattr(r, "UsageQuantity", 0) or 0),
              "UsageUnit": getattr(r, "UsageUnit", None),
              "TagsJson": getattr(r, "TagsJson", None),
              "CreatedAt": getattr(r, "CreatedAt", None).isoformat()
              if hasattr(r, "CreatedAt") and getattr(r, "CreatedAt", None)
              else None,
          }
      )
    return rows

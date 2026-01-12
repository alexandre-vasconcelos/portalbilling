from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .oci_client import fetch_latest_billing_data
from .repository import insert_billing_records, get_last_usage_day
from .settings import (
    get_sync_settings,
    mark_sync_running,
    update_last_rate_limit_at,
    update_last_usage_date,
)
from .sync_runs import start_sync_run, finish_sync_run


_scheduler: Optional[BackgroundScheduler] = None


def _sync_billing_job():
  settings = get_sync_settings()

  if settings["is_sync_running"]:
    print("Sincronização já em andamento; job automático será ignorado.")
    return

  if not settings["auto_enabled"]:
    print("Sincronização automática desabilitada; job automático não será executado.")
    return

  now_utc = datetime.now(timezone.utc)

  # Checa se houve rate limit recente gravado em banco
  last_rate_limit_at = settings.get("last_rate_limit_at_utc")
  if last_rate_limit_at is not None:
    try:
      last_rate_limit_dt = last_rate_limit_at.replace(tzinfo=timezone.utc)
    except Exception:
      last_rate_limit_dt = None
  else:
    last_rate_limit_dt = None

  # Define janela de coleta com base no último dia já registrado no banco.
  # Isso evita depender apenas de "agora - 2 dias" e garante que dias
  # futuros serão capturados automaticamente.
  last_usage = get_last_usage_day()
  if last_usage is not None:
    # UsageDate é UTC sem timezone; ancoramos em UTC.
    start_day = last_usage.date()
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=timezone.utc)
  else:
    # Sem dados ainda: pega uma janela curta recente.
    start = now_utc - timedelta(days=2)

  # Se a última tentativa sofreu rate limit, respeita um intervalo mínimo
  if last_rate_limit_dt is not None:
    cooldown = timedelta(hours=1)
    if now_utc - last_rate_limit_dt < cooldown:
      next_try = last_rate_limit_dt + cooldown
      print(
          "Sincronização automática ignorada: OCI retornou 429 recentemente; "
          f"aguardando até {next_try.isoformat()} para tentar novamente."
      )
      return

  # Registra início da execução
  run_id = start_sync_run(is_manual=False, window_start=start, window_end=now_utc)
  records_count: Optional[int] = None
  inserted_count: Optional[int] = None
  run_status = "success"
  run_error: Optional[str] = None

  print("Iniciando sincronização automática de billing OCI (Python)...")
  mark_sync_running(True, is_manual=False)
  try:
    records, error_message = fetch_latest_billing_data(start=start, end=now_utc)
    records_count = len(records) if records is not None else 0

    if error_message:
      run_status = "error"
      run_error = error_message

      if "TooManyRequests" in error_message or "429" in error_message:
        update_last_rate_limit_at(now_utc)
      print(
          "Erro na sincronização automática de billing OCI: "
          f"{error_message}"
      )
    elif records:
      inserted, updated = insert_billing_records(records)
      inserted_count = inserted
      run_status = "success"

      # Atualiza último dia de uso sincronizado
      last_usage = get_last_usage_day()
      if last_usage is not None:
        update_last_usage_date(last_usage)

      print(
          "Sincronização automática concluída. "
          f"Registros retornados: {len(records)}. "
          f"Novos registros inseridos: {inserted}. "
          f"Registros existentes atualizados: {updated}."
      )
    else:
      print("Sincronização automática concluída. Nenhum registro retornado.")
  except Exception as exc:
    run_status = "error"
    run_error = str(exc)
    print(f"Erro na sincronização automática de billing: {exc}")
  finally:
    mark_sync_running(False, is_manual=False)
    finish_sync_run(
        run_id,
        status=run_status,
        records_returned=records_count,
        records_inserted=inserted_count,
        error_message=run_error,
    )


def refresh_billing_schedule():
  """
  (Re)configura o job de sincronização automática com base nas configurações do banco.
  """
  global _scheduler

  settings = get_sync_settings()
  cron_expr = settings["cron_expression"]
  tz = settings["timezone"] or "UTC"

  if _scheduler is None:
    _scheduler = BackgroundScheduler(timezone=tz)
    _scheduler.start()

  scheduler = _scheduler

  # Remove job antigo, se existir
  try:
    scheduler.remove_job("billing_sync_job")
  except Exception:
    pass

  if not settings["auto_enabled"]:
    print(
        "Sincronização automática de billing desabilitada nas configurações; job não agendado."
    )
    return

  try:
    trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
  except ValueError as exc:
    # Se a expressão CRON estiver inválida, não derruba o worker:
    # registra mensagem e usa um valor padrão seguro (a cada hora).
    print(
        f"Expressão CRON inválida em SyncSettings ('{cron_expr}'): {exc}. "
        "Usando valor padrão '0 * * * *'."
    )
    cron_expr = "0 * * * *"
    trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)

  scheduler.add_job(
      _sync_billing_job,
      trigger,
      id="billing_sync_job",
      replace_existing=True,
  )
  print(
      f"Job de sincronização de billing (Python) agendado com cron '{cron_expr}' (timezone {tz})."
  )


def start_scheduler():
  """
  Inicializa o scheduler e agenda o job conforme as configurações atuais.
  """
  refresh_billing_schedule()

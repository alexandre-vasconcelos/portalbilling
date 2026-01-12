from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from .db import get_connection


logger = logging.getLogger("portal_billing.sync_runs")


def _utc_now_naive() -> datetime:
  """
  Retorna o horário atual em UTC, sem informação de timezone.
  """
  return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
  if dt is None:
    return None
  if dt.tzinfo is not None:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
  return dt


def start_sync_run(
    is_manual: bool,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> int:
  """
  Insere um registro de início de execução de sincronização e retorna o Id.
  """
  with get_connection() as conn:
    cursor = conn.cursor()
    now = _utc_now_naive()
    ws = _to_naive_utc(window_start)
    we = _to_naive_utc(window_end)

    cursor.execute(
        """
        INSERT INTO dbo.SyncRuns (
          IsManual,
          StartedAt,
          Status,
          WindowStart,
          WindowEnd
        )
        OUTPUT INSERTED.Id
        VALUES (?, ?, ?, ?, ?)
        """,
        (1 if is_manual else 0, now, "running", ws, we),
    )
    row = cursor.fetchone()
    conn.commit()

    run_id = int(row[0]) if row and row[0] is not None else 0
    logger.info(
        "SyncRun iniciado: id=%s, is_manual=%s, window_start=%s, window_end=%s",
        run_id,
        is_manual,
        ws,
        we,
    )
    return run_id


def finish_sync_run(
    run_id: int,
    status: str,
    records_returned: Optional[int] = None,
    records_inserted: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
  """
  Atualiza o registro de execução de sincronização com o resultado final.
  """
  if not run_id:
    return

  with get_connection() as conn:
    cursor = conn.cursor()
    now = _utc_now_naive()
    cursor.execute(
        """
        UPDATE dbo.SyncRuns
        SET CompletedAt = ?, Status = ?, ErrorMessage = ?, RecordsReturned = ?, RecordsInserted = ?
        WHERE Id = ?
        """,
        (now, status, error_message, records_returned, records_inserted, run_id),
    )
    conn.commit()

  logger.info(
      "SyncRun finalizado: id=%s, status=%s, records_returned=%s, records_inserted=%s",
      run_id,
      status,
      records_returned,
      records_inserted,
  )


def list_recent_sync_runs(limit: int = 20) -> List[Dict[str, Any]]:
  """
  Retorna as últimas execuções de sincronização (manuais e automáticas),
  ordenadas da mais recente para a mais antiga.
  """
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT TOP ({int(limit)})
          Id,
          IsManual,
          StartedAt,
          CompletedAt,
          Status,
          ErrorMessage,
          RecordsReturned,
          RecordsInserted,
          WindowStart,
          WindowEnd
        FROM dbo.SyncRuns
        ORDER BY StartedAt DESC
        """
    )
    runs: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
      runs.append(
          {
              "id": row.Id,
              "is_manual": bool(row.IsManual),
              "started_at": row.StartedAt,
              "completed_at": row.CompletedAt,
              "status": row.Status,
              "error_message": row.ErrorMessage,
              "records_returned": row.RecordsReturned,
              "records_inserted": row.RecordsInserted,
              "window_start": row.WindowStart,
              "window_end": row.WindowEnd,
          }
      )
    return runs


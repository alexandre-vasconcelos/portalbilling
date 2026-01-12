import os
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("portal_billing.oci")


def _to_utc_midnight(dt: datetime) -> datetime:
  """
  Ajusta um datetime para meia-noite em UTC (00:00:00, frações zero),
  conforme exigência da Usage API da OCI.
  """
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  else:
    dt = dt.astimezone(timezone.utc)
  return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def _build_oci_config() -> Dict[str, Any]:
  """
  Monta o dicionário de configuração para o SDK `oci` a partir das variáveis de ambiente.
  """
  config = {
      "user": os.getenv("OCI_USER_OCID"),
      "tenancy": os.getenv("OCI_TENANCY_OCID"),
      "region": os.getenv("OCI_REGION"),
      "fingerprint": os.getenv("OCI_FINGERPRINT"),
      "key_file": os.getenv("OCI_PRIVATE_KEY_PATH"),
      "pass_phrase": os.getenv("OCI_KEY_PASSPHRASE"),
  }

  missing = [k for k, v in config.items() if k != "pass_phrase" and not v]
  if missing:
    raise RuntimeError(
        "Variáveis de ambiente OCI ausentes: "
        + ", ".join(missing)
    )

  return config


def _fetch_from_oci_usage_api(
    start: datetime | None = None,
    end: datetime | None = None,
) -> List[Dict[str, Any]]:
  """
  Consulta a Usage API da OCI usando o SDK oficial (`oci`) e
  retorna os registros normalizados para o portal.
  """
  try:
    import oci
    from oci.usage_api.models import RequestSummarizedUsagesDetails
  except ImportError:
    logger.error(
        "Biblioteca 'oci' não instalada. "
        "Adicione 'oci' ao requirements.txt e reinstale as dependências."
    )
    return []

  config = _build_oci_config()

  # Permite ajustar o timeout de leitura via env.
  # Formato: segundos (ex.: 120). Usamos (connect=10s, read=valor).
  read_timeout_env = os.getenv("OCI_USAGE_READ_TIMEOUT", "120")
  try:
    read_timeout = max(30.0, float(read_timeout_env))
  except ValueError:
    read_timeout = 120.0

  usage_client = oci.usage_api.UsageapiClient(
      config,
      timeout=(10.0, read_timeout),
  )

  now = datetime.now(timezone.utc)
  if end is None:
    end = now
  if start is None:
    # Janela padrão: últimos 2 dias
    start = end - timedelta(days=2)

  # Usage API exige precisão de data: horas/minutos/segundos/frações = 0
  start = _to_utc_midnight(start)
  end = _to_utc_midnight(end)

  details = RequestSummarizedUsagesDetails(
      tenant_id=config["tenancy"],
      granularity="DAILY",
      query_type="COST",
      time_usage_started=start,
      time_usage_ended=end,
      # A Usage API permite no máximo 4 chaves em groupBy.
      # Aqui usamos apenas serviço, região e SKU.
      group_by=[
          "service",
          "region",
          "skuPartNumber",
      ],
  )

  logger.info(
      "Consultando Usage API da OCI: tenancy=%s, region=%s, janela=%s -> %s",
      config["tenancy"],
      config["region"],
      start.isoformat(),
      end.isoformat(),
  )

  response = usage_client.request_summarized_usages(details)

  normalized: List[Dict[str, Any]] = []

  # Cada item representa o custo agregado para um serviço/recurso em um dia
  for item in getattr(response.data, "items", []):
    try:
      service_name = getattr(item, "service", None)
      resource_name = getattr(item, "resource_name", None)
      region = getattr(item, "region", None)
      availability_domain = getattr(item, "availability_domain", None)
      sku_part_number = getattr(item, "sku_part_number", None)
      raw_tags = getattr(item, "tags", None)
      # Para granularidade DAILY, ancoramos o registro no dia de início
      # do intervalo de uso, não no fim, para que "Gasto de hoje"
      # reflita o dia correto no dashboard.
      usage_date = getattr(item, "time_usage_started", None) or getattr(
          item, "time_usage_ended", now
      )
      # Alguns registros podem trazer computed_amount = None; tratamos como 0
      raw_cost = getattr(item, "computed_amount", None)
      if raw_cost is None:
        raw_cost = 0.0
      cost = float(raw_cost or 0.0)
      # Quantidade de uso e unidade (quando disponíveis)
      raw_quantity = getattr(item, "computed_quantity", None)
      if raw_quantity is None:
        raw_quantity = getattr(item, "usage", None)
      usage_quantity = float(raw_quantity or 0.0)
      usage_unit = getattr(item, "unit", None)

      currency = getattr(item, "currency", None) or getattr(
          item, "currency_code", "USD"
      )

      tags_json = None
      if raw_tags is not None:
        try:
          tags_json = json.dumps(raw_tags, ensure_ascii=False)
        except Exception:
          tags_json = None

      normalized.append(
          {
              "tenancyOcid": config["tenancy"],
              "usageDate": usage_date,
              "serviceName": service_name,
              "resourceName": resource_name,
              "cost": cost,
              "currency": currency,
              "usageQuantity": usage_quantity,
              "usageUnit": usage_unit,
              "region": region,
              "availabilityDomain": availability_domain,
              "skuPartNumber": sku_part_number,
              "tagsJson": tags_json,
          }
      )
    except Exception as exc:  # pragma: no cover - apenas log defensivo
      logger.warning("Falha ao normalizar item de usage OCI: %s", exc)

  logger.info("Registros de billing retornados da OCI: %d", len(normalized))
  return normalized


def fetch_latest_billing_data(
    start: datetime | None = None,
    end: datetime | None = None,
    days: int = 2,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
  """
  Ponto central para coletar dados de billing na OCI.

  Consulta sempre a Usage API com o SDK oficial.
  Não há mais dados fake; em caso de falha, devolvemos lista vazia
  e uma mensagem de erro amigável (quando possível).
  """
  try:
    # Se não passar start/end, usa janela relativa em dias
    now = datetime.now(timezone.utc)
    if end is None:
      end = now
    if start is None:
      start = end - timedelta(days=days)

    # Garante ordenação correta
    if start > end:
      start, end = end, start

    # Ajusta o fim para incluir o dia final completo.
    # A Usage API considera o intervalo como [start, end) para granularidade DAILY,
    # então estendemos em +1 dia.
    end = end + timedelta(days=1)

    # Delay opcional entre chamadas para reduzir chance de 429.
    delay_env = os.getenv("OCI_USAGE_SLEEP_SECONDS", "0")
    try:
      delay_between_calls = max(0.0, float(delay_env))
    except ValueError:
      delay_between_calls = 0.0

    # Quebramos o intervalo em janelas de até 90 dias.
    # A Usage API com granularidade DAILY permite no máximo 93 dias;
    # usamos 90 para ficar com margem de segurança.
    records: List[Dict[str, Any]] = []

    current_start = start
    while current_start < end:
      current_end = current_start + timedelta(days=90)
      if current_end > end:
        current_end = end

      chunk = _fetch_from_oci_usage_api(start=current_start, end=current_end)
      if chunk:
        records.extend(chunk)

      # Se ainda houver janelas futuras, aguarda um pouco antes da próxima chamada.
      if delay_between_calls > 0 and current_end < end:
        time.sleep(delay_between_calls)

      current_start = current_end

    return records, None
  except Exception as exc:
    raw_msg = str(exc)
    logger.error("Erro ao consultar billing na OCI: %s", raw_msg)

    # Trata explicitamente erro de rate limit (TooManyRequests / 429)
    if "TooManyRequests" in raw_msg or "'status': 429" in raw_msg:
      friendly = (
          "A OCI retornou erro 429 (TooManyRequests) na Usage API. "
          "Reduza a frequência da sincronização automática ou aguarde "
          "algum tempo antes de tentar novamente."
      )
    else:
      friendly = f"Erro ao consultar billing na OCI: {raw_msg}"

    return [], friendly

import os
import csv
import io
import threading
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response

from billing.db import init_db
from billing.repository import (
    get_dashboard_summary,
    insert_billing_records,
    get_last_usage_day,
    get_billing_by_day_and_service,
)
from billing.oci_client import fetch_latest_billing_data
from billing.scheduler import start_scheduler, refresh_billing_schedule
from billing.settings import (
    ensure_sync_settings,
    get_sync_settings,
    update_sync_settings,
    mark_sync_running,
    update_last_rate_limit_at,
    update_last_usage_date,
)
from billing.sync_runs import start_sync_run, finish_sync_run, list_recent_sync_runs
from billing.users import (
    authenticate_user,
    create_user,
    get_user_by_id,
    list_users,
    update_user,
)


load_dotenv()


def _run_manual_sync_background(start: datetime, end: datetime, run_id: int) -> None:
  """
  Executa a sincronização manual em background, para não travar
  a requisição HTTP. Atualiza SyncRuns e marca início/fim via
  SyncSettings.
  """
  now_utc = end
  records_count = None
  inserted_count = None
  run_status = "success"
  run_error = None

  try:
    records, error_message = fetch_latest_billing_data(start=start, end=now_utc)
    records_count = len(records) if records is not None else 0

    if error_message:
      run_status = "error"
      run_error = error_message

      if "TooManyRequests" in error_message or "429" in error_message:
        update_last_rate_limit_at(now_utc)
    elif records:
      inserted, updated = insert_billing_records(records)
      inserted_count = inserted
      run_status = "success"

      # Atualiza último dia de uso sincronizado
      last_usage = get_last_usage_day()
      if last_usage is not None:
        update_last_usage_date(last_usage)
    else:
      run_status = "success"
  except Exception as exc:  # pragma: no cover
    run_status = "error"
    run_error = str(exc)
  finally:
    # Libera flag de execução e finaliza histórico
    try:
      mark_sync_running(False, is_manual=True)
    finally:
      finish_sync_run(
          run_id,
          status=run_status,
          records_returned=records_count,
          records_inserted=inserted_count,
          error_message=run_error,
      )


def create_app():
  app = Flask(
      __name__,
      static_folder="public",
      static_url_path="/static",
      template_folder="templates",
  )

  app.secret_key = os.getenv("SESSION_SECRET", "dev-secret")
  session_lifetime_minutes = int(os.getenv("SESSION_LIFETIME_MINUTES", "120"))
  app.permanent_session_lifetime = timedelta(minutes=session_lifetime_minutes)

  # Inicializa conexão (testa apenas) para falhar cedo se houver problema
  init_db()
  ensure_sync_settings()

  # Rotas de autenticação
  @app.route("/", methods=["GET"])
  def index():
    if session.get("user"):
      return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

  @app.route("/login", methods=["GET", "POST"])
  def login():
    if request.method == "GET":
      if session.get("user"):
        return redirect(url_for("dashboard"))
      return render_template("login.html", error=None)

    username = request.form.get("username")
    password = request.form.get("password")

    user = authenticate_user(username, password)
    if user:
      session.permanent = True
      session["user"] = {
          "id": user["id"],
          "username": user["username"],
          "role": user["role"],
      }
      return redirect(url_for("dashboard"))

    return render_template(
        "login.html",
        error="Credenciais inválidas ou usuário inativo.",
    )

  @app.route("/logout", methods=["GET"])
  def logout():
    session.clear()
    return redirect(url_for("login"))

  # Decorador simples de proteção de rota
  def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
      if not session.get("user"):
        return redirect(url_for("login"))
      return view(*args, **kwargs)

    return wrapped

  @app.route("/dashboard", methods=["GET"])
  @login_required
  def dashboard():
    def _parse_date(value: str):
      try:
        return datetime.strptime(value, "%Y-%m-%d").date()
      except Exception:
        return None

    start_str = request.args.get("start_date") or ""
    end_str = request.args.get("end_date") or ""

    # Padrão: mês atual (do primeiro dia até hoje)
    if not start_str and not end_str:
      today = datetime.utcnow().date()
      start_date = today.replace(day=1)
      end_date = today
    else:
      start_date = _parse_date(start_str) if start_str else None
      end_date = _parse_date(end_str) if end_str else None

    if start_date and end_date and end_date < start_date:
      # Se o usuário inverter as datas, corrigimos
      start_date, end_date = end_date, start_date

    summary = get_dashboard_summary(start_date, end_date)
    return render_template(
        "dashboard.html",
        user=session.get("user"),
        summary=summary,
        title="Dashboard",
    )

  @app.route("/dashboard/monitor", methods=["GET"])
  @login_required
  def dashboard_monitor():
    def _parse_date(value: str):
      try:
        return datetime.strptime(value, "%Y-%m-%d").date()
      except Exception:
        return None

    start_str = request.args.get("start_date") or ""
    end_str = request.args.get("end_date") or ""

    # Padrão: mês atual (do primeiro dia até hoje)
    if not start_str and not end_str:
      today = datetime.utcnow().date()
      start_date = today.replace(day=1)
      end_date = today
    else:
      start_date = _parse_date(start_str) if start_str else None
      end_date = _parse_date(end_str) if end_str else None

    if start_date and end_date and end_date < start_date:
      start_date, end_date = end_date, start_date

    summary = get_dashboard_summary(start_date, end_date)
    return render_template(
        "dashboard_monitor.html",
        user=session.get("user"),
        summary=summary,
        title="Dashboard · Modo monitor",
    )

  @app.route("/export/billing.csv", methods=["GET"])
  @login_required
  def export_billing_csv():
    """
    Exporta um CSV com dados agregados por dia (UTC) e serviço,
    usando o mesmo intervalo de datas do dashboard.
    """

    def _parse_date(value: str):
      try:
        return datetime.strptime(value, "%Y-%m-%d").date()
      except Exception:
        return None

    start_str = request.args.get("start_date") or ""
    end_str = request.args.get("end_date") or ""

    start_date = _parse_date(start_str) if start_str else None
    end_date = _parse_date(end_str) if end_str else None

    if start_date and end_date and end_date < start_date:
      start_date, end_date = end_date, start_date

    rows = get_billing_by_day_and_service(start_date, end_date)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["UsageDayUTC", "ServiceName", "TotalCost", "Currency", "TotalQuantity", "UsageUnit"]
    )
    for r in rows:
      writer.writerow(
          [
              r.get("UsageDay"),
              r.get("ServiceName") or "",
              f"{r.get('TotalCost', 0):.4f}",
              r.get("Currency") or "",
              f"{r.get('TotalQuantity', 0):.4f}",
              r.get("UsageUnit") or "",
          ]
      )

    csv_data = output.getvalue()
    output.close()

    filename = "billing_export.csv"
    if start_date and end_date:
      filename = f"billing_{start_date.isoformat()}_to_{end_date.isoformat()}.csv"

    response = make_response(csv_data)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response

  # Decoradores de permissão
  def admin_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
      user = session.get("user")
      if not user:
        return redirect(url_for("login"))
      if user.get("role") != "admin":
        return redirect(url_for("dashboard"))
      return view(*args, **kwargs)

    return wrapped

  # Gestão de usuários (apenas admin)
  @app.route("/users", methods=["GET"])
  @admin_required
  def users_list():
    users = list_users()
    return render_template(
        "users_list.html",
        user=session.get("user"),
        users=users,
    )

  @app.route("/users/new", methods=["GET", "POST"])
  @admin_required
  def users_create():
    if request.method == "GET":
      return render_template(
          "users_form.html",
          user=session.get("user"),
          mode="create",
          form_data={"username": "", "role": "user", "is_active": True},
          error=None,
      )

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    password_confirm = request.form.get("password_confirm", "")
    role = request.form.get("role", "user")
    is_active = request.form.get("is_active") == "on"

    if not username or not password:
      return render_template(
          "users_form.html",
          user=session.get("user"),
          mode="create",
          form_data={
              "username": username,
              "role": role,
              "is_active": is_active,
          },
          error="Usuário e senha são obrigatórios.",
      )

    if password != password_confirm:
      return render_template(
          "users_form.html",
          user=session.get("user"),
          mode="create",
          form_data={
              "username": username,
              "role": role,
              "is_active": is_active,
          },
          error="A confirmação de senha não confere.",
      )

    if role not in ("admin", "user"):
      role = "user"

    try:
      create_user(username, password, role, is_active)
    except Exception as exc:  # Ex.: violação de unique
      return render_template(
          "users_form.html",
          user=session.get("user"),
          mode="create",
          form_data={
              "username": username,
              "role": role,
              "is_active": is_active,
          },
          error=f"Erro ao criar usuário: {exc}",
      )

    return redirect(url_for("users_list"))

  @app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
  @admin_required
  def users_edit(user_id: int):
    existing = get_user_by_id(user_id)
    if not existing:
      return redirect(url_for("users_list"))

    if request.method == "GET":
      form_data = {
          "username": existing["username"],
          "role": existing["role"],
          "is_active": existing["is_active"],
      }
      return render_template(
          "users_form.html",
          user=session.get("user"),
          mode="edit",
          form_data=form_data,
          error=None,
      )

    role = request.form.get("role", "user")
    is_active = request.form.get("is_active") == "on"
    new_password = request.form.get("password") or None
    password_confirm = request.form.get("password_confirm") or None

    if new_password or password_confirm:
      if new_password != password_confirm:
        form_data = {
            "username": existing["username"],
            "role": role,
            "is_active": is_active,
        }
        return render_template(
            "users_form.html",
            user=session.get("user"),
            mode="edit",
            form_data=form_data,
            error="A confirmação de senha não confere.",
        )

    if role not in ("admin", "user"):
      role = "user"

    try:
      update_user(user_id, role, is_active, new_password)
    except Exception as exc:
      form_data = {
          "username": existing["username"],
          "role": role,
          "is_active": is_active,
      }
      return render_template(
          "users_form.html",
          user=session.get("user"),
          mode="edit",
          form_data=form_data,
          error=f"Erro ao atualizar usuário: {exc}",
      )

    return redirect(url_for("users_list"))

  # Sincronização manual de billing por período (apenas admin)
  @app.route("/sync/manual", methods=["POST"])
  @admin_required
  def manual_sync():
    # Evita duas sincronizações concorrentes (manual/auto)
    settings = get_sync_settings()
    if settings.get("is_sync_running"):
      return redirect(
          url_for(
              "settings",
              sync_status="error",
              sync_message="Já existe uma sincronização em andamento. Aguarde a conclusão antes de iniciar outra.",
          )
      )

    period = request.form.get("period", "1y")

    now_utc = datetime.now(timezone.utc)
    # Mantemos apenas a opção de 1 ano como período padrão
    start = now_utc - timedelta(days=365)

    # Marca início de execução e registra histórico
    mark_sync_running(True, is_manual=True)
    run_id = start_sync_run(is_manual=True, window_start=start, window_end=now_utc)

    # Dispara execução em background para não travar a requisição
    thread = threading.Thread(
        target=_run_manual_sync_background,
        args=(start, now_utc, run_id),
        daemon=True,
    )
    thread.start()

    # Após iniciar a sincronização em background, redireciona para o dashboard;
    # o aviso global no layout informará que há sync em andamento.
    return redirect(url_for("dashboard"))

  @app.context_processor
  def inject_sync_state():
    """
    Injeta o estado de sincronização em todas as páginas, para que
    possamos exibir um aviso global quando houver sync em andamento.
    """
    try:
      settings = get_sync_settings()
      return {"sync_is_running": bool(settings.get("is_sync_running"))}
    except Exception:
      # Em caso de erro de conexão/DB, não quebramos o render das páginas.
      return {"sync_is_running": False}

  # Configurações gerais (apenas admin)
  @app.route("/settings", methods=["GET", "POST"])
  @admin_required
  def settings():
    message = None
    status = request.args.get("sync_status")
    sync_message = request.args.get("sync_message")

    # Carrega configurações atuais para exibição e para uso nas atualizações parciais
    sync = get_sync_settings()
    sync_runs = list_recent_sync_runs()

    if request.method == "POST":
      form_name = request.form.get("form")
      if form_name == "sync_auto":
        auto_enabled = request.form.get("auto_enabled") == "on"
        cron_expr = (request.form.get("cron_expr") or "").strip() or "0 * * * *"
        timezone_str = (request.form.get("timezone") or "").strip() or "UTC"
        # Mantém data de projeção atual
        forecast_until_date = sync.get("forecast_until_date")

        try:
          update_sync_settings(
              auto_enabled,
              cron_expr,
              timezone_str,
              sync.get("daily_limit"),
              sync.get("daily_limit_currency"),
              sync.get("monthly_budget"),
              sync.get("monthly_budget_currency"),
              forecast_until_date,
          )
          refresh_billing_schedule()
          message = "Configurações de sincronização automática atualizadas com sucesso."
        except Exception as exc:
          message = f"Erro ao atualizar configurações: {exc}"
        else:
          # Recarrega para refletir os novos valores
          sync = get_sync_settings()

      elif form_name == "limits":
        daily_limit_str = (request.form.get("daily_limit") or "").strip()
        monthly_budget_str = (request.form.get("monthly_budget") or "").strip()
        forecast_until_str = (request.form.get("forecast_until_date") or "").strip()

        daily_limit = None
        monthly_budget = None
        forecast_until_date = None

        if daily_limit_str:
          try:
            daily_limit = float(daily_limit_str.replace(",", "."))
          except ValueError:
            message = "Valor inválido para limite diário. Use apenas números, ex.: 500.00."
            return render_template(
                "settings.html",
                user=session.get("user"),
                sync=sync,
                sync_runs=sync_runs,
                message=message,
                sync_status=status,
                sync_message=sync_message,
            )

        if monthly_budget_str:
          try:
            monthly_budget = float(monthly_budget_str.replace(",", "."))
          except ValueError:
            message = "Valor inválido para orçamento mensal. Use apenas números, ex.: 1000.00."
            return render_template(
                "settings.html",
                user=session.get("user"),
                sync=sync,
                sync_runs=sync_runs,
                message=message,
                sync_status=status,
                sync_message=sync_message,
            )

        if forecast_until_str:
          try:
            forecast_until_date = datetime.strptime(
                forecast_until_str, "%Y-%m-%d"
            ).date()
          except ValueError:
            message = "Data inválida para projeção. Use o formato AAAA-MM-DD."
            return render_template(
                "settings.html",
                user=session.get("user"),
                sync=sync,
                sync_runs=sync_runs,
                message=message,
                sync_status=status,
                sync_message=sync_message,
            )

        try:
          update_sync_settings(
              sync.get("auto_enabled", False),
              sync.get("cron_expression", "0 * * * *"),
              sync.get("timezone", "UTC"),
              daily_limit,
              sync.get("daily_limit_currency"),
              monthly_budget,
              sync.get("monthly_budget_currency"),
              forecast_until_date,
          )
          message = "Limites de gasto atualizados com sucesso."
        except Exception as exc:
          message = f"Erro ao atualizar limites: {exc}"
        else:
          sync = get_sync_settings()
          sync_runs = list_recent_sync_runs()

      elif form_name == "reset_data":
        confirm_text = (request.form.get("confirm_text") or "").strip()
        if confirm_text != "RESET":
          message = 'Para confirmar a limpeza, digite "RESET" exatamente como mostrado.'
        else:
          try:
            from billing.settings import reset_billing_data

            reset_billing_data()
            message = "Dados de billing e histórico de sincronização limpos com sucesso."
          except Exception as exc:
            message = f"Erro ao limpar dados de billing: {exc}"
          else:
            # Recarrega configurações e histórico (que agora estarão vazios)
            sync = get_sync_settings()
            sync_runs = list_recent_sync_runs()

    return render_template(
        "settings.html",
        user=session.get("user"),
        sync=sync,
        sync_runs=sync_runs,
        message=message,
        sync_status=status,
        sync_message=sync_message,
    )

  @app.route("/api/billing/summary", methods=["GET"])
  @login_required
  def billing_summary_api():
    def _parse_date(value: str):
      try:
        return datetime.strptime(value, "%Y-%m-%d").date()
      except Exception:
        return None

    start_str = request.args.get("start_date") or ""
    end_str = request.args.get("end_date") or ""

    start_date = _parse_date(start_str) if start_str else None
    end_date = _parse_date(end_str) if end_str else None

    if start_date and end_date and end_date < start_date:
      start_date, end_date = end_date, start_date

    summary = get_dashboard_summary(start_date, end_date)
    return jsonify(summary)

  # Inicia o scheduler apenas quando o app está pronto
  start_scheduler()

  return app


app = create_app()


if __name__ == "__main__":
  port = int(os.getenv("PORT", "3000"))
  app.run(host="0.0.0.0", port=port, debug=False)

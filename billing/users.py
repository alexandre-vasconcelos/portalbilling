from typing import Any, Dict, List, Optional
import logging

from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_connection


logger = logging.getLogger("portal_billing.users")


def _verify_password(stored_hash: str, password: str) -> bool:
  if not stored_hash:
    return False
  # Se parece com um hash do Werkzeug (contém metadados com '$'), usa check_password_hash
  if "$" in stored_hash or stored_hash.startswith(("pbkdf2:", "scrypt:", "sha256:", "sha1:")):
    try:
      return check_password_hash(stored_hash, password)
    except ValueError:
      # Formato inesperado: cai para comparação direta
      return stored_hash == password
  # Caso contrário, considera texto simples (útil em ambiente dev / seed manual)
  return stored_hash == password


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT Id, Username, PasswordHash, Role, IsActive
        FROM Users
        WHERE Username = ?
        """,
        (username,),
    )
    row = cursor.fetchone()
    if not row:
      return None
    return {
        "id": row.Id,
        "username": row.Username,
        "password_hash": row.PasswordHash,
        "role": row.Role,
        "is_active": bool(row.IsActive),
    }


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
  user = get_user_by_username(username)
  if not user:
    logger.warning("Tentativa de login com usuário inexistente: %s", username)
    return None
  if not user["is_active"]:
    logger.warning("Tentativa de login com usuário inativo: %s", username)
    return None
  if not _verify_password(user["password_hash"], password):
    logger.warning("Senha inválida para usuário: %s", username)
    return None
  logger.info("Login bem-sucedido para usuário: %s (role=%s)", username, user["role"])
  return {
      "id": user["id"],
      "username": user["username"],
      "role": user["role"],
  }


def list_users() -> List[Dict[str, Any]]:
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT Id, Username, Role, IsActive, CreatedAt
        FROM Users
        ORDER BY Username ASC
        """
    )
    users: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
      users.append(
          {
              "id": row.Id,
              "username": row.Username,
              "role": row.Role,
              "is_active": bool(row.IsActive),
              "created_at": row.CreatedAt,
          }
      )
    return users


def create_user(username: str, password: str, role: str, is_active: bool = True) -> None:
  with get_connection() as conn:
    cursor = conn.cursor()

    password_hash = generate_password_hash(password)

    cursor.execute(
        """
        INSERT INTO Users (Username, PasswordHash, Role, IsActive)
        VALUES (?, ?, ?, ?)
        """,
        (username, password_hash, role, 1 if is_active else 0),
    )
    conn.commit()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
  with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT Id, Username, Role, IsActive, CreatedAt
        FROM Users
        WHERE Id = ?
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
      return None
    return {
        "id": row.Id,
        "username": row.Username,
        "role": row.Role,
        "is_active": bool(row.IsActive),
        "created_at": row.CreatedAt,
    }


def update_user(
    user_id: int,
    role: str,
    is_active: bool,
    new_password: Optional[str] = None,
) -> None:
  with get_connection() as conn:
    cursor = conn.cursor()

    if new_password:
      password_hash = generate_password_hash(new_password)
      cursor.execute(
          """
          UPDATE Users
          SET Role = ?, IsActive = ?, PasswordHash = ?
          WHERE Id = ?
          """,
          (role, 1 if is_active else 0, password_hash, user_id),
      )
    else:
      cursor.execute(
          """
          UPDATE Users
          SET Role = ?, IsActive = ?
          WHERE Id = ?
          """,
          (role, 1 if is_active else 0, user_id),
      )

    conn.commit()

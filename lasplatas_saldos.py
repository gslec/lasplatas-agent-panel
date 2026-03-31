import requests
import json
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL  = "https://lasplatas.com"
USERNAME  = "GSARABIA001"
PASSWORD  = "Coco9224$"
TOP_N     = 10


# ──────────────────────────────────────────
#  AUTH
# ──────────────────────────────────────────
def login():
    resp = requests.post(f"{BASE_URL}/api/auth/login",
                         json={"login": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Login fallido: {data}")
    print(f"✅  Sesión iniciada como {USERNAME}\n")
    return data["jwt"]


def find_first_value(data, keys):
    """Busca recursivamente la primera clave presente en dicts/listas anidadas."""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        for value in data.values():
            found = find_first_value(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first_value(item, keys)
            if found is not None:
                return found
    return None


def get_agent_id_from_jwt(jwt):
    """Intenta obtener el userId directamente desde el payload del JWT."""
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None

        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return find_first_value(data, ("userId", "id", "playerId", "sub"))
    except Exception:
        return None


def get_agent_id(jwt):
    """Obtiene el userId del agente logueado."""
    try:
        resp = requests.get(f"{BASE_URL}/api/player/details",
                            headers={"Authorization": f"Bearer {jwt}"})
        resp.raise_for_status()
        data = resp.json()
        agent_id = find_first_value(data, ("userId", "id", "playerId"))
        if agent_id is not None:
            return agent_id
    except Exception:
        pass

    return get_agent_id_from_jwt(jwt)


def ensure_agent_id(jwt_ref, agent_id_ref):
    """Garantiza que tengamos un sourceUserId utilizable antes de transferir."""
    if agent_id_ref[0] is None:
        agent_id_ref[0] = get_agent_id(jwt_ref[0])

    if agent_id_ref[0] is None:
        raise RuntimeError(
            "No se pudo identificar el userId de la cuenta origen. "
            "La API no devolvio ese dato en /api/player/details."
        )

    return int(agent_id_ref[0])


def renovar_jwt_si_expiro(resp, jwt_ref):
    """Si la respuesta es 401, renueva el token y actualiza jwt_ref."""
    if resp.status_code == 401:
        print("🔄  Token expirado, renovando sesión...")
        jwt_ref[0] = login()
        return True
    return False


# ──────────────────────────────────────────
#  USUARIOS
# ──────────────────────────────────────────
def get_users(jwt_ref):
    resp = requests.get(f"{BASE_URL}/api/v2/hierarchy/get-direct-children-with-balance",
                        headers={"Authorization": f"Bearer {jwt_ref[0]}"})
    if renovar_jwt_si_expiro(resp, jwt_ref):
        resp = requests.get(f"{BASE_URL}/api/v2/hierarchy/get-direct-children-with-balance",
                            headers={"Authorization": f"Bearer {jwt_ref[0]}"})
    resp.raise_for_status()
    return resp.json()


def get_balance(user, jwt):
    try:
        resp = requests.get(f"{BASE_URL}/api/v2/balance?userId={user['userId']}",
                            headers={"Authorization": f"Bearer {jwt}"},
                            timeout=10)
        b = resp.json()
        cents = (b.get("balance") or 0) + (b.get("cashBalance") or 0)
        return {"username": user["username"],
                "userId":   user["userId"],
                "balance":  round(cents / 100, 2)}
    except Exception:
        return {"username": user["username"], "userId": user["userId"], "balance": 0.0}


def get_all_balances(users, jwt, workers=20):
    results = []
    print(f"💰  Consultando saldos de {len(users)} usuarios...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(get_balance, u, jwt): u for u in users}
        for i, f in enumerate(as_completed(futures), 1):
            results.append(f.result())
            if i % 50 == 0:
                print(f"    {i}/{len(users)} consultados...")
    return results


# ──────────────────────────────────────────
#  OPCIÓN 1 – LISTAR TOP N
# ──────────────────────────────────────────
def menu_listar(jwt_ref, users):
    balances = get_all_balances(users, jwt_ref[0])
    balances.sort(key=lambda x: x["balance"], reverse=True)

    non_zero = sum(1 for b in balances if b["balance"] > 0)
    top      = balances[:TOP_N]

    print(f"\n{'='*48}")
    print(f"  TOP {TOP_N} USUARIOS CON MAYOR SALDO — LasPlatas")
    print(f"{'='*48}")
    print(f"  {'#':<4} {'Usuario':<22} {'Saldo (USD)':>10}")
    print(f"  {'-'*44}")
    for i, u in enumerate(top, 1):
        print(f"  {i:<4} {u['username']:<22} ${u['balance']:>9.2f}")
    print(f"{'='*48}")
    print(f"  Total usuarios: {len(balances)}  |  Con saldo > $0: {non_zero}\n")

    with open("saldos_resultado.json", "w", encoding="utf-8") as f:
        json.dump(balances, f, indent=2, ensure_ascii=False)
    print("💾  Resultado completo guardado en saldos_resultado.json\n")


# ──────────────────────────────────────────
#  OPCIÓN 2 – TRANSFERIR
# ──────────────────────────────────────────
def menu_transferir(jwt_ref, agent_id_ref, users):
    print(f"\n  Origen fijo: {USERNAME}")
    to_name = input("  Usuario destino (username): ").strip()

    target = next((u for u in users if u["username"].upper() == to_name.upper()), None)
    if not target:
        print(f"\n  ❌  Usuario '{to_name}' no encontrado en tu jerarquía.\n")
        return

    try:
        amount_usd = float(input("  Monto a transferir (USD): $").strip())
        if amount_usd <= 0:
            raise ValueError
    except ValueError:
        print("\n  ❌  Monto inválido.\n")
        return

    amount_cents = int(round(amount_usd * 100))

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │  De   : {USERNAME:<24}│")
    print(f"  │  Para : {target['username']:<24}│")
    print(f"  │  Monto: ${amount_usd:<23.2f}│")
    print(f"  └─────────────────────────────────┘")
    confirm = input("  ¿Confirmar transferencia? (s/n): ").strip().lower()

    if confirm != "s":
        print("\n  Transferencia cancelada.\n")
        return

    try:
        source_user_id = ensure_agent_id(jwt_ref, agent_id_ref)
    except RuntimeError as exc:
        print(f"\n  âŒ  {exc}\n")
        return

    body = {
        "sourceUserId": source_user_id,
        "targetUserId": int(target["userId"]),
        "amount":       amount_cents
    }

    # Si el token expiró, renovar y reintentar una vez
    for _ in range(2):
        headers = {"Authorization": f"Bearer {jwt_ref[0]}", "Content-Type": "application/json"}
        resp = requests.post(f"{BASE_URL}/api/v2/balance/transfer",
                             headers=headers, json=body)
        if renovar_jwt_si_expiro(resp, jwt_ref):
            # Actualizar agentId con la sesión renovada también
            agent_id_ref[0] = None
            try:
                body["sourceUserId"] = ensure_agent_id(jwt_ref, agent_id_ref)
            except RuntimeError as exc:
                print(f"\n  âŒ  {exc}\n")
                return
            continue
        break

    if resp.status_code == 200:
        print(f"\n  ✅  Transferencia exitosa: ${amount_usd:.2f} → {target['username']}\n")
    else:
        print(f"\n  ❌  Error {resp.status_code}: {resp.text}\n")


# ──────────────────────────────────────────
#  MENÚ PRINCIPAL
# ──────────────────────────────────────────
def main():
    print("╔══════════════════════════════════╗")
    print("║      LasPlatas — Panel Agente    ║")
    print("╚══════════════════════════════════╝\n")

    jwt_ref      = [login()]
    agent_id_ref = [get_agent_id(jwt_ref[0])]
    users        = get_users(jwt_ref)
    print(f"📋  {len(users)} usuarios cargados.\n")

    while True:
        print("  1. Ver top 10 usuarios con mayor saldo")
        print("  2. Transferir saldo")
        print("  0. Salir")
        opcion = input("\n  Elige una opción: ").strip()

        if opcion == "1":
            menu_listar(jwt_ref, users)
        elif opcion == "2":
            menu_transferir(jwt_ref, agent_id_ref, users)
        elif opcion == "0":
            print("\n  👋  Hasta luego.\n")
            break
        else:
            print("\n  Opción no válida.\n")


if __name__ == "__main__":
    main()

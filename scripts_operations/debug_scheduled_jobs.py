#!/usr/bin/env python3
"""Diagnóstico de tarefas agendadas — gSage AI.

Verifica o pipeline completo de execução de scheduled jobs:
  1. Banco de dados — tabela gsage_scheduled_jobs
  2. RedBeat (Redis db1) — entradas no scheduler
  3. Celery task results (Redis db2) — resultados/erros recentes
  4. Logs dos containers (celery-beat e celery-worker-scheduled)

Uso:
    python scripts_operations/debug_scheduled_jobs.py
    python scripts_operations/debug_scheduled_jobs.py --job-id <uuid>
    python scripts_operations/debug_scheduled_jobs.py --tail 100   # linhas de log
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Configuração dos containers
# ---------------------------------------------------------------------------
CONTAINERS = {
    "redis": "gsage-redis",
    "postgres": "gsage-postgres",
    "beat": "gsage-celery-beat",
    "worker_scheduled": "gsage-celery-scheduled",
    "worker_tools": "gsage-celery-tools",
}

REDIS_PASS = "dev-redis-password"
REDIS_DB_BROKER = 1   # redbeat + filas
REDIS_DB_RESULTS = 2  # task results

PG_USER = "gsage"
PG_DB = "gsage"

# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

def _run(cmd: list[str], capture: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=capture, text=True)
    return (result.stdout + result.stderr).strip()


def _redis(db: int, *args: str) -> str:
    return _run([
        "docker", "exec", CONTAINERS["redis"],
        "redis-cli", "--no-auth-warning", "-a", REDIS_PASS, "-n", str(db),
        *args,
    ])


def _psql(query: str) -> str:
    return _run([
        "docker", "exec", CONTAINERS["postgres"],
        "psql", "-U", PG_USER, "-d", PG_DB,
        "-c", query,
    ])


def _docker_logs(container: str, tail: int, grep: Optional[str] = None) -> str:
    cmd = ["docker", "logs", "--tail", str(tail), container]
    out = _run(cmd)
    if grep and out:
        lines = [l for l in out.splitlines() if grep.lower() in l.lower()]
        return "\n".join(lines)
    return out


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _ok(msg: str) -> None:
    print(f"  \033[32m✔\033[0m  {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33m⚠\033[0m  {msg}")


def _err(msg: str) -> None:
    print(f"  \033[31m✖\033[0m  {msg}")


def _info(msg: str) -> None:
    print(f"     {msg}")


# ---------------------------------------------------------------------------
# 1. Banco de dados — gsage_scheduled_jobs
# ---------------------------------------------------------------------------

def check_db_jobs(job_id: Optional[str]) -> None:
    _section("1. BANCO DE DADOS — gsage_scheduled_jobs")

    where = f"WHERE id = '{job_id}'" if job_id else ""
    query = f"""
SELECT
    id,
    name,
    job_type,
    cron_expression,
    timezone,
    is_active,
    last_run_status,
    to_char(last_run_at, 'YYYY-MM-DD HH24:MI:SS TZ') AS last_run_at,
    run_count,
    max_runs,
    to_char(starts_at, 'YYYY-MM-DD HH24:MI:SS TZ') AS starts_at,
    to_char(ends_at,   'YYYY-MM-DD HH24:MI:SS TZ') AS ends_at,
    redbeat_key
FROM gsage_scheduled_jobs
{where}
ORDER BY created_at DESC;
""".strip()

    print()
    print("  Todos os jobs registrados:\n")
    out = _psql(query)
    for line in out.splitlines():
        print(f"  {line}")

    # Resultados da última execução
    query_result = f"""
SELECT
    id,
    name,
    last_run_status,
    last_run_result
FROM gsage_scheduled_jobs
{where}
ORDER BY last_run_at DESC NULLS LAST
LIMIT 10;
""".strip()
    print()
    print("  Último resultado (last_run_result):\n")
    out2 = _psql(query_result)
    for line in out2.splitlines():
        print(f"  {line}")

    # Alertas
    print()
    query_alerts = f"""
SELECT
    id, name, is_active, last_run_status,
    redbeat_key IS NOT NULL AS has_redbeat_key,
    prompt_content IS NOT NULL AS has_prompt,
    cron_expression
FROM gsage_scheduled_jobs
{where}
ORDER BY created_at DESC;
""".strip()
    rows_raw = _psql(query_alerts)
    # Parse simples: verificar se algum job ativo não tem redbeat_key
    for line in rows_raw.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 6 and parts[3] not in ("last_run_status", "---") :
            is_active = parts[2]
            has_redbeat = parts[4]
            has_prompt = parts[5]
            name = parts[1]
            if is_active == "t" and has_redbeat == "f":
                _err(f"Job '{name}' está ATIVO mas sem redbeat_key — não será agendado")
            if is_active == "t" and has_prompt == "f":
                _warn(f"Job '{name}' do tipo PROMPT_RUN está ativo mas sem prompt_content")


# ---------------------------------------------------------------------------
# 2. RedBeat — entradas no Redis
# ---------------------------------------------------------------------------

def check_redbeat(job_id: Optional[str]) -> None:
    _section("2. REDBEAT (Redis db1) — schedule entries")

    # sorted set com próximas execuções
    schedule_raw = _redis(REDIS_DB_BROKER, "ZRANGE", "redbeat::schedule", "0", "-1", "WITHSCORES")
    print()
    print("  Próximas execuções agendadas (redbeat::schedule):\n")
    entries: list[tuple[str, float]] = []
    lines = schedule_raw.splitlines()
    i = 0
    while i < len(lines) - 1:
        key = lines[i].strip()
        try:
            score = float(lines[i + 1].strip())
        except ValueError:
            i += 1
            continue
        entries.append((key, score))
        i += 2

    now_ts = datetime.now(timezone.utc).timestamp()
    for key, score in sorted(entries, key=lambda x: x[1]):
        dt = datetime.fromtimestamp(score, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        delay = score - now_ts
        if delay < -60:
            flag = f"\033[31m(atrasado {-delay:.0f}s)\033[0m"
        elif delay < 0:
            flag = "\033[33m(próximo)\033[0m"
        else:
            flag = f"(em {delay:.0f}s)"
        short = key.replace("redbeat:", "")
        _info(f"{dt}  {flag}  {short}")

    # membros estáticos
    statics = _redis(REDIS_DB_BROKER, "SMEMBERS", "redbeat::statics")
    print()
    print("  Tarefas estáticas (redbeat::statics):")
    for s in statics.splitlines():
        _info(s.strip())

    # Detalhe de cada job dinâmico
    keys_raw = _redis(REDIS_DB_BROKER, "KEYS", "redbeat:redbeat:scheduled_job:*")
    redbeat_keys = [k.strip() for k in keys_raw.splitlines() if k.strip()]
    if not redbeat_keys:
        _warn("Nenhuma entrada redbeat:scheduled_job:* encontrada no Redis")
        return

    print()
    print(f"  Definições ({len(redbeat_keys)} job(s)):\n")
    for rk in redbeat_keys:
        if job_id and job_id not in rk:
            continue
        meta_raw = _redis(REDIS_DB_BROKER, "HGET", rk, "meta")
        defn_raw = _redis(REDIS_DB_BROKER, "HGET", rk, "definition")
        print(f"  ── {rk.replace('redbeat:', '')}")
        try:
            meta = json.loads(meta_raw)
            last_run = meta.get("last_run_at", {})
            run_count = meta.get("total_run_count", "?")
            if isinstance(last_run, dict):
                # formato redbeat: {"__type__": "datetime", "year": ..., ...}
                try:
                    dt_last = datetime(
                        last_run["year"], last_run["month"], last_run["day"],
                        last_run["hour"], last_run["minute"], last_run["second"],
                        tzinfo=timezone.utc,
                    )
                    _info(f"  last_run: {dt_last.strftime('%Y-%m-%d %H:%M:%S UTC')}  |  total_runs: {run_count}")
                except (KeyError, ValueError):
                    _info(f"  meta: {meta_raw[:120]}")
            else:
                _info(f"  meta: {meta_raw[:120]}")
        except (json.JSONDecodeError, TypeError):
            _info(f"  meta (raw): {meta_raw[:120]}")
        try:
            defn = json.loads(defn_raw)
            enabled = defn.get("enabled", "?")
            task = defn.get("task", "?")
            kwargs = defn.get("kwargs", {})
            schedule = defn.get("schedule", {})
            status_flag = "\033[32mENABLED\033[0m" if enabled else "\033[31mDISABLED\033[0m"
            _info(f"  status:  {status_flag}")
            _info(f"  task:    {task}")
            _info(f"  kwargs:  {kwargs}")
            _info(f"  cron:    {schedule}")
            # Alertas
            if task == "src.backend.app.workers.tasks.scheduled_job.run_scheduled_job":
                _err("  ATENÇÃO: task aponta para caminho antigo 'src.backend.app.workers' "
                     "(não existe). O correto é 'src.backend_api.app.tasks.scheduled_job.run_prompt_job'")
            if not kwargs.get("org_id") or not kwargs.get("user_id"):
                _warn("  kwargs não contém 'org_id' / 'user_id' — run_prompt_job vai falhar ao ser acionado")
        except (json.JSONDecodeError, TypeError):
            _info(f"  definition (raw): {defn_raw[:200]}")
        print()


# ---------------------------------------------------------------------------
# 3. Celery task results — Redis db2
# ---------------------------------------------------------------------------

def check_celery_results(job_id: Optional[str], limit: int = 10) -> None:
    _section("3. CELERY TASK RESULTS (Redis db2)")

    keys_raw = _redis(REDIS_DB_RESULTS, "KEYS", "celery-task-meta-*")
    result_keys = [k.strip() for k in keys_raw.splitlines() if k.strip()]
    if not result_keys:
        _warn("Nenhum resultado encontrado em Redis db2")
        return

    print()
    print(f"  Últimos {min(limit, len(result_keys))} resultados de tarefas:\n")
    results = []
    for rk in result_keys:
        raw = _redis(REDIS_DB_RESULTS, "GET", rk)
        try:
            data = json.loads(raw)
            results.append(data)
        except (json.JSONDecodeError, TypeError):
            pass

    # Ordenar por date_done desc
    results.sort(key=lambda d: d.get("date_done", ""), reverse=True)

    for r in results[:limit]:
        task_id = r.get("task_id", "?")
        status = r.get("status", "?")
        date_done = r.get("date_done", "?")
        result = r.get("result", {})
        tb = r.get("traceback")
        color = "\033[32m" if status == "SUCCESS" else "\033[31m"
        print(f"  {color}{status}\033[0m  {task_id}  {date_done}")
        if isinstance(result, dict):
            _info(f"result:    {json.dumps(result)[:120]}")
        if tb:
            _err(f"traceback: {str(tb)[:200]}")
        print()


# ---------------------------------------------------------------------------
# 4. Logs dos containers
# ---------------------------------------------------------------------------

def check_logs(job_id: Optional[str], tail: int = 100) -> None:
    _section("4. LOGS DOS CONTAINERS")

    containers_to_check = [
        (CONTAINERS["beat"], "celery-beat"),
        (CONTAINERS["worker_scheduled"], "celery-worker-scheduled"),
    ]

    keywords = ["error", "fail", "exception", "traceback", "schedulingerror",
                "run_prompt_job", "scheduled_job"]
    if job_id:
        keywords.append(job_id[:8])  # primeiros 8 chars do UUID

    for container, label in containers_to_check:
        print()
        print(f"  ── {label}  (últimas {tail} linhas filtradas)")
        print()
        full_logs = _docker_logs(container, tail)
        if not full_logs:
            _warn(f"  Sem saída de {container}")
            continue
        relevant_lines = []
        for line in full_logs.splitlines():
            if any(kw in line.lower() for kw in keywords):
                relevant_lines.append(line)
        if relevant_lines:
            for line in relevant_lines[-40:]:   # max 40 linhas relevantes
                # colorir erros
                if any(kw in line.lower() for kw in ["error", "fail", "exception", "traceback", "schedulingerror"]):
                    print(f"  \033[31m{line}\033[0m")
                else:
                    print(f"  {line}")
        else:
            _info("(nenhuma linha relevante — mostrando últimas 10 linhas)")
            for line in full_logs.splitlines()[-10:]:
                _info(line)


# ---------------------------------------------------------------------------
# 5. Checklist final de integridade
# ---------------------------------------------------------------------------

def summary_checklist(job_id: Optional[str]) -> None:
    _section("5. CHECKLIST DE INTEGRIDADE")
    print()

    checks = []

    # Container beat rodando?
    beat_status = _run(["docker", "inspect", "--format", "{{.State.Status}}", CONTAINERS["beat"]])
    if beat_status.strip() == "running":
        checks.append((_ok, "celery-beat está RUNNING"))
    else:
        checks.append((_err, f"celery-beat NÃO está running: {beat_status}"))

    # Container worker scheduled rodando?
    ws_status = _run(["docker", "inspect", "--format", "{{.State.Status}}", CONTAINERS["worker_scheduled"]])
    if ws_status.strip() == "running":
        checks.append((_ok, "celery-worker-scheduled está RUNNING"))
    else:
        checks.append((_err, f"celery-worker-scheduled NÃO está running: {ws_status}"))

    # Redis disponível?
    ping = _redis(REDIS_DB_BROKER, "PING")
    if "PONG" in ping:
        checks.append((_ok, "Redis db1 responde PONG"))
    else:
        checks.append((_err, f"Redis db1 sem resposta: {ping}"))

    # Algum job ativo no banco?
    active_count = _psql("SELECT count(*) FROM gsage_scheduled_jobs WHERE is_active = true;")
    n = 0
    for line in active_count.splitlines():
        try:
            n = int(line.strip())
            break
        except ValueError:
            pass
    if n > 0:
        checks.append((_ok, f"{n} job(s) ativos no banco"))
    else:
        checks.append((_warn, "Nenhum job ativo no banco"))

    # Alguma entrada no redbeat::schedule?
    schedule_count_raw = _redis(REDIS_DB_BROKER, "ZCARD", "redbeat::schedule")
    try:
        sc = int(schedule_count_raw.strip())
        if sc > 0:
            checks.append((_ok, f"{sc} entrada(s) em redbeat::schedule"))
        else:
            checks.append((_err, "redbeat::schedule está vazio — beat não está agendando"))
    except ValueError:
        checks.append((_warn, f"Não foi possível contar redbeat::schedule: {schedule_count_raw}"))

    # Lock do redbeat (se preso por muito tempo é problema)
    lock_ttl = _redis(REDIS_DB_BROKER, "TTL", "redbeat::lock")
    try:
        ttl = int(lock_ttl.strip())
        if ttl == -2:
            checks.append((_ok, "redbeat::lock não existe (normal quando beat está idle)"))
        elif ttl > 0:
            checks.append((_info if ttl < 60 else _warn,
                           f"redbeat::lock existe com TTL={ttl}s "
                           + ("(normal)" if ttl < 60 else "(suspeito — beat preso?)")))
        elif ttl == -1:
            checks.append((_err, "redbeat::lock existe SEM TTL — pode estar preso"))
    except ValueError:
        pass

    # Erros recentes no log do beat?
    beat_errors = _docker_logs(CONTAINERS["beat"], 50, grep="error")
    sched_errors = _docker_logs(CONTAINERS["worker_scheduled"], 50, grep="error")
    if beat_errors.strip():
        checks.append((_err, "Erros recentes em celery-beat (ver Seção 4)"))
    else:
        checks.append((_ok, "Sem erros recentes em celery-beat"))
    if sched_errors.strip():
        checks.append((_err, "Erros recentes em celery-worker-scheduled (ver Seção 4)"))
    else:
        checks.append((_ok, "Sem erros recentes em celery-worker-scheduled"))

    for fn, msg in checks:
        fn(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnóstico de tarefas agendadas — gSage AI"
    )
    parser.add_argument("--job-id", metavar="UUID",
                        help="Filtrar por job_id específico")
    parser.add_argument("--tail", type=int, default=100,
                        help="Linhas de log a analisar por container (padrão: 100)")
    parser.add_argument("--results", type=int, default=10,
                        help="Quantidade de task results a exibir (padrão: 10)")
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║   gSage — Diagnóstico de Tarefas Agendadas                       ║")
    print(f"║   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'):<62}║")
    if args.job_id:
        print(f"║   Filtrando job_id: {args.job_id:<46}║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    check_db_jobs(args.job_id)
    check_redbeat(args.job_id)
    check_celery_results(args.job_id, limit=args.results)
    check_logs(args.job_id, tail=args.tail)
    summary_checklist(args.job_id)
    print()


if __name__ == "__main__":
    main()

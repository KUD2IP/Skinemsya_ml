# CI/CD пайплайн Skinemsya ML

Тот же Git Flow и схема деплоя, что у бека (`Skinemsya`) и фронта (`Skinemsya_ui`): GitHub Actions → GHCR → SSH на VPS.

## Ветки

```
feature/* ──PR──► develop ──► release/X.Y.Z ──PR──► master ──► production deploy
hotfix/*  ──PR──► master (+ develop)
```

| Workflow | Триггер | Тесты | Docker push | Deploy |
| --- | --- | --- | --- | --- |
| `ml-ci.yml` | PR → develop/master/release/hotfix | да | нет | нет |
| `dependency-review.yml` | PR | deps | нет | нет |
| `ml-release.yml` | push `release/X.Y.Z` | да | да (`-rc`) | нет |
| `ml-production.yml` | push `master` / manual | да | да | да |
| `ml-hotfix.yml` | push `hotfix/**` | да | да (`-hotfix`) | только manual + deploy |

## Образ

`ghcr.io/<owner>/skinemsya-ml`

| Тег | Когда |
| --- | --- |
| `1.2.3` | release + production |
| `1.2.3-rc` | `release/*` |
| `1.2.3-hotfix` | hotfix |
| `1.2` | production (minor pointer) |
| `sha-abc1234` | каждый билд |
| `production` | последний prod |

## Секреты (Environment `production`)

Те же, что у бека/фронта (можно переиспользовать один Environment на org/repo):

| Secret | Обязателен | Назначение |
| --- | --- | --- |
| `PRODUCTION_ENV` | да | multiline `.env` (см. `deploy/env.production.example`) |
| `SSH_PRIVATE_KEY` | да | deploy-ключ |
| `PRODUCTION_HOST` | да | VPS |
| `PRODUCTION_USER` | да | обычно `deploy` |
| `PRODUCTION_SSH_PORT` | нет | по умолчанию `22` |
| `SSH_KNOWN_HOSTS` | нет | иначе `ssh-keyscan` |

`GITHUB_TOKEN` хватает для push в GHCR (`packages: write`).

## VPS (один раз)

1. `docker login ghcr.io` под пользователем с `read:packages`.
2. Каталог `/opt/skinemsya/deploy/ml` (создаст CI).
3. На диске держи запас **≥2 ГиБ** свободно: образ ML тяжёлый, CI перед деплоем делает `docker image/builder prune`, но при полностью забитом корне rsync `.env` падает с `No space left on device`.
4. Swap 1–2 ГБ на 1 ГБ RAM-машине.
5. CPU должен быть не экзотически старым, но **x86-64-v2 не обязателен**: в образе NumPy зафиксирован `<2.1` (иначе на v1-only VPS падает `NumPy was built with baseline optimizations: X86_V2`).
6. В `PRODUCTION_ENV` **бека** указать:

```bash
ML_SERVICE_URL=http://172.17.0.1:8001
```

ML слушает только `127.0.0.1:8001` на хосте.

### Если диск забит прямо сейчас

```bash
df -h /
docker system df
docker system prune -af
docker builder prune -af
sudo journalctl --vacuum-size=50M
sudo apt-get clean
df -h /
```

## Деплой

1. CI пишет `deploy/.env` из секрета + `APP_VERSION` / `GHCR_OWNER`.
2. На VPS — prune неиспользуемых Docker-слоёв, проверка свободного места, затем `rsync` → `/opt/skinemsya/deploy/ml/`.
3. `docker compose pull ml && up -d ml`.
4. Liveness: `curl http://127.0.0.1:8001/healthz` (сразу после старта uvicorn).
   Readiness: `curl http://127.0.0.1:8001/readyz` (после прогрева OCR, до ~5 мин на слабом CPU).
5. При фейле — откат на `.previous-version`.

## Локальная проверка образа

```bash
docker build -t skinemsya-ml:local .
docker run --rm -p 8100:8000 skinemsya-ml:local
curl http://localhost:8100/healthz
```

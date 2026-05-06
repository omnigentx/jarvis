---
name: cicd-pipeline
description: >
  CI/CD pipeline design and configuration. Use when a DSO needs to set
  up GitHub Actions, automated testing, or deployment workflows.
---

# CI/CD PIPELINE

## GitHub Actions Workflow Template

```yaml
name: CI/CD Pipeline
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest --cov=./ --cov-report=xml

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: |
          pip install ruff
          ruff check .

  build:
    needs: [test, lint]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build Docker
        run: docker compose build
```

## Pipeline Stages
```
Push → Lint → Test → Build → Deploy (staging) → Approval → Deploy (prod)
```

## Security rules
- ❌ DO NOT hardcode secrets in source.
- ✅ Use GitHub Secrets for API keys.
- ✅ Scan dependencies for vulnerabilities.
- ✅ Scan Docker images before deploying.

## Deployment
- Staging: auto-deploy on `develop` branch
- Production: manual approval required
- Rollback: keep 3 previous versions

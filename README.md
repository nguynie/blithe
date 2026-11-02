# 🔄 Routine Cache & State Synchronizer

A lightweight, serverless automated event polling and state synchronization utility built with Python and GitHub Actions.

---

## ✨ Features

- **Dual-Trigger Architecture**: Pre-configured with scheduled GitHub Actions cron workflows and external webhook triggers (`repository_dispatch`).
- **Deterministic Deduplication**: Uses cryptographic state hashing (`seen_posts.json`) to track processed records across ephemeral CI runner environments without database infrastructure.
- **Workflow Keepalive**: Native automated workflow maintenance to ensure scheduled cron jobs remain active indefinitely.
- **Minimal Footprint**: Native HTTP session management consuming under 30MB of memory without heavy browser drivers.
- **Atomic State Synchronization**: Temporary write-and-replace state synchronization ensuring zero corrupted cache states during concurrent tasks.

---

## 🚀 Quick Start

### 1. Configuration
Create a `.env` file based on `.env.example`:

```env
CHECK_INTERVAL_SECONDS=300
REQUEST_DELAY_SECONDS=2.0
AUTO_BASELINE_FIRST_RUN=true
```

Define input feeds/sources in `sources.yaml` (see `sources.yaml.example` for reference format):

```yaml
schema_version: 1
sources:
  - id: 1
    name: "Example Source"
    url: "https://example.com/feed"
    category: "general"
    enabled: true
```

### 2. Local Commands

- **Run single sync cycle**:
  ```bash
  python3 main.py --once
  ```

- **Run as continuous daemon**:
  ```bash
  python3 main.py --daemon
  ```

- **Inspect cache statistics**:
  ```bash
  python3 main.py --stats
  ```

---

## ⚙️ CI/CD Deployment

The repository includes a ready-to-run GitHub Actions workflow in `.github/workflows/sync.yml`.

1. Add your encrypted source configuration into GitHub Secrets:
   - Secret Name: `APP_SOURCES_DATA` (raw content of your `sources.yaml`)
2. Ensure repository Actions permissions are set to **Read and write permissions** (Settings > Actions > General).
3. The workflow will run automatically on schedule and commit updated cache state.

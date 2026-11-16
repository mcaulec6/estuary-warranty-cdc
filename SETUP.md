# Setup
 
You'll need Docker (with the Compose plugin), Python 3.9+, and accounts for Estuary, ngrok, MotherDuck, AWS (for an S3 staging bucket) and the Anthropic API. The Anthropic API is billed separately from a Claude subscription.
 
## 1. Start Postgres
 
```bash
cp .env.example .env
openssl rand -hex 24      # run twice: one for POSTGRES_PASSWORD, one for FLOW_CAPTURE_PASSWORD
docker compose up -d
docker compose logs postgres
```
 
Look for `warranty database initialised for Estuary CDC`.
 
- Use hex passwords. Base64 can include `/`, `+` and `=`, which break `DATABASE_URL`.
- The password is only set the first time the container starts. If you change it later, run `docker compose down -v && docker compose up -d`.
## 2. Generate data
 
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r generator/requirements.txt -r llm/requirements.txt
set -a; source .env; set +a
python generator/generate.py
```
 
Run `set -a; source .env; set +a` again in every new terminal. Options: `--interval 0.5` for more traffic, `--burst 5000` for a load spike.
 
## 3. Expose Postgres
 
```bash
ngrok tcp 5432
```
 
Use the `host:port` part of the forwarding address, e.g. `4.tcp.eu.ngrok.io:12345`. Type it rather than copying it, because trailing spaces break the connection string.
 
## 4. Create the capture
 
In the Estuary dashboard, go to **Sources → New Capture → PostgreSQL**:
 
| Field | Value |
|---|---|
| Name | `warranty-postgres` |
| Server address | your ngrok `host:port` |
| User | `flow_capture` |
| Password | `FLOW_CAPTURE_PASSWORD` from `.env` |
| Database | `warranty` |
 
Select `vehicles` and `warranty_claims`, then publish. The collections are named `<prefix>/warranty-postgres/public/<table>`.
 
## 5. Create the MotherDuck materialization
 
**AWS:** create an S3 bucket and an IAM user with `s3:PutObject`, `GetObject`, `DeleteObject` and `ListBucket` on it.
 
**MotherDuck:**
 
```sql
CREATE DATABASE warranty;
CREATE SECRET IN MOTHERDUCK (TYPE S3, KEY_ID '...', SECRET '...', REGION 'your-region-code');
```
 
Use the region code, e.g. `eu-north-1`, not the display name. Then create a service token under **Settings → Access Tokens**.
 
**Estuary:** go to **Destinations → New Materialization → MotherDuck**. Enter the token, database `warranty`, and the bucket and region code. Link both collections, set the sync frequency low, and publish.
 
## 6. Add the derivation
 
```bash
cd estuary
sed -i '' 's#YOUR_PREFIX#your-prefix#g' flow.yaml      # macOS; on Linux drop the ''
flowctl auth login
flowctl preview --source flow.yaml --name your-prefix/warranty/high_value_claims
flowctl catalog publish --source flow.yaml
```
 
Then add `high_value_claims` to the materialization.
 
## 7. Ask questions
 
Set `MOTHERDUCK_TOKEN`, `ANTHROPIC_API_KEY` and `MOTHERDUCK_SCHEMA` (usually `public`) in `.env`, then:
 
```bash
python llm/ask.py --show-sql "Which component has cost the most in warranty claims?"
```
 
## 8. Clean up
 
Delete the capture in Estuary first, then drop its replication slot. An orphaned slot keeps WAL forever.
 
```bash
docker exec warranty-postgres psql -U postgres -d warranty -c "SELECT pg_drop_replication_slot('flow_slot');"
docker compose down -v
```
 
Stop ngrok, and delete the S3 bucket and IAM key.
 
**While ngrok is running, the database is reachable from the internet.** Use strong passwords and fake data only, and stop the tunnel when you're not using it.
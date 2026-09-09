# GreatGatsby Budget

A private, local-first household budgeting application inspired by envelope budgeting workflows.

## Envelope accounting rules

- Ready to Assign is on-budget cash not currently reserved in positive category balances.
- Monthly assignments move cash into category envelopes; unused Available balances carry forward.
- Account balances are derived from starting balances plus their transaction ledgers.
- Account transfers create linked, equal-and-opposite transactions and are not spending.
- Funded credit-card spending automatically reserves cash in the card's payment envelope.
- Unfunded credit-card spending creates debt without creating payment cash.
- Credit-card payments are transfers from a cash account to the card and reduce the payment envelope.

## Account registers

Select an account from the Budget sidebar to open its transaction register. Registers support
editing and deleting ordinary transactions, editing or deleting both legs of a linked transfer,
cleared status, statement reconciliation, running balances, and possible-duplicate warnings.
Reconciled transactions are locked against accidental edits and deletion.

Transactions may also be split across two or more spending categories. Split lines must add up
exactly to the account transaction total. For credit-card purchases, each funded split reserves
cash in the card-payment envelope independently; unfunded portions remain card debt.

## Local Docker run

```powershell
docker compose up --build -d
```

Open http://localhost:8081 and create the initial owner account. Stop it with:

```powershell
docker compose down
```

Application data is stored in `./data` and is intentionally excluded from Git and the Docker image.

For anything beyond temporary laptop testing, copy `.env.example` to `.env` and replace
`APP_SECRET_KEY` with a long random value. Never commit `.env`.

## Reports, backups, and household access

- **Reports** provides spending by group/subcategory, monthly income versus net spending, and date/account filters. Linked transfers and credit-card payments are excluded; split purchases are counted once. Uncategorized inflows are treated as income and categorized inflows as refunds. Unmatched payment imports must be linked first. Refunds can produce negative net spending.
- **Backup & restore** is owner-only. Download a versioned JSON financial backup and store it somewhere secure/off the NAS. It includes import drafts and financial information in plain text, but no login passwords, memberships, invitation tokens, or email credentials. It is not a full installation backup. On a new installation, create an owner first and restore the financial backup; invite members again.
- Restore accepts backups up to 5 MB, requires the owner password and typing `RESTORE`, validates in an isolated database, then replaces financial data in a transaction. A mandatory pre-restore copy is saved under `data/backups` (or `/data/backups` in Docker). These recovery files are excluded from Git and the image; keep additional off-device backups yourself. Household access and SMTP settings are preserved. Only use trusted backups from this app version.
- **Household** is owner-only. Members have shared read/write access to the budget and reports, not invitations or backup/restore. Removing a member blocks their next budget request. Links expire after seven days and can only be accepted once. Reissuing an invite for the same address revokes earlier pending links.
- An email address identifies the member; this does not implement Google sign-in. New members create a local password. Existing accounts must supply their existing password. Copyable invitation links work with no email setup and no internet connection.

### Optional Gmail SMTP

Add these settings to your existing **untracked** `.env` file; do not replace your current `APP_SECRET_KEY`:

```dotenv
APP_BASE_URL=http://YOUR-NAS-LAN-IP:8081
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD=your-google-app-password
SMTP_FROM=your-address@gmail.com
```

Use a Google app password, not your normal Gmail password. Enable 2-Step Verification first; some managed/advanced-protection accounts may not offer app passwords. Official guidance: https://support.google.com/mail/answer/185833 and https://support.google.com/a/answer/176600 .

Rebuild/restart with `docker compose up --build -d`. The Household screen then offers an optional **Send invitation by email** checkbox. SMTP uses STARTTLS with certificate verification on port 587. When disabled, no email connection is made. When delivery fails, the valid copyable link remains available. No email is sent merely by configuring credentials.

`APP_BASE_URL` must be reachable by the recipient on your LAN. A localhost link only works on the same machine. Do not port-forward this application; use a trusted LAN/VPN, preferably local HTTPS for passwords and financial data. Gmail delivery requires outbound internet access only when explicitly sending an invitation. All budgeting/import processing stays local. Reverse proxies should also avoid logging invitation URL tokens.

## Navigation and multiple budgets

- The dark interface keeps account registers and budgeting in the same sidebar, with subtle transitions that respect reduced-motion preferences.
- Add transactions and make account transfers from an account register. Target setup includes expandable explanations of each target type.
- Use the budget name at the top of the sidebar to switch budgets, create a new budget, or open budget management. Each budget keeps its own accounts, categories, transactions, and household access.
- Only an owner can delete a budget. Deletion requires its exact name and the owner's password, and saves a financial recovery copy in the database's `backups` directory before removal. Recovery copies do not preserve household memberships or invitations. Deleting the last budget leaves the account available to create another.

## Running tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Release image

```powershell
docker build -t ghcr.io/charliec94/greatgatsby-budget:0.1.0 .
docker push ghcr.io/charliec94/greatgatsby-budget:0.1.0
```

For Unraid, map host port `8081` to container port `8000` and map `/mnt/user/appdata/greatgatsby-budget` to `/data`.

### Per-container Tailscale on Unraid

Use bridge networking and set the Unraid WebUI field to `http://[IP]:[PORT:8000]`. Enable Tailscale Serve with a unique hostname, target the app's internal HTTP port `8000`, set the state directory to `/data/.tailscale_state`, and leave Funnel disabled. The normal LAN URL uses host port `8081`; the private Tailscale HTTPS URL does not.

For the Unraid container icon, use:

```text
https://raw.githubusercontent.com/charliec94/greatgatsby-budget/main/assets/greatgatsby-budget-icon.png
```

The image runs as the non-root `app` user by default. If Unraid's Tailscale hook reports that it lacks root privileges, set the container's Extra Parameters to `--user 0`. After applying the template, verify `docker exec YNAB tailscale serve status`. If it reports `No serve config`, run `docker exec -u 0 YNAB tailscale serve --bg http://127.0.0.1:8000` once.

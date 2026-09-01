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

## Tests

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

# BTC Market Data Oracle — Python client demo

This folder contains a small Python demo client focused on **querying** the oracle + using the
agent-native reasoning endpoints.

> Note: for the fastest automated top-up via **NWC**, use `clients/node` to obtain an API key,
> then use this Python demo to query endpoints.

## Setup

```bash
cd clients/python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# set ORACLE_API_KEY
```

## Run

```bash
python oracle_demo.py

# or run specific calls
python oracle_demo.py --snapshot
python oracle_demo.py --reasoning
```

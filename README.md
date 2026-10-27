# intent-pay

A payment agent that only proposes. A policy in code decides. The sandbox key never reaches the model. PayPal and Airwallex sandboxes.

## Status

Work in progress. This commit sets up the project, the test run and the CI. The policy engine, the sandbox adapters, the agent and the demo page land in the next pull requests.

## Run the tests

Python 3.11 or newer. On macOS or Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
```

On Windows, activate with `.venv\Scripts\activate` instead of the `source` line.

## Keys

Copy `.env.example` to `.env` and fill it in. The code reads keys from the environment only, and `.env` is ignored by git. The default demo mode needs no keys at all.

## License

Apache-2.0, see [LICENSE](LICENSE).

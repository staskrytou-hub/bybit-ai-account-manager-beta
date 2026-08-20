# Bybit AI Account Manager — Beta

An experimental desktop **AI account-management agent for Bybit**. The project combines deterministic trading/risk components with AI-assisted research, opportunity analysis, account monitoring, execution verification, and constrained browser automation.

> **Active beta / portfolio project.** This codebase is under frequent iteration: patches, refactors, regression checks, and behavioural changes are expected as new edge cases are discovered. It is not a finished production release. The repository demonstrates AI-agent orchestration, technical product design, automation, risk gates, stateful workflows, and regression testing. It is not financial advice and should be validated with paper trading or testnet before any live use.

## Development status

This repository should be treated as a **working beta**, not a stable release. The development process is intentionally iterative: run the system, observe behaviour, identify an edge case, patch it, add or update a regression check, and repeat.

Because exchange APIs, browser flows, market conditions, and agent behaviour can change, the code may receive frequent patches and internal revisions. A recent commit should therefore be viewed as the current experimental state rather than a long-term stable API or release contract.

## Why I built it

The goal was not to build a chatbot that simply suggests trades. I wanted a system that could coordinate a much broader account workflow: collect market/account evidence, find opportunities, research strategies, evaluate risk, decide whether an action is eligible, verify what actually happened, and keep state for future decisions.

The central design rule is simple: **AI can help reason, but deterministic controls decide what is allowed and evidence decides what actually happened.**

## What this project demonstrates

- translating an ambiguous product idea into a modular working system
- using AI as an implementation and decision-support layer rather than a single black-box trader
- combining model reasoning with deterministic market, risk, sizing, and eligibility logic
- stateful agent workflows with local persistence and research memory
- controlled browser automation for account and promotion workflows
- execution verification so the agent cannot report success without evidence
- iterative debugging and regression protection through targeted smoke/evaluation scripts
- privacy-aware handling of exchange credentials and runtime account data

## Core workflow

1. Collect market and account state from deterministic sources.
2. Scan and rank candidate opportunities.
3. Run strategy/research logic only where it is justified.
4. Apply eligibility, sizing, leverage, exposure, and stop-risk gates.
5. Use AI selectively for analysis or workflow decisions.
6. Execute only through an allowed path.
7. Reconcile the exchange result and verify the actual state.
8. Persist outcomes for monitoring, learning, and later research refreshes.

## Main capabilities

- **Futures and Spot workflows**
- **Market / universe scanning** and opportunity ranking
- **Strategy research and robustness evaluation**
- **Risk engine** for sizing, leverage, exposure, stop conditions, and execution eligibility
- **Execution verification** and state reconciliation
- **Account-level orchestration** and operational monitoring
- **Promotion / rewards workflows** with explicit restrictions
- **AI routing and usage governance** to avoid unnecessary model calls
- **Local persistent stores** for research, runtime state, and learning
- **Desktop interface** built with PySide6
- **Constrained browser operator** using Playwright

## Architecture

The codebase is separated by responsibility instead of placing trading, AI, browser control, and account state in one agent loop.

| Area | Representative modules |
| --- | --- |
| Account orchestration | `account_os.py`, `opportunity_os.py`, `runtime_control.py` |
| Futures / Spot | `trading_engine.py`, `spot_engine.py` |
| Market evidence | `market_analysis.py`, `universe_scanner.py` |
| Research | `strategy_lab.py`, `adaptive_strategy_lab.py`, `strategy_discovery_ai.py` |
| Risk / eligibility | `risk_engine.py`, `execution_eligibility.py`, `trade_proposal.py` |
| Verification | `execution_verifier.py`, `verification.py` |
| AI layer | `agent.py`, `trading_ai.py`, `model_router.py` |
| Browser automation | `browser_operator.py`, `site_audit.py` |
| Learning / state | `portfolio_learning.py`, `research_store.py`, `trading_store.py` |
| Regression checks | `evals/` |

## Tech stack

- Python 3.11+
- OpenAI Agents SDK
- Pydantic
- PySide6
- Playwright
- SQLite / local persistent stores
- Windows DPAPI support for locally protected Bybit credentials

## AI-assisted development workflow

This project was built iteratively with AI coding tools. My role was to define the product behaviour, constraints, workflow logic, acceptance criteria, risk expectations, and test scenarios; run the system; identify failures; and drive repeated implementation/refactoring cycles until the required behaviour was reached.

Typical loop:

**problem → requirements → implementation → run → inspect failure → patch → regression check → next iteration**

That workflow is also reflected in the `evals/` directory: targeted smoke scripts protect important behaviours such as risk handling, execution truth, opportunity selection, browser constraints, time synchronization, account state, and runtime continuity.

## Repository safety

This public portfolio version intentionally excludes generated builds and runtime/private state. It must never contain:

- real OpenAI or Bybit API keys or secrets
- real account identifiers, balances, orders, or exported trade history
- browser cookies, session profiles, or authentication state
- local trading databases containing real account activity
- operational logs from a real account
- private workspace files

`.gitignore` blocks the expected runtime credential/state locations. `.env.example` contains placeholders only.

## Local setup

Create a virtual environment and install the runtime dependencies:

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

Or run:

```text
RUN_DEV.cmd
```

The application requires local credentials for features that call OpenAI or Bybit. Never commit real credentials.

## Evaluation

A local deterministic evaluation can be run with:

```bash
python evals/run_local.py
```

The repository also includes targeted smoke/regression scripts for core risk, execution, opportunity, browser, and runtime behaviours. These tests use synthetic or mocked conditions and are not evidence of trading profitability.

### Verified on the public-clean source package

- Python source compilation: **PASS**
- deterministic `evals/run_local.py`: **PASS**
- targeted smoke/regression scripts: **24 / 24 PASS**

These checks validate code paths and regression expectations only; they do not prove live exchange readiness or profitability.

## Beta limitations

- **active development with frequent patches and refactors**
- interfaces and internal behaviour may change between commits
- no claim of profitability or autonomous live-trading readiness
- exchange/browser behaviour can change and requires revalidation
- live execution paths require explicit credentials and independent testing
- desktop-first implementation; not packaged here as an installer

## Disclaimer

Automated crypto trading can result in substantial financial loss. This repository is an experimental software portfolio project, not a recommendation to trade or a promise of returns.


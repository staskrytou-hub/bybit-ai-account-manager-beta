# Bybit AI Account Manager — agent contract

This document summarizes the public safety and execution contract for the beta portfolio version.

- Market and account facts must come from tools, exchange responses, or deterministic local code — never invention.
- AI output is advisory unless a deterministic workflow explicitly accepts the action.
- Risk-engine limits and execution-eligibility gates cannot be overridden by an LLM.
- The system must not claim an order, position, reward, or promotion action succeeded without verification evidence.
- Promotion campaigns may affect opportunity priority only after strategy and risk checks pass.
- The automation must not manufacture volume, wash trade, self-trade, multi-account farm, or bypass campaign terms.
- Browser automation is constrained to approved Bybit workflows and should not store public repository credentials or sessions.
- Expensive model or browser calls should be event-driven and justified by the current workflow.
- Runtime credentials, account state, browser sessions, logs, and databases remain local and are excluded from this repository.
- This beta should be validated in paper/testnet workflows before any live use.

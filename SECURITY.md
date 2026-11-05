# Security policy

AccessKeeper is a demo and portfolio project. It is meant to run locally on one machine and is not hardened for production or for exposure to a network (see the threat model in the README).

## Reporting a vulnerability

Please report security problems **privately**: open the **Security** tab of this repository and choose **Report a vulnerability** (GitHub private vulnerability reporting). Do not open a public issue for a vulnerability.

Include what you found, how to reproduce it, and the version. You can expect an answer within a few days. This is a personal project, so there is no formal SLA.

## Supported versions

Only the latest release receives fixes.

## Never publish secrets

AccessKeeper must never store passwords or secrets in its records. If you find a way the app stores or logs a secret, treat it as a vulnerability and report it as above. Never commit your `.env` file or your database.

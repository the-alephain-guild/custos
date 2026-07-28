---
title: "What is custos?"
sidebar_position: 1
---


# What is custos?

## Bounded context

Custos owns local execution mechanics only:

- runner enrollment material and local machine credentials;
- verification of ARX-signed commands;
- reconciliation of desired deployment state into a local engine;
- process supervision, watchdogs and local safety circuit breakers;
- signing and publishing observed runner facts.

Custos does not own actor authorization, approval workflows, strategy or risk
configuration, promotion decisions, portfolio truth, settlement truth or the
canonical deployment lifecycle.

---
title: "Gateway Contract v1"
sidebar_position: 1
---


# Gateway Contract v1

:::warning 中文翻译尚未完成
本章暂时显示英文原文。
:::

The current runner contract contains two local validation schemas:

- `enrollment.schema.json` for locally provisioned machine enrollment material;
- `deployment_spec.schema.json` for the validated execution view translated
  from an ARX-signed canonical DeploymentSpec.

Production deployment publication is not a Custos CLI operation. ARX
publishes `DeploymentSpecReadyForRunner` and
`DeploymentInstanceDesiredStateChanged`; Custos verifies exact subject, exact
event bytes, canonical digest, runner binding and instance binding.

`custos.contracts.DeploymentMessage` is the sole public decode seam for those
signed events. It accepts neither an unsigned ARX topic nor a locally produced
command envelope.

`deployment_spec.schema.json` is generated from
`custos.contracts.DeploymentSpec.model_json_schema()`. The canonical business
payload remains ARX-owned; this schema covers only the narrow local engine
view after signature and digest verification.

Runner lifecycle observations use `RunnerDeploymentLifecycleFact.v1` through
the signed RunnerFact durable local queue. The durable local queue allocates `facts[].seq`; typed fact
builders are forbidden from supplying that field. There is no unsigned
business-topic compatibility schema or publication path.

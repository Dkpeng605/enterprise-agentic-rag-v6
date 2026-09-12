# 评测 / Evaluations

本目录保存可版本化、可复现的评测输入。生成的报告由 Git 忽略；只有经过审阅的 schema、
人工改写 fixture 和 Golden Case 可以提交。

`golden/v1/manifest.yaml` 是首版入口：它锁定 schema/revision、Case 与 corpus 文件，并声明
类别和语言数量。`schema-v1.json` 是可移植 JSON Schema；后端 `GoldenSetLoader` 还会执行
严格字段、跨文件 ID、collection scope、expected fact 原文和覆盖数量校验。

The directory contains versioned, reproducible evaluation inputs. Generated reports are ignored by
Git; only reviewed schemas, rewritten fixtures, and Golden Cases may be committed. Start with
`golden/v1/manifest.yaml`. The backend loader enforces strict fields, cross-file references,
collection scope, verbatim expected facts, and declared category/language coverage.

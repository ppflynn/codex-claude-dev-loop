# 开发计划

## 目标

在 demo-project 目录创建一个简单的 Python 命令行计算器。

## 功能

实现以下函数：

- add(a, b)
- subtract(a, b)
- multiply(a, b)
- divide(a, b)

## 文件范围

允许创建或修改：

- demo-project/calculator.py
- demo-project/test_calculator.py
- demo-project/README.md

禁止修改：

- scripts/
- docs/PLAN.md
- 项目根目录配置
- .git/
- .env

## 实现要求

- divide 遇到除数为 0 时抛出 ValueError；
- 使用 pytest 编写测试；
- 覆盖正常计算和除零情况；
- 不引入第三方业务依赖。

## 验收标准

运行：

pytest demo-project -q

所有测试通过。

## 完成报告

完成后将以下内容写入：

docs/IMPLEMENTATION_REPORT.md

报告需要包含：

- 修改文件；
- 实现内容；
- 运行的命令；
- 测试退出码；
- 测试结果；
- 遗留问题。
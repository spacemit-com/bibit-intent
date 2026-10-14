# 意图识别用例:知识库查询

**对应产品需求**: §3.1 知识库、§4.1 进迭知识库  
**场景**: 查本地知识库（个人笔记/手册/导入资料）vs 开放知识问答（升级云端）  
**核心边界**: 本地有的知识 → query_knowledge；通用/开放知识 → escalate

## 字段说明

- **输入**: 模拟真实用户的口语表达
- **期望路由**: `rule_call` / `to_model` / `escalate`
- **期望工具**: 目标工具名，无则 None
- **期望参数**: 关键槽位值，无则 `-`
- **说明**: 用例意图与边界标注

> **重要边界**: 知识库查询是否命中，取决于本地知识库**实际导入了什么**。
> 本文档假设本地库含：设备手册、WiFi/亮度等操作文档（见 kb/ 目录）。
> 库里没有的内容（如"量子纠缠"）应 escalate，而非硬查本地。

---

## 本地知识库查询（query_knowledge）—— 库内有的内容

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 怎么调亮度 | to_model | query_knowledge | query=调亮度 | 操作类，本地手册有 |
| 如何连接WiFi | to_model | query_knowledge | query=连接WiFi | 本地文档有 |
| WiFi怎么设置 | to_model | query_knowledge | query=WiFi设置 | 本地文档 |
| 亮度在哪里调 | to_model | query_knowledge | query=亮度调节 | 操作指引 |
| 设备怎么重启 | to_model | query_knowledge | query=重启方法 | 手册类 |
| 查一下WiFi的说明 | to_model | query_knowledge | query=WiFi说明 | 明确查文档 |
| 帮我查下亮度设置文档 | to_model | query_knowledge | query=亮度设置 | 明确查知识库 |
| 关于网络配置的说明 | to_model | query_knowledge | query=网络配置 | 查文档 |

## 明确指向知识库的查询

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 知识库里有关于WiFi的吗 | to_model | query_knowledge | query=WiFi | 明确查库 |
| 搜索我的笔记里的网络设置 | to_model | query_knowledge | query=网络设置 | 查个人笔记 |
| 手册里怎么说亮度的 | to_model | query_knowledge | query=亮度 | 查手册 |
| 文档里有没有提到重启 | to_model | query_knowledge | query=重启 | 查文档 |

## 开放知识问答（escalate）—— 本地库没有，升级云端

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 什么是量子纠缠 | escalate | - | - | 开放知识，本地无，前置判断拦截升级 |
| 解释一下RISC-V架构 | escalate | - | - | 开放知识问答 |
| 为什么天是蓝的 | escalate | - | - | 开放知识 |
| 介绍一下进迭时空 | escalate | - | - | 公司介绍，走云端对话/进迭知识库 |
| 讲讲人工智能的历史 | escalate | - | - | 开放知识 |
| Python怎么写循环 | escalate | - | - | 编程知识，端侧库无，升级 |
| 什么是端侧AI | escalate | - | - | 开放知识 |
| 相对论是什么原理 | escalate | - | - | 开放知识 |
| 介绍下K3芯片 | escalate | - | - | 产品知识，走进迭知识库（云端） |

## 混淆/边界用例

| 输入 | 期望路由 | 期望工具 | 期望参数 | 说明 |
|------|---------|---------|---------|------|
| 怎么调亮度 | to_model | query_knowledge | query=调亮度 | 问"怎么做"=查文档，非直接执行调亮度 |
| 把亮度调到50 | rule_call | set_brightness | level=50 | 直接执行，非查知识 |
| 亮度 | to_model | - | - | 单词歧义：查文档/查状态/设置？ |
| WiFi连不上怎么办 | escalate | - | - | 故障排查=复杂诊断（§3.2.4），升级 |
| 蓝牙怎么配对 | to_model | query_knowledge | query=蓝牙配对 | 操作类，假设手册有则查库 |
| 帮我写个WiFi配置教程 | escalate | - | - | 生成内容≠查询，写作类升级 |
| 我的会议记录在哪 | to_model | query_knowledge | query=会议记录 | 查个人笔记（若已导入） |
| 天气怎么样 | escalate | - | - | 实时信息，端侧无，升级 |

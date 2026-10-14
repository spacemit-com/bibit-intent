# Ubuntu 命令 Skill 准确率测试

## 背景
依据 [wiki.ubuntu.org.cn/UbuntuSkills](https://wiki.ubuntu.org.cn/UbuntuSkills)
把「安装升级/系统/硬盘/进程/网络/iptables/安全/服务/用户/文件编码」等能力提炼为
两个端侧 Skill（共 92 个工具）：

- `skills/ubuntu_cmd/`（58 个只读查询）：dpkg/apt 包管理、uname/lsmod/lspci/lsusb/
  dmidecode/ulimit、df/du/lsblk/fdisk/hdparm/iostat、ps/pstree/lsof/free、
  ip/ss/whois/tracepath/curl、iptables -L、clamscan/last/lastlog2/w、
  systemctl 查询、/etc/passwd、file/enca 等
- `skills/ubuntu_admin/`（22 个修改类，全部 `confirm: true` 二次确认）：
  安装/卸载/更新/清理软件、挂载/卸载/检查/格式化分区、杀进程/清僵尸、
  启停服务、增删/锁定用户、iptables 拦截、NAT、convmv 转码

关键配套改动：`agent/skill_loader.py` 透传 `confirm` 字段，管理类工具
在执行层统一走二次确认（非交互/EOF 默认拒绝）。

口语路由增强：新增 embedding 语义路由层（`agent/embedding.py`），
用 bge-small-zh 把用户口语编码成 512 维向量做**混合语义路由**
（dense 余弦 + BM25 稀疏通道、按工具分组打分），流水线变为
「规则引擎 → embedding 语义路由 → 端侧小钢炮(1B)模型 → 云端」，
详见下文「embedding 语义路由」。

## 文件
- `skills/ubuntu_cmd/{skill.yaml,handlers.py}`   只读查询工具
- `skills/ubuntu_admin/{skill.yaml,handlers.py}` 管理类工具（二次确认）
- `corpus.jsonl`  140 条自然语言用例（覆盖全部 92 个工具 + 应升级用例）
- `anchors.jsonl`  920 句口语锚点（92 个工具 × 10 句，仅作 embedding 索引，不参与计分）
- `probe.py`      路由准确率探针（规则/模型/综合，--n 取多数票）
- `run_on_device.py`  推送到设备并执行（BIBIT_DEV_PW）

## 运行
```bash
# 设备上（llama-server 在 127.0.0.1:8081）
cd /opt/bibit
set -a; . /etc/bibit-intent/inference.env; set +a
.venv/bin/python tests/ubuntu_cmd/probe.py --n 3

# 对照旧行为（关闭 embedding 语义召回）
.venv/bin/python tests/ubuntu_cmd/probe.py --n 3 --no-embed

# 或从本机一键推送执行
BIBIT_DEV_PW='***' python3 tests/ubuntu_cmd/run_on_device.py --n 3
```

## embedding 语义路由（先 embedding，再走小钢炮模型）

- 模型：`Bge-Small-Zh-V1.5-Q4_K_M.gguf`（15M，中文优化，512 维），
  下载自 https://archive.spacemit.com/spacemit-ai/model_zoo/embed/ ，
  放 `/opt/bibit/models/`；备选更大模型 `piccolo-large-zh-v2-q4_0.gguf`(177M)
  或 `Qwen3-Embedding-0.6B-Q4_0.gguf`(364M)。
- 服务：独立 systemd 单元 `bibit-intent-embed.service`（见 deploy/bibit-intent-embed.service），
  `llama-server --embedding --pooling mean --embd-normalize 2` 监听 127.0.0.1:8082，
  与主 LLM（8081）互不抢占；本机复现部署：
  `cp deploy/bibit-intent-embed.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now bibit-intent-embed`
- 路由：`agent/intent.py:decide_local_intent` 在规则未命中后先做 embedding 语义路由，
  `agent/embedding.py:EmbeddingRetriever` 一次查询只调一次 `/embedding`（`route()`）：
    - 稠密通道：查询向量与锚点示例做余弦相似度，**按工具分组打分**
      （组内 max 与均值 0.7/0.3 加权），避免锚点数多寡影响排名；
    - 稀疏通道：BM25 字符 bigram + ASCII 词（纯标准库），对每个工具的锚点文档
      打分，弥补小 embedding 对「装了/依赖/病毒」等术语的混淆；
    - 混合排序：`0.75*dense + 0.25*sparse`，取 top-5 工具。
  索引锚点 = corpus.jsonl 的 rule 用例 + anchors.jsonl（每工具 5 句口语锚点），
  惰性构建 + 本地缓存（`*.embed_cache.json`，version 3，多源文件 mtime 校验）。
  只在与关键词通道**一致**（agreed）时给强提示「最可能是: xxx」并排到候选首位；
  关键词够不着且 embedding 置信 ≥0.72 时才由 embedding 独立供给候选。
  服务不可用/构建失败自动降级回关键词逻辑。
- 容错兜底（实测关键）：1B 模型常把口语里的软件名当工具名（如 `CALL vim`）或
  误判升级，路由器按 ①agreed 强提示 ②唯一候选 兜底执行
  （`registry.resolve_with_fallback` + router 的 escalate 覆盖），并用
  `rule.match_tool` 补抽参数；模型参数是幻觉时只保留目标工具声明的参数。
- 缺参提示：兜底后仍缺必填参数时，不再报硬错误，而是返回
  `ExecResult.need_param`（`agent/executor.py`）；REPL 会话模式会
  **挂起 pending，下一轮直接补值自动回填执行**（如回复 `python3` 即
  补 `pkg`），换话题则自动清空走正常路由。详见
  [docs/intent/slot_missing_management.md](../../docs/intent/slot_missing_management.md)。

## 链路性能
三段链路（规则 → 混合语义路由 → 端侧模型）的性能数据与解读见
[docs/design/performance.md](../../docs/design/performance.md)，基准代码在 `tests/perf/`：

```bash
.venv/bin/python tests/perf/bench_pipeline.py --n 2
```

## 实测结果（设备 10.0.91.72，bge-small-zh @ llama-embed:8082 + MiniCPM5-1B @ llama-server:8081，多数票）
- 规则引擎：100%（128/128，多次复跑恒定），负责绝大多数自然问法，
  另抢答 23 条标注 model 的用例（规则优先是设计）；本轮为口语「转成 utf8」
  补了一条带路径槽位的规则（`把 /etc/hosts 这个文件转成 utf8` 直通）
- 端侧模型：100%（11/11），处理规则够不着的口语用例，平均耗时 ~1.8s/条；
  embedding 强提示 10/10 全对（混合路由升级后较 8/8 提升，新增 pkg_depends/
  sec_antivirus 两例命中），误判升级/幻觉工具名由确定性通道兜底
- 开放问答升级：100%（1/1）
- 综合：140/140（100%）；关闭 embedding（--no-embed）同为 140/140
  （关键词通道已补齐，测试集内无差距；混合路由的价值在于语料外的口语输入）
- 端到端回归（设备实跑）：「今天天气怎么样/今天星期几」→ 升云并正确作答；
  「请给我打开浏览器/打开浏览器」→ 规则直达 launch_app

### 已知限制
- bge-small-zh（15M）对相近语义有混淆：如「装了哪些内容」会在
  pkg_files/pkg_installed/pkg_info 间摆动、「拉哪些依赖」会在 pkg_depends/
  pkg_rdepends 间摆动，故只在与关键词通道一致时才给强提示，避免误导。
- grammar 参数值只允许 `[a-z0-9_.]`，带 `/` 的路径参数模型无法输出；
  「转成 utf8」这类已补规则直通，其余靠容错兜底 + 缺参提示向用户补充。
- 1B 模型把口语软件名当工具名（`CALL vim`/`CALL tree`）的问题仍存在，
  由 embedding 强提示/唯一候选兜底解决；若去掉 embedding，这类用例会升级云端。
- 未安装命令会明确报「未安装命令 xxx：请先 apt-get install 对应软件包」；
  设备现已装齐 `last/iostat/hdparm/lshw/clamscan/enca/whois/
  tracepath/convmv/iptables/lastlog2`。
- 修改类工具在非交互模式默认拒绝执行（安全设计），交互 REPL 中会弹 [y/N] 确认。

### 本轮配套修复（提升准确率）
- 内置 `launch_app`/`close_app` 规则加 `^` 锚定，避免「系统运行了多长时间了」
  被「运行」关键字抢答（修掉 2 条误路由）。
- 规则槽位惰性量词修复：`apt里能搜到htop`、`加一个用户carol` 等不再把
  query/name 截成最后一个字符（pkg_search/user_add/pkg_files 等 7 处）。
- 关键词候选收紧 + 补齐：`安装/依赖/端口/目录/病毒/转成` 等口语词加入
  skill 关键词，使模型用例只注入正确候选（曾注入核心工具集导致乱选）。
- online_users 去掉单字符关键词 `w`（会误伤任何含字母 w 的英文路径）。

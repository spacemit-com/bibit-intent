# 离线词汇资源

## 中文：jieba 0.42.1

- 官方源：<https://github.com/fxsjy/jieba/tree/v0.42.1>
- 许可证：MIT；<https://github.com/fxsjy/jieba/blob/v0.42.1/LICENSE>
- 本项目读取已安装依赖中的 `jieba/dict.txt`，不复制其字典、不调用
  `jieba.posseg`、HMM、神经网络或网络下载。
- 依赖版本在 `requirements.txt` 和 `pyproject.toml` 固定为 0.42.1。
- 当前字典 SHA-256：
  `7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8`。
- 349,045 个词条，其中 34,761 个原始 `v` 标签词条。
  这些是字典规模，不是路由正确率；`vn` 默认视为名词候选，领域词可以覆盖。

## 英文：Princeton WordNet 3.0

- 官方归档：<https://wordnetcode.princeton.edu/3.0/WordNet-3.0.tar.gz>
- 原始归档 SHA-256：
  `640db279c949a88f61f851dd54ebbb22d003f8b90b85267042ef85a3781d3a52`。
- `wordnet-verbs.json` 从 `dict/index.verb` 和 `dict/verb.exc` 提取，
  包含 11,529 个动词词目、2,401 个不规则变形入口。
- 许可证原文保存在 `WORDNET-LICENSE.txt`，同时在 `debian/copyright` 声明。
- 保留源版本和源校验值；不分发 WordNet 可执行程序，也不引入 NLTK。
- 短语词目保留在数据中，但当前英文 tokenizer 只做单词级查表，
  不能把词目总数解读成已经支持全部短语或全部现代英文词汇。

在仓库根目录离线重新生成（归档需预先获取）：

```sh
.venv/bin/python scripts/build_intent_lexicon.py /path/to/WordNet-3.0.tar.gz | apply_patch
```

生成器先校验归档，不解压执行任何文件；输出补丁，不进行网络访问。
生成后的 JSON 与许可证随 wheel 和 Debian 包内的 `src` 资源一起分发。

## 共享领域词汇

`analysis/domain_lexicon.py` 从原 `multi_action.py` 迁移领域动作及连接表达，
并补充配置、验证、部署、条件与转折表达。它与通用词典候选分层使用：
词典提供可能的词性，领域词和结构规则决定是否计为动作。
词典缺失或损坏时返回 `lexicon_unavailable`，不尝试联网修复，不继续自动执行。

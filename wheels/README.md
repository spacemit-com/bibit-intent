# wheels/ 与 deps/ 依赖说明

`wheels/` 存放离线 wheel 包（已纳入 git），作为 `deps/` 的再生成来源。
运行时实际使用的是 `deps/` 下解包后的 vendored 依赖，**无需 pip 安装**
（由 `src/bibit_intent/core/text_normalizer.py` 自动加载）。

## 包清单

| 文件 | 说明 |
| --- | --- |
| `pynini-2.1.7-cp314-cp314-linux_riscv64.whl` | 在 Bianbu `riscv64` + `cp314` 上源码编译的 Pynini（约 60MB） |
| `wetextprocessing-1.2.0-py3-none-any.whl` | 官方 WeTextProcessing 1.2.0 wheel 注入英文 ITN FST 的定制版（约 2.1MB） |

## 与 deps/ 的关系

| 目录 | 内容 | 用途 |
| --- | --- | --- |
| `src/bibit_intent/deps/pynini` | 解包 pynini wheel（`pynini/`、`pywrapfst/`、`*.so`） | 运行时 import `pynini` |
| `src/bibit_intent/deps/wetext` | 解包 WeTextProcessing wheel（`tn/`、`itn/`） | 运行时 import `tn` / `itn` |

## 从 wheels/ 再生成 deps/

```shell
rm -rf deps && mkdir -p src/bibit_intent/deps/pynini src/bibit_intent/deps/wetext
unzip -q wheels/pynini-*.whl -d src/bibit_intent/deps/pynini
unzip -q wheels/wetextprocessing-*.whl -d src/bibit_intent/deps/wetext
```

## 再生成 wheel

### pynini wheel（需在 riscv64 设备上，约 30 分钟）

```shell
sudo apt-get install -y libfst-dev
pip wheel pynini==2.1.7 --no-deps -w wheels/ -i https://pypi.org/simple
```

依赖说明：`libfst-dev`（OpenFst 1.8.4）来自
`archive.spacemit.com/bianbu4 universe`；构建依赖 `Cython` 需从公共 PyPI 拉取。

### WeTextProcessing 定制 wheel

1. 下载官方 wheel：
   ```shell
   pip download WeTextProcessing==1.2.0 --no-deps -d /tmp/wtp_orig -i https://pypi.org/simple
   ```
2. 解压并注入英文 ITN FST：
   ```shell
   mkdir -p /tmp/wtp_unpack && cd /tmp/wtp_unpack
   unzip -q /tmp/wtp_orig/wetextprocessing-1.2.0-py3-none-any.whl
   cp <site-packages>/itn/en_itn_tagger.fst itn/
   cp <site-packages>/itn/en_itn_verbalizer.fst itn/
   ```
3. 重新打包：
   ```shell
   python -m wheel pack /tmp/wtp_unpack -d wheels/
   ```

> 英文 ITN FST（`en_itn_tagger.fst` / `en_itn_verbalizer.fst`）可通过在已安装
> WeTextProcessing 的设备上以 root 运行一次
> `python -c "from itn.english.inverse_normalizer import InverseNormalizer; InverseNormalizer()"`
> 生成。

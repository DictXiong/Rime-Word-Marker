# Rime Word Marker

一个面向 Rime 词库整理场景的本地 Web 应用，支持导入、导出、逐条标注和高密度词库管理。

> Assisted-by: Codex:GPT-5.4

## 功能概览

- SQLite 存储词条、拼音、词频、状态、导入时间、标注时间
- 按 Rime 风格逐行导入词库，自动去重
- 缺失拼音时自动补全带声调拼音
- 按状态导出 Rime YAML，可选是否包含词频
- 导出时可选纳入 AI 辅助标注结果
- 审核页使用随机取词，支持历史跳转、拼音即时编辑和优先审核 AI 已标注词条
- 支持 OpenAI 兼容接口的 AI 辅助标注与后台自动批处理
- 审核快捷键支持 `←/↓/→`、`J/K/L`、`1/2/3`、`A/S/D`，并可用 `Space` 采纳 AI 建议
- 词库管理页支持分页、搜索、单条编辑、批量修改
- 导入大词库时提供“导入中”遮罩与导入结果摘要

## 页面说明

- 首页：展示总览，并进入不同工作页面
- 筛词页：一次展示一个待定词条，适合连续标注
- 导入 / 导出页：处理词库文件导入与按状态导出
- 词库管理页：用表格高密度浏览、搜索、批量维护词条，并控制 AI 自动标注

## 环境要求

- Python 3.10+
- `pip`

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖目前只有：

- `pypinyin`

## 启动

最简单的启动方式：

```bash
.venv/bin/python main.py
```

默认监听：

- 地址：`127.0.0.1`
- 端口：`8000`

打开浏览器访问：

```text
http://127.0.0.1:8000
```

如果要从局域网其他设备访问：

```bash
.venv/bin/python main.py --host 0.0.0.0 --port 8000
```

## 配置

应用支持命令行参数，也支持 JSON 配置文件。

### 命令行参数

```bash
.venv/bin/python main.py --host 0.0.0.0 --port 8000 --db-path /data/rime-marker/words.db
```

支持参数：

- `--host`：监听地址
- `--port`：监听端口
- `--db-path`：SQLite 数据库文件路径
- `--config`：配置文件路径
- `--verbose`：打印详细调试日志，包括用户更新操作与 AI 请求 / 回复

### 配置文件

示例配置文件见 `config.example.json`：

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "db_path": "./data/words.db",
  "verbose": false,
  "ai": {
    "endpoint": "http://127.0.0.1:11434/v1",
    "api_key": "",
    "model": "your-model-name",
    "timeout": 90,
    "batch_size": 24,
    "examples_per_class": 768,
    "candidate_mode": "sequential",
    "retry_extreme_batches": false,
    "max_tokens": null
  }
}
```

`ai.max_tokens` 为 `null` 或省略时会按 `ai.batch_size` 自动估算；如需手动固定输出上限，也可以直接填整数。

使用方式：

```bash
cp config.example.json config.json
.venv/bin/python main.py --config ./config.json
```

优先级：

1. 命令行参数
2. 配置文件
3. 内置默认值

## 数据说明

每条词条包含：

- `phrase`：词条本身，唯一
- `pinyin`：标准拼音，可手动修改
- `weight`：词频 / 权重，默认 `1`
- `weight_defined`：词频是否由导入文件或人工编辑明确定义
- `status`：`pending` / `accepted` / `rejected`
- `imported_at`：导入时间
- `labeled_at`：最后一次被标注为接受或拒绝的时间
- `ai_label`：AI 标注结果，`NULL` / `pending` / `accepted` / `rejected`
- `ai_score`：AI 输出的 0~1 分数
- `ai_labeled_at`：AI 最后一次标注时间
- `ai_model`：使用的模型名
- `ai_prompt_version`：使用的提示词版本

默认数据库路径：

- `data/words.db`

## 导入格式

支持类似 Rime 的逐行格式：

```text
词条\t拼音\t词频
```

说明：

- 列之间用 `Tab` 分隔
- 后两列可省略
- 缺失拼音时自动补全
- 缺失词频时默认设为 `1`
- 系统会区分“未定义词频”和“词频明确定义为 1”
- 旧版数据库自动升级时，既有词条默认视为“词频未定义”
- 只按词条本身去重
- 重复词条可按导入页选项覆盖拼音和词频；默认不覆盖拼音、覆盖词频
- 覆盖只会使用导入行中实际提供的列，省略拼音或词频时不会覆盖已有值
- 如果文件中包含 Rime YAML 头块，`---` 到 `...` 之间的内容会被整段忽略

## 导出格式

导出为 Rime `.dict.yaml`，支持：

- 选择导出哪些状态
- 选择是否包含词频
- 选择是否纳入 AI 辅助结果
- 实时显示当前选择将导出多少条词条

## 审核页说明

- 使用随机取词，默认优先抽取尚未 AI 标注的待定词条
- 可开启“优先 AI 已标注”，优先级为 `AI 待定` > `AI 接受 / AI 拒绝` > 普通待定词条
- 支持浏览器本地历史跳转
- 支持直接编辑当前词条拼音
- 支持显示 AI 建议状态与 AI 分数
- 当 AI 给出明确建议时，可一键采纳 AI 建议
- 审核 session 按浏览器标签页隔离，不同标签页不会互相覆盖审核进度

快捷键：

- `←` / `J` / `1` / `A`：接受
- `↓` / `K` / `2` / `S`：待定
- `→` / `L` / `3` / `D`：拒绝
- `Space`：同意 AI 建议（仅在 AI 建议为接受/拒绝时）
- `Ctrl/Cmd + S`：保存拼音

## AI 辅助标注

开启方式：

1. 在配置文件中填写 `ai.endpoint`、`ai.model` 等参数
2. 启动服务
3. 在“词库管理”页打开“自动标注”开关

当前实现规则：

- 只会为 `人工状态 = pending` 且尚未 AI 标注或 AI 规则版本已过期的词条自动生成 AI 标注
- 只有当人工 `accepted + rejected` 样本量足够时，才允许开启自动标注
- AI few-shot 只采样人工接受 / 拒绝样本，并会额外优先加入人工结果与旧 AI 结果不一致的 hard examples
- few-shot 样本会在服务进程内缓存，并在人工标注或词条修改后自动失效
- AI 候选词只会在词频明确定义时携带 `weight`；词频越高会提示模型提高接受倾向
- `ai.candidate_mode` 可设为 `sequential` 或 `random`，用于控制后台 AI 队列顺序抽取或随机抽取
- `ai.retry_extreme_batches` 默认关闭；打开后，若整批 AI 结果全为接受或全为拒绝，会自动重跑一次
- 如果 AI 输出被 `max_tokens` 截断，会先临时提高 `max_tokens` 重试，再自动拆批重试
- 顶层 `verbose` 可开启详细日志；AI 日志会打印请求 payload 与响应内容，但不会打印 API key
- prompt 版本由代码内部维护，不从配置文件读取
- AI 输出必须是 `0~1` 分数，大于 `0.66` 记为接受，小于 `0.33` 记为拒绝，中间记为待定
- 如果词条内容 `phrase` 被修改，原有 AI 标注会自动清空

当前默认阈值：

- 总人工样本至少 `2000`
- 接受样本至少 `300`
- 拒绝样本至少 `300`

## 测试

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

快速检查 Python 文件能否正常编译：

```bash
python3 -m compileall app main.py tests
```

## 部署建议

当前项目适合部署在：

- 本机自用
- 局域网内家庭 / 小团队环境
- 内网服务器

已经做过的几项部署友好优化：

- 数据库路径可配置
- 导出改为流式写出，避免大导出时占用过多内存
- 导入重复词统计改成批量查询，并支持可选覆盖拼音和词频

如果你准备长期运行，建议额外做：

- 用 `systemd` 或进程管理器托管
- 配反向代理
- 定期备份 SQLite 数据库文件

## 当前状态

项目已经可以稳定完成以下核心流程：

1. 导入大词库
2. 连续审核词条
3. 管理和批量编辑词条
4. 按状态导出结果

如果后续继续扩展，比较值得考虑的方向包括：

- 更强的导入性能优化
- 审核统计图表
- 多用户隔离与权限
- 更完整的部署文档

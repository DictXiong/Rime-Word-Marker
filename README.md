# Rime Word Marker

一个面向 Rime 词库整理场景的本地 Web 应用，支持导入、导出、逐条标注和高密度词库管理。

> Assisted-by: Codex:GPT-5.4

## 功能概览

- SQLite 存储词条、拼音、词频、状态、导入时间、标注时间
- 按 Rime 风格逐行导入词库，自动去重
- 缺失拼音时自动补全带声调拼音
- 按状态导出 Rime YAML，可选是否包含词频
- 导出时可选纳入 AI 辅助标注结果
- 中英混合词条支持拼音/编码锁定，导出时可保留人工指定编码
- 支持为词条维护 OpenCC 延伸词，并导出 OpenCC 扩展词典
- 审核页使用随机取词，支持历史跳转、拼音即时编辑和优先审核 AI 已标注词条
- 支持 OpenAI 兼容接口的 AI 辅助标注与后台自动批处理
- 审核快捷键支持 `←/↓/→`、`J/K/L`、`1/2/3`、`A/S/D`，并可用 `Space` 采纳 AI 建议
- 词库管理页支持分页、搜索、词频范围筛选、单条编辑、批量修改与拼音锁定
- 词库管理页提供全局维护区，可批量重算无声调拼音
- 导入大词库时提供“导入中”遮罩、可选导入前备份与导入结果摘要

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

如果要同时监听 IPv4 和 IPv6 地址，可以重复传入 `--host`，或使用逗号分隔：

```bash
.venv/bin/python main.py --host 0.0.0.0 --host :: --port 8000
```

## 配置

应用支持命令行参数，也支持 JSON 配置文件。

### 命令行参数

```bash
.venv/bin/python main.py --host 0.0.0.0 --host :: --port 8000 --db-path /data/rime-marker/words.db
```

支持参数：

- `--host`：监听地址，可重复指定，也可用逗号分隔多个地址
- `--port`：监听端口
- `--db-path`：SQLite 数据库文件路径
- `--config`：配置文件路径
- `--verbose`：打印详细调试日志，包括用户更新操作与 AI 请求 / 回复

### 配置文件

示例配置文件见 `config.example.json`：

```json
{
  "host": ["127.0.0.1"],
  "port": 8000,
  "db_path": "./data/words.db",
  "allowed_hosts": [],
  "access_token": "",
  "access_token_file": "",
  "max_request_body_mb": 512,
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

`host` 可以写成单个字符串或字符串数组；也支持使用 `hosts` 字段表达多个监听地址。例如同时监听 IPv4 / IPv6：`"host": ["0.0.0.0", "::"]`。

`allowed_hosts` 为空数组时不检查 `Host` 请求头；如果通过 Nginx 反向代理公开访问，建议填入实际域名和本机回源地址，例如 `["rime.example.test", "127.0.0.1"]`。

`access_token` 为空字符串时不启用应用层访问控制；设置为高强度随机字符串后，除首页、静态资源、`/api/health`、`/api/stats`、`/api/export` 外，其它页面和 API 都需要授权。首次访问受保护页面时在 URL 后添加 `?token=你的token`，服务端会写入长期 HttpOnly Cookie 并自动跳转去掉 URL 中的 token，之后同一浏览器无需再次输入。

也可以用 `access_token_file` 从文件读取 token，路径相对于配置文件所在目录解析；如果同时设置 `access_token_file` 和 `access_token`，优先使用文件内容。token 文件末尾可以带换行，程序会自动去除首尾空白；如果指定的 token 文件不存在、不可读或内容为空，服务会拒绝启动。

`max_request_body_mb` 是后端请求体大小上限，主要用于导入大词库时保护后端；Nginx 的 `client_max_body_size` 应不小于该值。

使用方式：

```bash
cp config.example.json config.json
.venv/bin/python main.py --config ./config.json
```

优先级：

1. 命令行参数
2. 配置文件
3. 内置默认值

## Nginx 反向代理

建议后端只监听本机地址，由 Nginx 对外提供 HTTPS：

```json
{
  "host": ["127.0.0.1"],
  "port": 8000,
  "db_path": "/var/lib/rime-word-marker/words.db",
  "allowed_hosts": ["rime.example.test", "127.0.0.1"],
  "access_token_file": "/run/secrets/rime-word-marker-token",
  "max_request_body_mb": 512
}
```

示例 Nginx 配置：

```nginx
server {
    listen 80;
    server_name rime.example.test;

    client_max_body_size 512m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

如果不是纯内网使用，建议在 Nginx 层启用 HTTPS。应用内置的 `access_token` 是轻量访问控制，适合个人内网或反代后的简单保护；如果要暴露到公网，仍建议叠加 Nginx Basic Auth、IP 白名单或 SSO。

首次授权需要在 URL 中携带 `?token=...`。应用会立即写入 Cookie 并跳转去掉 token，但反向代理的访问日志仍可能记录首次请求的完整 query string。若 token 需要严格保密，建议调整 Nginx `log_format`，避免记录 `$request_uri` 中的查询参数，或在首次授权后轮换 token。

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

也支持 Rime `userdb` 导出的用户词典行：

```text
# Rime user dictionary
#@/db_name	luna_pinyin
bu 	不	c=1 d=0.909373 t=35
拼音\t词条\tc=词频 d=... t=...
```

说明：

- 列之间用 `Tab` 分隔
- 后两列可省略
- `userdb` 格式只读取 `c` 作为词频，不处理 `d` 和 `t`
- 缺失拼音时自动补全
- 缺失词频时默认设为 `1`
- 系统会区分“未定义词频”和“词频明确定义为 1”
- 旧版数据库自动升级时，既有词条默认视为“词频未定义”
- 只按词条本身去重
- 重复词条可按导入页选项覆盖拼音、更新词频；默认不覆盖拼音、更新词频
- 更新只会使用导入行中实际提供的列，省略拼音或词频时不会覆盖已有值
- 可选“忽略拼音”，导入时丢弃文件中的拼音并使用内置拼音生成；该选项不能和“覆盖拼音”同时启用
- “更新词频”会取已有词频和导入词频中的较大值；如果已有词频未定义，则使用导入词频并标记为已定义
- 可选“全部接受”，把本次导入涉及的词条直接标注为接受并写入标注时间
- 如果文件中包含 Rime YAML 头块，`---` 到 `...` 之间的内容会被整段忽略

## 导出格式

导出为 Rime `.dict.yaml`，支持：

- 选择导出哪些状态
- 选择是否包含词频
- 选择是否纳入 AI 辅助结果
- 可在主词库、中英混杂专用词库、OpenCC 延伸词典三种导出类型之间切换
- OpenCC 延伸词典每行为 `输入内容<Tab>输入内容 响应内容1 响应内容2`
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
- 导入重复词统计改成批量查询，并支持可选覆盖拼音和更新词频

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

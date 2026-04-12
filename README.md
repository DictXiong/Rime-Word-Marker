# Rime Word Marker

一个面向 Rime 词库整理场景的本地 Web 应用，支持导入、导出、逐条标注和高密度词库管理。

> Assisted-by: Codex:GPT-5.4

## 功能概览

- SQLite 存储词条、拼音、词频、状态、导入时间、标注时间
- 按 Rime 风格逐行导入词库，自动去重
- 缺失拼音时自动补全带声调拼音
- 按状态导出 Rime YAML，可选是否包含词频
- 审核页支持顺序 / 随机模式、历史跳转、拼音即时编辑
- 审核快捷键支持 `←/↓/→`、`J/K/L`、`1/2/3`、`A/S/D`
- 词库管理页支持分页、搜索、单条编辑、批量修改
- 导入大词库时提供“导入中”遮罩与导入结果摘要

## 页面说明

- 首页：展示总览，并进入不同工作页面
- 筛词页：一次展示一个待定词条，适合连续标注
- 导入 / 导出页：处理词库文件导入与按状态导出
- 词库管理页：用表格高密度浏览、搜索和批量维护词条

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

### 配置文件

示例配置文件见 `config.example.json`：

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "db_path": "./data/words.db"
}
```

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
- `status`：`pending` / `accepted` / `rejected`
- `imported_at`：导入时间
- `labeled_at`：最后一次被标注为接受或拒绝的时间

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
- 只按词条本身去重
- 如果文件中包含 Rime YAML 头块，`---` 到 `...` 之间的内容会被整段忽略

## 导出格式

导出为 Rime `.dict.yaml`，支持：

- 选择导出哪些状态
- 选择是否包含词频
- 实时显示当前选择将导出多少条词条

## 审核页说明

- 支持顺序模式和随机模式
- 支持浏览器本地历史跳转
- 支持直接编辑当前词条拼音
- 审核 session 按浏览器标签页隔离，不同标签页不会互相覆盖审核进度

快捷键：

- `←` / `J` / `1` / `A`：接受
- `↓` / `K` / `2` / `S`：待定
- `→` / `L` / `3` / `D`：拒绝
- `Ctrl/Cmd + S`：保存拼音

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
- 导入重复词统计改成批量查询，减少大词库重复导入时的性能损耗

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

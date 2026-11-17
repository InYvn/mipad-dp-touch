# AI 许愿工坊 · LLM 接入层（独立插件）实施方案

> 状态：方案稿，未落地。基准 `main` @ `2e8e449`（含上一轮内置版 AI 页，按本方案要回滚）。
> 本方案只做 **LLM 接入层 + 模型配置界面**；不做设置修改、不做插件生成。

---

## 0. 结论

1. **AI 许愿工坊改为独立插件**，宿主只加一条注册表；上一轮内置进主程序的 AI 页（`2e8e449`）**回滚**。
2. **Kilo 免费通道用 stdlib 独立实现**（urllib + SSE），零依赖、零登录、零 Key，默认可用。
3. **LiteLLM 只作为可选后端**：不在宿主进程里 import；用独立 sidecar 进程 + 按需创建的 venv，用户不用就不装、不加载。
4. **统一 `LLMClient`** 抽象 Kilo / OpenAI 兼容 / LiteLLM 三种后端；宿主完全不知道各家的协议。
5. LLM 核心是一个**不依赖 AppKit、不依赖宿主**的纯 Python 包，可整体搬到别的项目。

---

## 1. 现状核查（决定方案的事实）

### 1.1 插件契约（`src/dptouch_plugin.py` + `src/dptouch.py`）

- 注册表 `PLUGINS` 是唯一事实来源，字段：`id / name / brand / blurb / repo / asset_re / entry / min_host / symbol`。
- 安装：从 GitHub Release 拉资产，**发版正文里必须有该 ZIP 的 SHA256**，否则拒绝安装；ZIP 上限 **20 MB**（`MAX_ZIP_BYTES`）。
- 加载：`PLUG.load(pid)` → `importlib` 在**宿主进程内** `exec_module`，插件与 App 同权限。
- 宿主约定：

  ```python
  mod = import_entry()
  ctl = mod.create(on_hide=..., log=...)
  ctl.show() / hide() / toggle() / is_visible
  ctl.set_auto(True) / ctl.tick()      # tick 由宿主 1s 定时器驱动
  ctl.close()                          # 卸载前收尾
  ```

- **按需加载**：`_plug_ensure()` 在用户点「显示」或菜单项时才 import；不用不占内存。
- 插件可以自由建 `NSPanel`/窗口、用 AppKit、读写自己的目录；宿主不传引擎/HID 对象。

### 1.2 宿主的 AI 现状

- 上一轮 `2e8e449` 把 AI 页做进了主程序（侧栏第 7 页 + `ai_*` 模块 + Provider + 设置桥）。
- 与本次要求冲突（必须插件化），**整体回滚**；其中协议/SSE/Provider 的思路可以带到插件里重写（不 import 宿主模块）。

### 1.3 Kilo 免费接口（已实测，2026-11-16）

```bash
# 1) 模型列表：无需任何鉴权
curl https://ai-gateway.kilo.ai/api/v1/models
# → 200，469 个模型，其中 pricing.prompt=completion=0 的免费模型 17 个
#   含 kilo-auto/free、nvidia/nemotron-3.5-*:free、baidu/ernie-5.5-flash-preview:free 等

# 2) 对话：完全不带 Authorization 也能用
curl -X POST https://ai-gateway.kilo.ai/api/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"kilo-auto/free","messages":[{"role":"user","content":"say ok"}],"max_tokens":16}'
# → 200，自动路由到 nvidia/nemotron-3.5-super-vl:free

# 3) 流式：SSE，data: {...}，delta 里有 content / reasoning
curl -N -X POST ... -d '{"model":"kilo-auto/free","messages":[...],"stream":true}'
```

要点：
- **不需要 Key、不需要登录**；`kilo-auto/free` 会自动挑一个免费模型，响应里的 `model` 字段是实际模型名。
- 部分模型先输出 `delta.reasoning`（思维链），`delta.content` 稍后才来；UI 要能分别处理，且不能把 reasoning 当正文。
- 免费模型没有 SLA，可能限流/换模型；错误要如实显示。

### 1.4 LiteLLM 的现实约束

- 宿主是 PyInstaller 打的 `.app`，**没有 pip、没有可写 site-packages**；不能要求用户 `pip install litellm` 到宿主里。
- litellm + 依赖（openai/httpx/pydantic/tiktoken/jinja2/jsonschema…）远超插件 ZIP 的 20 MB 上限，**不能随插件打包**。
- 本机 `/usr/bin/python3` 是 **3.9.6**；新版 litellm 需要 3.10+。所以 sidecar 需要能找到 3.10+ 解释器（`uv` / Homebrew python3.12 / 用户指定）。

结论：LiteLLM 只能是**可选 sidecar**，不能进宿主进程，也不能塞进插件包。

---

## 2. 架构

```
Mipad DP Touch（宿主，只认识 plugin id）
   └─ 插件与工具 → 「AI 许愿工坊」显示
        └─ aiworkshop_plugin（进程内，轻）
             ├─ ui/            窗口、聊天视图、设置面板
             ├─ store/         配置 + 会话历史（本地 JSON）
             └─ llm/           ★ 可移植核心（不 import AppKit / 宿主）
                  ├─ client.py        LLMClient：统一 stream / models / test
                  ├─ types.py         Message / Chunk / ModelInfo / LLMError
                  ├─ backends/
                  │    ├─ kilo.py            stdlib HTTP + SSE（零依赖）
                  │    ├─ openai_compat.py   OpenRouter / DeepSeek / OpenCode Go / Gemini(OpenAI 口)
                  │    └─ litellm_sidecar.py 子进程 JSON-lines
                  └─ sidecar/litellm_worker.py  在独立 venv 里跑，import litellm
```

依赖方向：`ui → llm`，`llm` 不反向依赖任何宿主或 UI；`llm` 只用标准库（Kilo / OpenAI 兼容）或子进程（LiteLLM）。

---

## 3. 统一 LLM Client

### 3.1 接口

```python
class LLMClient:
    def list_models(self, provider_id: str) -> list[ModelInfo]
    def stream(self, provider_id: str, model: str, messages: list[Message],
               *, temperature=0.7, max_tokens=1200, cancel: Event | None = None
               ) -> Iterator[Chunk]
    def test(self, provider_id: str, model: str | None = None) -> TestResult
```

```python
Chunk = {"type": "content"|"reasoning"|"usage"|"done"|"error",
         "text": str, "usage": dict|None, "error": LLMError|None}
```

`LLMError` 统一分类：`auth / rate_limit / timeout / network / bad_response / config / not_installed`，带 `status`、`retry_after`、`user_text()`（中文可读）。

### 3.2 Provider 描述（插件自己的注册表）

```python
PROVIDERS = {
  "kilo":        {"kind": "kilo",  "title": "Kilo 免费", "needs_key": False,
                  "base": "https://ai-gateway.kilo.ai/api/v1",
                  "default_model": "kilo-auto/free", "free_only": True},
  "openrouter":  {"kind": "openai", "title": "OpenRouter", "needs_key": True,
                  "base": "https://openrouter.ai/api/v1"},
  "deepseek":    {"kind": "openai", "title": "DeepSeek", "needs_key": True,
                  "base": "https://api.deepseek.com/v1"},
  "opencode_go": {"kind": "openai", "title": "OpenCode Go", "needs_key": True,
                  "base": "https://opencode.ai/zen/go/v1"},
  "gemini":      {"kind": "openai", "title": "Gemini", "needs_key": True,
                  "base": "https://generativelanguage.googleapis.com/v1beta/openai"},
  "litellm":     {"kind": "litellm", "title": "LiteLLM（自定义）", "needs_key": "optional",
                  "base": "", "model_hint": "openrouter/anthropic/claude-…"},
}
```

> 这四家都有 OpenAI 兼容口，所以「不装 LiteLLM 也能用自定义 API」是成立的；LiteLLM 用于不兼容 OpenAI 协议的后端（Bedrock/Vertex/本地特殊协议等），或用户已经习惯的 model 字符串。

### 3.3 Kilo 后端（stdlib）

- `GET /models` → 过滤 `pricing.prompt == "0" and pricing.completion == "0"`，保留 `kilo-auto/free` 与 `:free`；缓存 10 分钟。
- `POST /chat/completions`，`stream=true`；`urllib.request` 逐行读 `data: `；`[DONE]` 结束。
- 超时：连接 10s / 读 120s；取消：关闭 response（`resp.close()`）+ 置 cancel。
- 错误：401/403/429/5xx 映射为 LLMError；空响应/非 JSON 行忽略并计数，连续坏行 >20 判 `bad_response`。

### 3.4 OpenAI 兼容后端

同一份实现，`base_url + key + model` 可配；`Authorization: Bearer`；请求体与 Kilo 相同；`/models` 失败时允许手动填模型名（有些兼容服务不实现 /models）。

### 3.5 LiteLLM sidecar

- 插件首次选择「LiteLLM」时，检测可用运行时：
  1. `uv`（`uv venv --python 3.12` + `uv pip install litellm`）
  2. `python3.12 / 3.11 / 3.10`（`-m venv` + `pip install litellm`）
  3. 用户手动指定解释器路径（设置里）
- venv 放在 `~/Library/Application Support/MipadDPTouch/plugins/aiworkshop/runtime/`。
- 启动 `runtime/bin/python -m aiworkshop_plugin.llm.sidecar.litellm_worker`，**JSON-lines**：

  ```json
  {"op":"chat","model":"openrouter/...","messages":[...],"stream":true,"api_key":"...","api_base":"..."}
  → {"event":"delta","text":"..."} / {"event":"usage",...} / {"event":"done"}
  {"op":"models"} → {"event":"models","data":[...]}
  {"op":"test","model":"..."} → {"event":"ok"|"error",...}
  ```

- 生命周期：懒启动；空闲 10 分钟退出；插件关闭/卸载时 terminate；崩溃自动重启一次。
- 没装 LiteLLM 时：UI 明确显示「未安装」+ 安装按钮；Kilo 与 OpenAI 兼容通道不受影响。

---

## 4. 配置与密钥

- 配置：`~/Library/Application Support/MipadDPTouch/plugins/aiworkshop/config.json`
  ```json
  {"provider":"kilo","model":"kilo-auto/free","recent":[...],
   "openai":{"openrouter":{"base":"...","model":"..."}, "...":{}},
   "litellm":{"python":"","api_base":""}}
  ```
- 密钥：macOS 钥匙串（`/usr/bin/security`，service `MipadDPTouch-aiworkshop`，account=provider），不写配置文件、不进日志。
- 会话历史：`.../aiworkshop/sessions/<id>.json`，保留最近 N 个会话（默认 30），单条消息截断 32 KB。

---

## 5. 界面（插件自己的窗口）

一个浮动 `NSPanel`（不占 Dock、不建菜单栏图标），宿主「插件与工具」里的「显示」打开：

```
┌──────────────────────────────────────────────┐
│ AI 许愿工坊      [模型 ▾]  ● 已连接   [设置] │
├──────────────────────────────────────────────┤
│  对话区（可滚动，流式追加）                   │
│  ── 思考（可折叠，reasoning）                 │
│  正文…                                        │
├──────────────────────────────────────────────┤
│ [输入框]                        [发送] [停止] │
└──────────────────────────────────────────────┘
```

设置面板（sheet）三个分区：
1. **Kilo 免费**：说明「无需登录/Key」；模型下拉（免费列表）；测试连接。
2. **自定义 API**：Provider 下拉（OpenRouter/DeepSeek/OpenCode Go/Gemini/自定义）；Base URL、模型、Key；测试。
3. **LiteLLM**：状态（未安装/已安装/版本）、安装按钮、解释器路径、默认模型；测试。

交互：
- 流式输出在后台线程，`tick()` 把缓冲刷到 UI；「停止」关连接。
- 错误以红色系统条呈现，不静默；429 显示 `Retry-After`。
- 模型切换立即生效，下一次请求用新模型。

---

## 6. 宿主改动（唯一的一处）

`src/dptouch_plugin.py` 的 `PLUGINS` 加：

```python
{
  "id": "aiworkshop",
  "name": "AI 许愿工坊",
  "brand": "AI Workshop",
  "blurb": "统一模型服务：Kilo 免费开箱即用；自定义 API 走 OpenAI 兼容或 LiteLLM。",
  "repo": "InYvn/AIWorkshop",
  "asset_re": r"^aiworkshop-plugin-(.+)\.zip$",
  "entry": "aiworkshop_plugin",
  "min_host": "2.2.5",
  "symbol": "sparkles",
}
```

外加：
- 回滚 `2e8e449` 的内置 AI 页（`dptouch_window.py` 的 PAGES/动作、`dptouch.py` 的初始化、`ai_*` 模块、build hidden imports、CHANGELOG 条目）。
- 宿主**不引入**任何 LLM 依赖，也不认识 Kilo/LiteLLM 的协议。

---

## 7. 仓库与发布

- 新仓库 `InYvn/AIWorkshop`（插件独立版本线，MIT）。
- 目录：
  ```
  aiworkshop_plugin/        # 入口包（__init__.py 暴露 create）
  aiworkshop_plugin/llm/    # 可移植核心
  aiworkshop_plugin/ui/
  plugin.json
  tests/
  tools/make_plugin_zip.py
  ```
- 发布：ZIP 资产 + 发版正文写 `aiworkshop-plugin-X.Y.Z.zip sha256: <64hex>`（宿主强制校验）。
- 插件包体目标 < 200 KB（不含 LiteLLM）。

---

## 8. 里程碑

| 阶段 | 内容 | 估时 |
|---|---|---|
| **M1** | 插件骨架 + Kilo 后端 + 聊天窗口 + 免费模型列表 + 流式 | 2–3 天 |
| **M2** | OpenAI 兼容 Provider（四家预设）+ 设置面板 + 测试连接 + 会话历史 | 2 天 |
| **M3** | LiteLLM sidecar + 运行时安装流程 + 崩溃恢复 | 2–3 天 |
| **M4** | 错误态/停止/打包/发布 + 宿主注册表条目 | 1–2 天 |

---

## 9. 风险

1. **免费模型没有 SLA**：可能限流、换路由、输出 reasoning 占满 token；UI 要能显示实际模型名和 finish_reason。
2. **LiteLLM 安装门槛**：需要 3.10+ 解释器与网络；没有 uv/新版 Python 的用户只能走 OpenAI 兼容通道。方案里把它做成"可选、可失败、不阻塞 Kilo"。
3. **插件在宿主进程内**：UI 崩溃会带走主程序；网络/解析全部放线程，sidecar 隔离 LiteLLM。
4. **20 MB ZIP 上限**：不能 vendor LiteLLM；也不该 vendor。
5. **取消语义**：SSE 读循环要能被 `resp.close()` 打断；停止后丢弃尾部数据。

---

## 10. 需要你确认

1. **LiteLLM 形态**：按方案做「sidecar venv（按需安装）」？还是只支持用户已有的 LiteLLM proxy（填 Base URL 即可）？
2. **默认模型**：`kilo-auto/free`（自动路由到免费模型）作为默认，是否接受？
3. **模型列表**：默认只显示免费模型；是否要一个「显示全部（可能需登录/付费）」开关？
4. **内置版处理**：确认回滚 `2e8e449`（内置 AI 页）？
